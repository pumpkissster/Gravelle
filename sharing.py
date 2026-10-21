"""Password protected, revocable single-work guest capabilities.

Guest access never authenticates a normal account. Every image read validates
the capability twice, with no database transaction held during media IO.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing, contextmanager
import hashlib
import re
import secrets
import threading
import time

from accounts import check_password, hash_password
from share_crypto import encode_key, encrypt, generate_key
from share_passwords import SharePasswordVault

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
BOOK_ID = re.compile(r"[a-f0-9]{24}\Z")
SESSION_SECONDS = 24 * 60 * 60
ATTEMPT_WINDOW = 5 * 60
MAX_ATTEMPTS = 8
MAX_RATE_KEYS = 4096
MAX_LIVE_SESSIONS = 512
SUPPORTED_FORMATS = frozenset(("folder", "zip", "cbz", "archive", "pdf", "epub"))


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class SharingManager:
    def __init__(self, library, error_type=None):
        self.library = library
        if error_type is None:
            from server import ReaderError
            error_type = ReaderError
        self.error_type = error_type
        self.rate_lock = threading.Lock()
        self.attempts = OrderedDict()
        self.image_slots = threading.BoundedSemaphore(2)
        self.password_slots = threading.BoundedSemaphore(2)
        self.closed = threading.Event()
        with closing(library.connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS shares (
                    id TEXT PRIMARY KEY,
                    token TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    max_visits INTEGER CHECK(max_visits BETWEEN 1 AND 10),
                    visits INTEGER NOT NULL DEFAULT 0,
                    revoked_at REAL,
                    CHECK((expires_at IS NULL) <> (max_visits IS NULL))
                );
                CREATE INDEX IF NOT EXISTS shares_creator ON shares(creator_id,created_at);
                CREATE TABLE IF NOT EXISTS share_sessions (
                    token_hash TEXT PRIMARY KEY,
                    share_id TEXT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
                    encryption_key BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS share_sessions_expiry ON share_sessions(expires_at);
                CREATE INDEX IF NOT EXISTS share_sessions_share ON share_sessions(share_id);
            """)
            if "password_encrypted" not in {row["name"] for row in connection.execute("PRAGMA table_info(shares)")}:
                connection.execute("ALTER TABLE shares ADD COLUMN password_encrypted BLOB")
            # The unreleased prototype supported count-only capabilities with
            # no deadline. Retire them atomically instead of accidentally
            # turning those links into unrestricted, permanent time shares.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""UPDATE shares SET revoked_at=COALESCE(revoked_at,?)
                WHERE expires_at IS NULL OR max_visits IS NOT NULL""", (time.time(),))
            connection.execute("""DELETE FROM share_sessions WHERE share_id IN
                (SELECT id FROM shares WHERE expires_at IS NULL OR max_visits IS NOT NULL)""")
            encrypted_exists = connection.execute("SELECT 1 FROM shares WHERE password_encrypted IS NOT NULL LIMIT 1").fetchone() is not None
        self.password_vault = SharePasswordVault(library.data, allow_create=not encrypted_exists)

    def _error(self, message, status=400):
        return self.error_type(message, status)

    def close(self):
        self.closed.set()
        with self.rate_lock:
            self.attempts.clear()

    def _open(self):
        if self.closed.is_set():
            raise self._error("分享服务正在关闭，请稍后再试。", 503)

    @contextmanager
    def _password_work(self):
        # Share creation and anonymous password verification use the same
        # process-wide manager budget. Never queue unbounded KDF work in HTTP
        # threads, or hold a database write transaction while hashing.
        self._open()
        if not self.password_slots.acquire(blocking=False):
            raise self._error("密码验证正忙，请稍后再试。", 503)
        try:
            self._open()
            yield
            self._open()
        finally:
            self.password_slots.release()

    @staticmethod
    def _valid_token(value):
        return isinstance(value, str) and TOKEN.fullmatch(value) is not None

    def _admin(self, connection):
        self.library.require_admin(connection)
        actor = self.library._actor(connection)
        # The pre-registration internal administrator is not an account that
        # may issue public capabilities.
        row = connection.execute("SELECT id,role,enabled FROM users WHERE id=?", (actor["id"],)).fetchone()
        if row is None or row["role"] != "admin" or not row["enabled"]:
            raise self._error("此操作需要管理员权限。", 403)
        return row["id"]

    def _book(self, connection, row):
        owner = connection.execute("SELECT role,enabled FROM users WHERE id=?", (row["creator_id"],)).fetchone()
        if owner is None or owner["role"] != "admin" or not owner["enabled"]:
            return None
        with self.library.acting_as(row["creator_id"]):
            book = self.library._select(connection, row["book_id"])
        if book is None or book["format"] not in SUPPORTED_FORMATS or book["page_count"] < 1:
            return None
        # _select checks active and shared publication state. Removed entries
        # must never remain accessible even during a scan publication race.
        if "removed" in book.keys() and book["removed"]:
            return None
        return book

    def _state(self, row, book, now):
        if row["revoked_at"] is not None:
            return "closed"
        # Also fail closed for a legacy record encountered before a restart or
        # restored while the service is running. A deadline is mandatory.
        if row["expires_at"] is None or row["max_visits"] is not None:
            return "unavailable"
        if now >= row["expires_at"]:
            return "expired"
        if book is None:
            return "unavailable"
        return "active"

    def _share(self, connection, token, now=None):
        self._open()
        if not self._valid_token(token):
            raise self._error("分享链接不存在。", 404)
        row = connection.execute("SELECT * FROM shares WHERE token_hash=?", (_digest(token),)).fetchone()
        if row is None:
            raise self._error("分享链接不存在。", 404)
        book = self._book(connection, row)
        state = self._state(row, book, time.time() if now is None else now)
        if state in ("closed", "expired", "unavailable"):
            raise self._error("分享已失效或不可用。", 410)
        return row, book, state

    def _session(self, connection, share, token, now):
        if not self._valid_token(token):
            return None
        return connection.execute("""SELECT * FROM share_sessions
            WHERE token_hash=? AND share_id=? AND expires_at>?""",
            (_digest(token), share["id"], now)).fetchone()

    def _management(self, connection, row, password_key):
        book = self._book(connection, row)
        # Administrative metadata includes no password hash or guest session.
        # The capability token is retained for copying an existing share.
        title = "作品不可用" if book is None else (book["title_override"] or book["title"])
        return {"id": row["id"], "token": row["token"], "book_id": row["book_id"],
                "title": title, "created_at": row["created_at"],
                "expires_at": row["expires_at"], "revoked_at": row["revoked_at"],
                "visits": row["visits"],
                "password_available": self.password_vault.available(row["password_encrypted"], password_key),
                "mode": "time",
                "status": self._state(row, book, time.time())}

    def create(self, body):
        self._open()
        if not isinstance(body, dict):
            raise self._error("分享设置格式不正确。")
        allowed = {"book_id", "mode", "days", "password"}
        if set(body) - allowed:
            raise self._error("分享设置包含不支持的选项。")
        book_id = body.get("book_id")
        if not isinstance(book_id, str) or not BOOK_ID.fullmatch(book_id):
            raise self._error("请选择需要分享的作品。")
        mode = body.get("mode", "time")
        if mode != "time":
            raise self._error("分享仅支持按时间设置。")
        days = body.get("days", 1)
        if type(days) is not int or days not in (1, 3, 7):
            raise self._error("分享时间请选择 1 天、3 天或 7 天。")
        password = body.get("password", None)
        if password is None:
            password = secrets.token_urlsafe(12)
        if (not isinstance(password, str) or not 8 <= len(password) <= 64
                or any(0xD800 <= ord(char) <= 0xDFFF for char in password)):
            raise self._error("访问密码需要为 8 至 64 个字符。")
        # Authentication and eligibility precede expensive password hashing.
        with closing(self.library.connect()) as connection:
            creator = self._admin(connection)
            candidate = {"creator_id": creator, "book_id": book_id}
            if self._book(connection, candidate) is None:
                raise self._error("作品不可用，或暂不支持分享此格式。", 404)
        with self._password_work():
            encoded = hash_password(password)
        now, token, identifier = time.time(), secrets.token_urlsafe(32), secrets.token_hex(16)
        password_encrypted = self.password_vault.seal(password, identifier, creator)
        if password_encrypted is None:
            raise self._error("分享密码密钥不可用，请恢复服务端 data/share-passwords.key 后重试。", 503)
        with closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._admin(connection) != creator or self._book(connection, candidate) is None:
                raise self._error("作品已不可用。", 410)
            password_key = self.password_vault.key()
            if not self.password_vault.available(password_encrypted, password_key):
                raise self._error("分享密码密钥不可用，请恢复服务端 data/share-passwords.key 后重试。", 503)
            live = connection.execute("""SELECT COUNT(*) FROM shares WHERE creator_id=?
                AND revoked_at IS NULL AND expires_at>? AND max_visits IS NULL""",
                (creator, now)).fetchone()[0]
            if live >= 500:
                raise self._error("有效分享过多，请先关闭不再需要的分享。", 409)
            connection.execute("""INSERT INTO shares
                (id,token,token_hash,creator_id,book_id,password_hash,created_at,expires_at,max_visits,password_encrypted)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (identifier, token, _digest(token), creator,
                book_id, encoded, now, now + days * 86400, None, password_encrypted))
            row = connection.execute("SELECT * FROM shares WHERE id=?", (identifier,)).fetchone()
            result = self._management(connection, row, password_key)
        return {"share": result, "password": password}

    def listing(self):
        self._open()
        with closing(self.library.connect()) as connection:
            creator = self._admin(connection)
            rows = connection.execute("SELECT * FROM shares WHERE creator_id=? ORDER BY created_at DESC,id DESC", (creator,)).fetchall()
            key = self.password_vault.key()
            return {"shares": [self._management(connection, row, key) for row in rows]}

    def revoke(self, identifier):
        self._open()
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise self._error("分享不存在。", 404)
        with closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            creator = self._admin(connection)
            row = connection.execute("SELECT * FROM shares WHERE id=? AND creator_id=?", (identifier, creator)).fetchone()
            if row is None:
                raise self._error("分享不存在。", 404)
            connection.execute("UPDATE shares SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (time.time(), identifier))
            connection.execute("DELETE FROM share_sessions WHERE share_id=?", (identifier,))
            row = connection.execute("SELECT * FROM shares WHERE id=?", (identifier,)).fetchone()
            return {"share": self._management(connection, row, self.password_vault.key())}

    def reveal_password(self, identifier):
        self._open()
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise self._error("分享不存在。", 404)
        with closing(self.library.connect()) as connection:
            creator = self._admin(connection)
            row = connection.execute("SELECT password_encrypted FROM shares WHERE id=? AND creator_id=?",
                                     (identifier, creator)).fetchone()
            if row is None:
                raise self._error("分享不存在。", 404)
            if row["password_encrypted"] is None:
                raise self._error("此分享创建时未保留可查看的密码，可重新分享生成新密码。", 409)
            password = self.password_vault.reveal(row["password_encrypted"], identifier, creator)
            if password is None:
                raise self._error("此分享的密码暂时无法查看，可重新分享生成新密码。", 409)
            return {"password": password}

    def delete(self, identifier):
        """Remove only this creator's explicitly closed sharing record."""
        self._open()
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise self._error("分享不存在。", 404)
        with closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            creator = self._admin(connection)
            row = connection.execute("SELECT revoked_at FROM shares WHERE id=? AND creator_id=?",
                                     (identifier, creator)).fetchone()
            if row is None:
                raise self._error("分享不存在。", 404)
            if row["revoked_at"] is None:
                raise self._error("请先关闭分享，再删除记录。", 409)
            # Explicitly delete sessions as well as the cascading FK. Nothing
            # in the catalogue, personal records, or source files is touched.
            connection.execute("DELETE FROM share_sessions WHERE share_id=?", (identifier,))
            connection.execute("DELETE FROM shares WHERE id=? AND creator_id=?", (identifier, creator))
        return {"deleted": True, "id": identifier}

    def _manifest(self, connection, book, session):
        # _select resolves the creator's personal preferences for ordinary
        # reading. Guest defaults use only published work/library settings;
        # never export the creator's account preferences or progress.
        defaults = connection.execute("""SELECT
            COALESCE(m.reading_mode_override,l.reading_mode) AS reading_mode,
            COALESCE(m.reading_direction_override,l.reading_direction) AS reading_direction
            FROM books b JOIN media_libraries l ON l.id=b.library_id
            LEFT JOIN book_metadata m ON m.book_id=b.id WHERE b.id=?""", (book["id"],)).fetchone()
        mode = defaults["reading_mode"] if defaults is not None else None
        direction = defaults["reading_direction"] if defaults is not None else None
        mode = mode if mode in ("single", "double", "webtoon") else "single"
        direction = direction if direction in ("ltr", "rtl", "ttb") else "ltr"
        if mode == "webtoon":
            direction = "ttb"
        elif mode == "double" and direction == "ttb":
            direction = "ltr"
        return {"manifest": {"title": book["title_override"] or book["title"],
                             "page_count": book["page_count"], "format": book["format"],
                             "reading_mode": mode, "reading_direction": direction},
                "key": encode_key(bytes(session["encryption_key"])),
                "session_expires_at": session["expires_at"],
                "encryption": {"algorithm": "AES-GCM", "envelope": "GSH1", "aad_prefix": "gravelle-share-v1:"}}

    def info(self, token, session=""):
        now = time.time()
        with closing(self.library.connect()) as connection:
            row, _, state = self._share(connection, token, now)
            return {"status": state, "requires_password": self._session(connection, row, session, now) is None}

    def manifest(self, token, session):
        now = time.time()
        with closing(self.library.connect()) as connection:
            row, book, _ = self._share(connection, token, now)
            guest = self._session(connection, row, session, now)
            if guest is None:
                raise self._error("请先输入访问密码。", 401)
            return self._manifest(connection, book, guest)

    def logout(self, token, session=""):
        """Revoke only this visitor's session, even after its share expires."""
        self._open()
        if not self._valid_token(token):
            raise self._error("分享链接不存在。", 404)
        if self._valid_token(session):
            with closing(self.library.connect()) as connection, connection:
                connection.execute("""DELETE FROM share_sessions WHERE token_hash=?
                    AND share_id IN (SELECT id FROM shares WHERE token_hash=?)""",
                    (_digest(session), _digest(token)))
        # No password check, account authentication, count adjustment, or
        # share-state lookup: logout is safe and idempotent for stale cookies.
        return {"logged_out": True}

    def _attempt(self, token, client_ip):
        # The server supplies the actual remote address; no proxy headers are
        # trusted here. Both attempts and their key lengths are bounded.
        address = str(client_ip)[:255]
        key = (_digest(token), hashlib.sha256(address.encode("utf-8", "replace")).hexdigest())
        now = time.monotonic()
        with self.rate_lock:
            # Prune stale keys without making normal requests scan an
            # unbounded attacker-controlled structure.
            for stale in list(self.attempts):
                history = self.attempts[stale]
                if not history or now - history[-1] >= ATTEMPT_WINDOW:
                    del self.attempts[stale]
            history = self.attempts.pop(key, [])
            history = [item for item in history if now - item < ATTEMPT_WINDOW]
            if len(history) >= MAX_ATTEMPTS:
                self.attempts[key] = history
                raise self._error("尝试次数过多，请稍后再试。", 429)
            history.append(now)
            self.attempts[key] = history
            while len(self.attempts) > MAX_RATE_KEYS:
                self.attempts.popitem(last=False)

    def unlock(self, token, password, session="", client_ip=""):
        now = time.time()
        with closing(self.library.connect()) as connection:
            row, book, _ = self._share(connection, token, now)
            guest = self._session(connection, row, session, now)
            if guest is not None:
                return {**self._manifest(connection, book, guest), "session": session}
            stored_hash = row["password_hash"]
        if not isinstance(password, str) or len(password) > 64:
            self._attempt(token, client_ip)
            raise self._error("访问密码不正确。", 403)
        with self._password_work():
            # A busy service has not actually attempted a password. Count
            # only requests admitted to the bounded verification lane.
            self._attempt(token, client_ip)
            valid = check_password(password, stored_hash)
        if not valid:
            raise self._error("访问密码不正确。", 403)
        raw_session, key = secrets.token_urlsafe(32), generate_key()
        now = time.time()
        with closing(self.library.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row, book, _ = self._share(connection, token, now)
            guest = self._session(connection, row, session, now)
            if guest is not None:
                return {**self._manifest(connection, book, guest), "session": session}
            connection.execute("DELETE FROM share_sessions WHERE expires_at<=?", (now,))
            if connection.execute("SELECT COUNT(*) FROM share_sessions WHERE share_id=?", (row["id"],)).fetchone()[0] >= MAX_LIVE_SESSIONS:
                raise self._error("当前访问人数较多，请稍后再试。", 429)
            expiry = min(now + SESSION_SECONDS, row["expires_at"])
            connection.execute("""INSERT INTO share_sessions
                (token_hash,share_id,encryption_key,created_at,expires_at) VALUES(?,?,?,?,?)""",
                (_digest(raw_session), row["id"], key, now, expiry))
            connection.execute("UPDATE shares SET visits=visits+1 WHERE id=?", (row["id"],))
            guest = self._session(connection, row, raw_session, now)
            return {**self._manifest(connection, book, guest), "session": raw_session}

    def image(self, token, session, index, width=None):
        self._open()
        if not self.image_slots.acquire(blocking=False):
            raise self._error("图片正在加载，请稍后再试。", 503)
        try:
            return self._image(token, session, index, width)
        finally:
            self.image_slots.release()

    def _image(self, token, session, index, width=None):
        if type(index) is not int or index < 0:
            raise self._error("图片页码不存在。", 404)
        if width is not None and (type(width) is not int or not 1 <= width <= 4096):
            raise self._error("图片宽度无效。")
        now = time.time()
        with closing(self.library.connect()) as connection:
            row, book, _ = self._share(connection, token, now)
            guest = self._session(connection, row, session, now)
            if guest is None:
                raise self._error("请先输入访问密码。", 401)
            if index >= book["page_count"]:
                raise self._error("图片页码不存在。", 404)
            creator, book_id = row["creator_id"], row["book_id"]
            key = bytes(guest["encryption_key"])
        # This scoped identity is thread-local and always restored, including
        # decoder exceptions. It neither grants guest normal-API access nor
        # bypasses publication and safe-path checks in the existing reader.
        with self.library.acting_as(creator):
            data, mime, _etag = self.library.image(book_id, index, width=width)
        packet = encrypt(data, mime, key, ("gravelle-share-v1:" + token + ":" + str(index)).encode("utf-8"))
        # Closing, account changes, expiry, or scan publication changes while
        # reading/decoding must be observed before any encrypted bytes escape.
        now = time.time()
        with closing(self.library.connect()) as connection:
            current, book, _ = self._share(connection, token, now)
            guest = self._session(connection, current, session, now)
            if guest is None:
                raise self._error("访问已失效，请重新打开分享。", 401)
            if current["book_id"] != book_id or index >= book["page_count"]:
                raise self._error("分享作品已变化，请重新打开分享。", 410)
        return packet, "application/octet-stream"
