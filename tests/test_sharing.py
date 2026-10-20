"""Guest capability security and concurrency against an isolated catalogue."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image

from sharing import MAX_ATTEMPTS, MAX_RATE_KEYS, SESSION_SECONDS, SharingManager
from share_passwords import KEY_NAME, SharePasswordVault


class SharingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Catalogue:
    """The minimal Library interface; no personal state or source is exported."""
    def __init__(self, database):
        self.database = database
        self.data = database.parent
        self.request_context = threading.local()
        self.request_context.user_id = 1
        self.read_hook = None
        output = BytesIO()
        Image.new("RGB", (30, 40), "#ee4499").save(output, "PNG")
        self.image_bytes = output.getvalue()
        with closing(self.connect()) as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,enabled INTEGER);
                INSERT INTO users VALUES(1,'admin',1),(2,'user',1),(3,'admin',1);
                CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT,title_override TEXT,
                    format TEXT,page_count INTEGER,active INTEGER,removed INTEGER,published INTEGER);
            """)
            with c:
                for identifier, format_name in (("a"*24, "folder"), ("b"*24, "pdf"), ("c"*24, "epub"),
                                                ("d"*24, "archive"), ("e"*24, "zip"), ("f"*24, "cbz")):
                    c.execute("INSERT INTO books VALUES(?,?,NULL,?,3,1,0,1)", (identifier, "Generated photo work", format_name))

    def connect(self):
        c = sqlite3.connect(self.database, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextmanager
    def acting_as(self, user_id):
        previous = getattr(self.request_context, "user_id", 1)
        self.request_context.user_id = user_id
        try:
            yield self
        finally:
            self.request_context.user_id = previous

    def _actor(self, c):
        user_id = getattr(self.request_context, "user_id", 1)
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None or not row["enabled"]:
            raise SharingError("Login required", 401)
        return row

    def require_admin(self, c):
        if self._actor(c)["role"] != "admin":
            raise SharingError("Admin required", 403)

    def _select(self, c, book_id):
        self._actor(c)
        return c.execute("SELECT * FROM books WHERE id=? AND active=1 AND published=1", (book_id,)).fetchone()

    def image(self, book_id, index, width=None):
        if self.request_context.user_id != 1:
            raise AssertionError("Guest media read did not use its creator")
        if self.read_hook:
            self.read_hook()
        return self.image_bytes, "image/png", '"private-etag"'


class SharingTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "work" / "sharing-tests"
        root.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(scratch.cleanup)
        self.library = Catalogue(Path(scratch.name) / "catalogue.sqlite3")
        self.manager = SharingManager(self.library, SharingError)
        self.addCleanup(self.manager.close)
        self.password = "generated-visit-password"

    def create(self, **options):
        body = {"book_id": "a"*24, "password": self.password}
        body.update(options)
        return self.manager.create(body)

    def unlock(self, share, **options):
        return self.manager.unlock(share["token"], self.password, client_ip="127.0.0.1", **options)

    def statement(self, query, parameters=()):
        with closing(self.library.connect()) as c, c:
            return c.execute(query, parameters).fetchall()

    def assertStatus(self, status, function, *args, **kwargs):
        with self.assertRaises(SharingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def test_creation_uses_random_tokens_and_hashes_password_not_password_itself(self):
        result = self.create()
        share = result["share"]
        self.assertEqual(result["password"], self.password)
        self.assertRegex(share["token"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(share["status"], "active")
        self.assertAlmostEqual(share["expires_at"]-share["created_at"], 86400)
        self.assertNotIn("max_visits", share)
        self.assertEqual(share["visits"], 0)
        self.assertTrue(share["password_available"])
        self.assertNotIn("password_hash", share)
        row = self.statement("SELECT * FROM shares")[0]
        self.assertNotEqual(row["password_hash"], self.password)
        self.assertTrue(row["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertIsInstance(row["password_encrypted"], bytes)
        self.assertNotIn(self.password.encode(), row["password_encrypted"])
        self.assertEqual(row["token_hash"], hashlib.sha256(share["token"].encode()).hexdigest())
        self.assertNotIn(self.password, json.dumps(self.manager.listing()))

    def test_password_reveal_is_creator_admin_only_and_survives_reopen(self):
        share = self.create()["share"]
        self.assertEqual(self.manager.reveal_password(share["id"]), {"password": self.password})
        for actor, status in ((None, 401), (2, 403), (3, 404)):
            with self.library.acting_as(actor):
                self.assertStatus(status, self.manager.reveal_password, share["id"])
        reopened = SharingManager(self.library, SharingError)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.reveal_password(share["id"]), {"password": self.password})
        self.manager.revoke(share["id"])
        self.assertEqual(self.manager.reveal_password(share["id"]), {"password": self.password})
        self.manager.delete(share["id"])
        self.assertStatus(404, self.manager.reveal_password, share["id"])

    def test_listing_includes_only_password_availability_not_password_ciphertext_or_keys(self):
        share = self.create()["share"]
        result = self.manager.listing()["shares"][0]
        self.assertTrue(result["password_available"])
        self.assertEqual(result["visits"], 0)
        for field in ("password", "password_hash", "password_encrypted", "encryption_key", "key"):
            self.assertNotIn(field, result)
        self.assertNotIn(self.password, json.dumps(result))
        self.assertEqual(self.manager.reveal_password(share["id"])["password"], self.password)
        self.assertEqual(self.manager.listing()["shares"][0]["visits"], 0)

    def test_legacy_hash_only_share_keeps_access_but_cannot_reveal_old_password(self):
        share = self.create()["share"]
        self.statement("UPDATE shares SET password_encrypted=NULL WHERE id=?", (share["id"],))
        reopened = SharingManager(self.library, SharingError)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.listing()["shares"][0]["password_available"])
        self.assertStatus(409, reopened.reveal_password, share["id"])
        guest = reopened.unlock(share["token"], self.password)
        self.assertEqual(reopened.info(share["token"], guest["session"])["status"], "active")

    def test_lost_master_key_never_recreated_and_password_verification_still_works(self):
        share = self.create()["share"]
        path = self.library.data / KEY_NAME
        original_key = path.read_bytes()
        path.unlink()
        reopened = SharingManager(self.library, SharingError)
        self.addCleanup(reopened.close)
        self.assertFalse(path.exists())
        self.assertFalse(reopened.listing()["shares"][0]["password_available"])
        self.assertStatus(409, reopened.reveal_password, share["id"])
        self.assertIn("session", reopened.unlock(share["token"], self.password))
        self.assertStatus(503, reopened.create, {"book_id": "a"*24})
        self.assertFalse(path.exists())
        self.assertEqual(self.statement("SELECT COUNT(*) FROM shares")[0][0], 1)
        path.write_bytes(original_key)
        self.assertEqual(reopened.reveal_password(share["id"])["password"], self.password)

    def test_wrong_or_malformed_key_never_overwritten_and_password_hash_still_works(self):
        share = self.create()["share"]
        path = self.library.data / KEY_NAME
        original_key = path.read_bytes()
        for replacement in (b"broken", secrets.token_bytes(32)):
            path.write_bytes(replacement)
            reopened = SharingManager(self.library, SharingError)
            self.addCleanup(reopened.close)
            self.assertEqual(path.read_bytes(), replacement)
            self.assertFalse(reopened.listing()["shares"][0]["password_available"])
            self.assertStatus(409, reopened.reveal_password, share["id"])
            self.assertIn("session", reopened.unlock(share["token"], self.password))
        path.write_bytes(original_key)
        self.assertEqual(self.manager.reveal_password(share["id"])["password"], self.password)

    def test_password_ciphertext_is_bound_to_share_and_creator(self):
        first = self.create()["share"]
        second = self.create(password="other-generated-password")["share"]
        blob = self.statement("SELECT password_encrypted FROM shares WHERE id=?", (first["id"],))[0][0]
        self.statement("UPDATE shares SET password_encrypted=? WHERE id=?", (blob, second["id"]))
        self.assertStatus(409, self.manager.reveal_password, second["id"])
        self.assertIsNone(self.manager.password_vault.reveal(blob, first["id"], 3))
        tampered = blob[:-1] + bytes((blob[-1] ^ 1,))
        self.statement("UPDATE shares SET password_encrypted=? WHERE id=?", (tampered, first["id"]))
        self.assertStatus(409, self.manager.reveal_password, first["id"])

    def test_generated_password_required_and_sensitive_keys_rejected(self):
        result = self.manager.create({"book_id": "a"*24})
        self.assertGreaterEqual(len(result["password"]), 12)
        self.assertStatus(403, self.manager.unlock, result["share"]["token"], "")
        for bad in ("", "tiny", "x"*65, True, "\ud800abcdefgh"):
            self.assertStatus(400, self.create, password=bad)
        self.assertStatus(400, self.create, require_password=False)

    def test_validation_allows_only_time_presets_and_rejects_count_links(self):
        for day in (1, 3, 7):
            share = self.create(days=day)["share"]
            self.assertEqual(share["expires_at"]-share["created_at"], day*86400)
            self.assertEqual(share["mode"], "time")
        for bad in (0, 2, 6, 8, True, "1"):
            self.assertStatus(400, self.create, days=bad)
        self.assertStatus(400, self.create, mode="visits", max_visits=1)
        self.assertStatus(400, self.create, mode="visits")
        self.assertStatus(400, self.create, max_visits=10)
        self.assertStatus(400, self.create, max_visits=None)
        self.assertStatus(400, self.create, book_id="../../private")

    def test_legacy_count_capability_and_sessions_are_retired_on_reopen(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        valid = self.create(days=3)["share"]
        valid_guest = self.unlock(valid)
        self.statement("UPDATE shares SET expires_at=NULL,max_visits=2 WHERE id=?", (share["id"],))
        self.assertStatus(410, self.manager.info, share["token"])
        self.assertStatus(410, self.manager.manifest, share["token"], guest["session"])
        self.assertStatus(410, self.manager.image, share["token"], guest["session"], 0)
        self.assertStatus(410, self.unlock, share)
        reopened = SharingManager(self.library, SharingError)
        self.addCleanup(reopened.close)
        closed = next(item for item in reopened.listing()["shares"] if item["id"] == share["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions WHERE share_id=?", (share["id"],))[0][0], 0)
        self.assertStatus(410, reopened.manifest, share["token"], guest["session"])
        self.assertEqual(reopened.manifest(valid["token"], valid_guest["session"])["key"], valid_guest["key"])
        self.assertEqual(self.statement("SELECT revoked_at FROM shares WHERE id=?", (valid["id"],))[0][0], None)

    def test_anonymous_and_child_cannot_manage_shares(self):
        share = self.create()["share"]
        for actor, status in ((None, 401), (2, 403)):
            with self.library.acting_as(actor):
                self.assertStatus(status, self.create)
                self.assertStatus(status, self.manager.listing)
                self.assertStatus(status, self.manager.revoke, share["id"])
        self.assertEqual(self.library.request_context.user_id, 1)

    def test_management_never_lists_or_revokes_other_creators(self):
        share = self.create()["share"]
        with self.library.acting_as(3):
            self.assertEqual(self.manager.listing(), {"shares": []})
            self.assertStatus(404, self.manager.revoke, share["id"])
        self.assertEqual(len(self.manager.listing()["shares"]), 1)

    def test_info_has_no_title_key_or_personal_data_and_creates_no_session(self):
        share = self.create(days=3)["share"]
        self.assertEqual(self.manager.info(share["token"]), {"status":"active", "requires_password":True})
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 0)
        guest = self.unlock(share)
        self.assertEqual(self.manager.info(share["token"], guest["session"]), {"status":"active", "requires_password":False})
        self.assertEqual(self.manager.info(share["token"], "invalid"), {"status":"active", "requires_password":True})
        self.assertEqual(self.manager.listing()["shares"][0]["visits"], 1)

    def test_unlock_manifest_minimum_and_sessions_are_stored_only_hashed(self):
        share = self.create()["share"]
        with self.library.acting_as(None):
            guest = self.unlock(share)
            result = self.manager.manifest(share["token"], guest["session"])
            self.assertIsNone(self.library.request_context.user_id)
        self.assertEqual(set(result), {"manifest", "key", "session_expires_at", "encryption"})
        self.assertEqual(set(result["manifest"]), {"title", "page_count", "format"})
        self.assertEqual(result["manifest"]["format"], "folder")
        self.assertEqual(len(base64.urlsafe_b64decode(result["key"]+"=")), 32)
        self.assertNotIn("session", result)
        row = self.statement("SELECT * FROM share_sessions")[0]
        self.assertNotEqual(row["token_hash"], guest["session"])
        self.assertEqual(row["token_hash"], hashlib.sha256(guest["session"].encode()).hexdigest())
        self.assertLessEqual(row["expires_at"], share["expires_at"])
        self.assertLessEqual(row["expires_at"]-row["created_at"], SESSION_SECONDS)

    def test_wrong_password_creates_no_guest_session(self):
        share = self.create(days=3)["share"]
        self.assertStatus(403, self.manager.unlock, share["token"], "wrong-password", client_ip="local")
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 0)
        self.assertEqual(self.manager.listing()["shares"][0]["visits"], 0)
        self.assertIn("session", self.unlock(share))

    def test_existing_session_reuse_keeps_key_and_does_not_create_another_session(self):
        share = self.create(days=3)["share"]
        guest = self.unlock(share)
        reused = self.manager.unlock(share["token"], "", session=guest["session"], client_ip="other")
        self.assertEqual(reused["session"], guest["session"])
        self.assertEqual(reused["key"], guest["key"])
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 1)
        self.assertEqual(self.manager.listing()["shares"][0]["visits"], 1)
        self.assertIn("session", self.unlock(share))
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 2)
        self.manager.manifest(share["token"], guest["session"])
        self.manager.image(share["token"], guest["session"], 0)
        self.assertEqual(self.manager.listing()["shares"][0]["visits"], 2)

    def test_parallel_time_share_visitors_obtain_independent_sessions(self):
        share = self.create(days=3)["share"]
        barrier = threading.Barrier(6)
        def visit(number):
            barrier.wait(timeout=10)
            deadline = time.monotonic()+10
            while True:
                try:
                    return self.manager.unlock(share["token"], self.password, client_ip="generated-"+str(number))
                except SharingError as error:
                    if error.status == 503 and time.monotonic()<deadline:
                        time.sleep(.1)
                        continue
                    return error.status
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(visit, range(6)))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 6)
        self.assertEqual(len({item["session"] for item in results}),6)
        self.assertEqual(len({item["key"] for item in results}), 6)
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 6)
        self.assertEqual(self.manager.listing()["shares"][0]["visits"], 6)

    def test_guest_logout_revokes_only_matching_session_without_reducing_visits(self):
        share = self.create()["share"]
        first, second = self.unlock(share), self.unlock(share)
        other = self.create()["share"]
        other_guest = self.unlock(other)
        # A cookie issued for another capability cannot be revoked through
        # this one's endpoint, even when both share the same underlying work.
        self.manager.logout(share["token"], other_guest["session"])
        self.manager.manifest(other["token"], other_guest["session"])
        with self.library.acting_as(None):
            self.assertEqual(self.manager.logout(share["token"], first["session"]), {"logged_out": True})
            self.assertEqual(self.manager.logout(share["token"], first["session"]), {"logged_out": True})
        self.assertStatus(401, self.manager.manifest, share["token"], first["session"])
        self.assertStatus(401, self.manager.image, share["token"], first["session"], 0)
        self.assertStatus(403, self.manager.unlock, share["token"], "incorrect", first["session"])
        self.manager.manifest(share["token"], second["session"])
        rows = {row["id"]: row for row in self.manager.listing()["shares"]}
        self.assertEqual(rows[share["id"]]["visits"], 2)
        self.assertEqual(rows[other["id"]]["visits"], 1)
        replacement = self.unlock(share)
        self.assertNotEqual(replacement["session"], first["session"])
        self.assertNotEqual(replacement["key"], first["key"])

    def test_guest_logout_can_clear_closed_expired_or_unknown_share_and_stale_cookies(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.statement("UPDATE shares SET expires_at=? WHERE id=?", (time.time()-1, share["id"]))
        self.assertEqual(self.manager.logout(share["token"], guest["session"]), {"logged_out": True})
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 0)
        self.manager.revoke(share["id"])
        for session in ("", "bad-cookie", guest["session"]):
            self.assertEqual(self.manager.logout(share["token"], session), {"logged_out": True})
        self.assertEqual(self.manager.logout(secrets.token_urlsafe(32), guest["session"]), {"logged_out": True})

    def test_logout_during_image_read_never_returns_inflight_encrypted_image(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.library.read_hook = lambda: self.manager.logout(share["token"], guest["session"])
        self.assertStatus(401, self.manager.image, share["token"], guest["session"], 0)

    def test_share_and_session_expiry_checked_without_refresh_extension(self):
        with patch("sharing.time.time", return_value=1000):
            share = self.create(days=3)["share"]
            guest = self.unlock(share)
        with patch("sharing.time.time", return_value=1001):
            repeated = self.unlock(share, session=guest["session"])
            self.assertEqual(repeated["session_expires_at"], 1000+SESSION_SECONDS)
        with patch("sharing.time.time", return_value=1000+SESSION_SECONDS):
            self.assertStatus(401, self.manager.manifest, share["token"], guest["session"])
        timed = self.create()["share"]
        guest = self.unlock(timed)
        self.statement("UPDATE shares SET expires_at=? WHERE id=?", (time.time()-1, timed["id"]))
        self.assertStatus(410, self.manager.info, timed["token"], guest["session"])
        self.assertStatus(410, self.manager.image, timed["token"], guest["session"], 0)

    def test_revocation_immediately_rejects_manifest_image_and_unlock(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.assertEqual(self.manager.revoke(share["id"])["share"]["status"], "closed")
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 0)
        self.assertStatus(410, self.manager.manifest, share["token"], guest["session"])
        self.assertStatus(410, self.manager.image, share["token"], guest["session"], 0)
        self.assertStatus(410, self.unlock, share)
        self.assertEqual(self.manager.revoke(share["id"])["share"]["status"], "closed")

    def test_only_creator_admin_can_delete_closed_share(self):
        share = self.create()["share"]
        self.manager.revoke(share["id"])
        for actor, status in ((None, 401), (2, 403), (3, 404)):
            with self.library.acting_as(actor):
                self.assertStatus(status, self.manager.delete, share["id"])
        self.assertStatus(404, self.manager.delete, "invalid-id")
        self.assertEqual(self.manager.delete(share["id"]), {"deleted": True, "id": share["id"]})
        self.assertStatus(404, self.manager.delete, share["id"])

    def test_active_or_expired_share_must_be_closed_before_deletion(self):
        share = self.create()["share"]
        self.assertStatus(409, self.manager.delete, share["id"])
        self.statement("UPDATE shares SET expires_at=0 WHERE id=?", (share["id"],))
        self.assertStatus(409, self.manager.delete, share["id"])
        self.manager.revoke(share["id"])
        self.assertTrue(self.manager.delete(share["id"])["deleted"])

    def test_deletion_invalidates_old_url_and_leaves_work_and_other_share_untouched(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        other = self.create()["share"]
        other_guest = self.unlock(other)
        self.manager.revoke(share["id"])
        # A stale persisted session must be removed as part of the same
        # deletion, even though ordinary revocation already cleans sessions.
        self.statement("""INSERT INTO share_sessions(token_hash,share_id,encryption_key,created_at,expires_at)
            VALUES(?,?,?,?,?)""", (hashlib.sha256(guest["session"].encode()).hexdigest(), share["id"],
            base64.urlsafe_b64decode(guest["key"]+"="), time.time(), time.time()+100))
        before = self.library.image_bytes
        self.manager.delete(share["id"])
        self.assertStatus(404, self.manager.info, share["token"])
        self.assertStatus(404, self.manager.manifest, share["token"], guest["session"])
        self.assertStatus(404, self.manager.image, share["token"], guest["session"], 0)
        self.assertStatus(404, self.unlock, share)
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions WHERE share_id=?", (share["id"],))[0][0], 0)
        self.assertEqual(self.statement("SELECT COUNT(*) FROM books")[0][0], 6)
        self.assertEqual(self.library.image_bytes, before)
        self.manager.image(other["token"], other_guest["session"], 0)

    def test_resharing_creates_fresh_link_password_and_key_preserving_closed_record(self):
        old = self.manager.create({"book_id": "a"*24})
        old_guest = self.manager.unlock(old["share"]["token"], old["password"])
        self.manager.revoke(old["share"]["id"])
        new = self.manager.create({"book_id": old["share"]["book_id"], "days": 3})
        new_guest = self.manager.unlock(new["share"]["token"], new["password"])
        self.assertNotEqual(new["share"]["token"], old["share"]["token"])
        self.assertNotEqual(new["password"], old["password"])
        self.assertNotEqual(new_guest["key"], old_guest["key"])
        self.assertEqual({item["status"] for item in self.manager.listing()["shares"]}, {"active", "closed"})
        self.assertStatus(401, self.manager.manifest, new["share"]["token"], old_guest["session"])
        self.assertStatus(410, self.manager.manifest, old["share"]["token"], old_guest["session"])

    def test_revocation_during_media_io_does_not_return_encrypted_bytes(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.library.read_hook = lambda: self.manager.revoke(share["id"])
        self.assertStatus(410, self.manager.image, share["token"], guest["session"], 0)

    def test_deletion_during_media_io_does_not_return_encrypted_bytes(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        def delete_while_reading():
            self.manager.revoke(share["id"])
            self.manager.delete(share["id"])
        self.library.read_hook = delete_while_reading
        self.assertStatus(404, self.manager.image, share["token"], guest["session"], 0)

    def test_expiry_during_media_io_does_not_return_encrypted_bytes(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.library.read_hook = lambda: self.statement("UPDATE shares SET expires_at=0 WHERE id=?", (share["id"],))
        self.assertStatus(410, self.manager.image, share["token"], guest["session"], 0)

    def test_removed_unpublished_disabled_owner_or_changed_role_revokes_reading(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        changes = (("books", "active", 0, 1), ("books", "removed", 1, 0),
                   ("books", "published", 0, 1), ("users", "enabled", 0, 1),
                   ("users", "role", "user", "admin"))
        for table, field, changed, reset in changes:
            self.statement(f"UPDATE {table} SET {field}=?", (changed,))
            self.assertStatus(410, self.manager.manifest, share["token"], guest["session"])
            self.assertStatus(410, self.manager.image, share["token"], guest["session"], 0)
            self.statement(f"UPDATE {table} SET {field}=?", (reset,))

    def test_unpublished_changed_during_io_is_rechecked(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.library.read_hook = lambda: self.statement("UPDATE books SET published=0")
        self.assertStatus(410, self.manager.image, share["token"], guest["session"], 0)

    def test_guest_session_cannot_open_another_share_of_same_work(self):
        share = self.create()["share"]
        other = self.create()["share"]
        guest = self.unlock(share)
        self.assertStatus(401, self.manager.manifest, other["token"], guest["session"])
        self.assertStatus(401, self.manager.image, other["token"], guest["session"], 0)
        self.assertStatus(404, self.manager.info, "not-a-token")
        self.assertStatus(404, self.manager.info, secrets.token_urlsafe(32))

    def test_guest_cannot_choose_book_page_outside_work_or_invalid_pdf_width(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        for index in (-1, 3, True, "1"):
            self.assertStatus(404, self.manager.image, share["token"], guest["session"], index)
        for width in (0, 4097, True, "800"):
            self.assertStatus(400, self.manager.image, share["token"], guest["session"], 0, width)
        self.assertStatus(401, self.manager.image, share["token"], "", 0)

    def test_pdf_and_photo_epub_allowed_but_unpublished_work_not_issuable(self):
        share = self.create(book_id="b"*24)["share"]
        self.assertEqual(self.unlock(share)["manifest"]["format"], "pdf")
        archived = self.create(book_id="d"*24)["share"]
        self.assertEqual(self.unlock(archived)["manifest"]["format"], "archive")
        for identifier, format_name in (("e"*24, "zip"), ("f"*24, "cbz")):
            compressed = self.create(book_id=identifier)["share"]
            self.assertEqual(self.unlock(compressed)["manifest"]["format"], format_name)
        photo_epub = self.create(book_id="c"*24)["share"]
        self.assertEqual(self.unlock(photo_epub)["manifest"]["format"], "epub")
        self.statement("UPDATE books SET published=0")
        self.assertStatus(404, self.create)

    def test_ciphertext_not_original_and_every_response_has_new_nonce(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        share = self.create()["share"]
        guest = self.unlock(share)
        with self.library.acting_as(None):
            one, mime = self.manager.image(share["token"], guest["session"], 0)
            two, _ = self.manager.image(share["token"], guest["session"], 0)
            self.assertIsNone(self.library.request_context.user_id)
        self.assertEqual(mime, "application/octet-stream")
        self.assertEqual(one[:4], b"GSH1")
        self.assertNotEqual(one, two)
        self.assertNotIn(self.library.image_bytes, one)
        key = base64.urlsafe_b64decode(guest["key"]+"=")
        aad = ("gravelle-share-v1:"+share["token"]+":0").encode()
        self.assertEqual(AESGCM(key).decrypt(one[4:16], one[17:], one[:17]+aad), self.library.image_bytes)
        with self.assertRaises(Exception):
            AESGCM(key).decrypt(one[4:16], one[17:], one[:17]+aad[:-1]+b"1")

    def test_decoder_failure_restores_context_and_releases_bounded_slot(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        def failure():
            raise SharingError("Decoder failed", 422)
        self.library.read_hook = failure
        with self.library.acting_as(None):
            for _ in range(4):
                self.assertStatus(422, self.manager.image, share["token"], guest["session"], 0)
                self.assertIsNone(self.library.request_context.user_id)
        self.library.read_hook = None
        self.manager.image(share["token"], guest["session"], 0)

    def test_bounded_image_parallelism_rejects_excess_and_shutdown_rechecks_io(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.manager.image_slots.acquire()
        self.manager.image_slots.acquire()
        try:
            self.assertStatus(503, self.manager.image, share["token"], guest["session"], 0)
        finally:
            self.manager.image_slots.release()
            self.manager.image_slots.release()
        self.library.read_hook = self.manager.close
        self.assertStatus(503, self.manager.image, share["token"], guest["session"], 0)
        self.assertStatus(503, self.manager.info, share["token"])

    def test_password_attempts_limited_and_reset_after_window(self):
        share = self.create()["share"]
        with patch("sharing.time.monotonic", return_value=1):
            for _ in range(MAX_ATTEMPTS):
                self.assertStatus(403, self.manager.unlock, share["token"], "wrong", client_ip="test")
            self.assertStatus(429, self.manager.unlock, share["token"], self.password, client_ip="test")
        with patch("sharing.time.monotonic", return_value=1000):
            self.assertIn("session", self.manager.unlock(share["token"], self.password, client_ip="test"))
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 1)

    def test_password_budget_covers_creation_and_unlock_but_not_existing_session(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        self.manager.password_slots.acquire()
        self.manager.password_slots.acquire()
        try:
            self.assertStatus(503, self.create)
            self.assertStatus(503, self.unlock, share)
            # Refreshing a valid capability never needs to hash its password.
            with patch("sharing.check_password", side_effect=AssertionError("Unexpected KDF")):
                reused = self.unlock(share, session=guest["session"])
            self.assertEqual(reused["session"], guest["session"])
            self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 1)
        finally:
            self.manager.password_slots.release()
            self.manager.password_slots.release()
        self.assertIn("share", self.create())
        self.assertIn("session", self.unlock(share))

    def test_password_kdf_exceptions_release_slots_and_success_recovers(self):
        share = self.create()["share"]
        with patch("sharing.hash_password", side_effect=RuntimeError("Generated hashing failure")):
            for _ in range(3):
                with self.assertRaisesRegex(RuntimeError, "Generated hashing failure"):
                    self.create()
        with patch("sharing.check_password", side_effect=RuntimeError("Generated verification failure")):
            for _ in range(3):
                with self.assertRaisesRegex(RuntimeError, "Generated verification failure"):
                    self.unlock(share)
        self.assertIn("share", self.create())
        self.assertIn("session", self.unlock(share))
        # A returned incorrect password also releases the permit.
        with patch("sharing.check_password", return_value=False):
            self.assertStatus(403, self.unlock, share)
        self.assertTrue(self.manager.password_slots.acquire(blocking=False))
        self.assertTrue(self.manager.password_slots.acquire(blocking=False))
        self.assertFalse(self.manager.password_slots.acquire(blocking=False))
        self.manager.password_slots.release()
        self.manager.password_slots.release()

    def test_rate_state_bounded_and_unknown_tokens_do_not_allocate(self):
        share = self.create()["share"]
        with patch("sharing.time.monotonic", return_value=1):
            for index in range(MAX_RATE_KEYS+10):
                self.manager._attempt(share["token"], str(index))
        self.assertEqual(len(self.manager.attempts), MAX_RATE_KEYS)
        before = len(self.manager.attempts)
        self.assertStatus(404, self.manager.unlock, secrets.token_urlsafe(32), self.password)
        self.assertEqual(len(self.manager.attempts), before)

    def test_live_session_budget_and_expired_cleanup(self):
        share = self.create()["share"]
        one = self.unlock(share)
        with patch("sharing.MAX_LIVE_SESSIONS", 1):
            self.assertStatus(429, self.unlock, share)
            self.statement("UPDATE share_sessions SET expires_at=0")
            two = self.unlock(share)
        self.assertNotEqual(one["session"], two["session"])
        self.assertEqual(self.statement("SELECT COUNT(*) FROM share_sessions")[0][0], 1)

    def test_database_reopen_keeps_live_sessions_and_no_guest_account_created(self):
        share = self.create()["share"]
        guest = self.unlock(share)
        reopened = SharingManager(self.library, SharingError)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.manifest(share["token"], guest["session"])["key"], guest["key"])
        self.assertEqual(self.statement("SELECT COUNT(*) FROM users")[0][0], 3)


class RealArchiveSharingTests(unittest.TestCase):
    def test_real_scanner_zip_and_cbz_formats_are_shareable(self):
        from server import Library
        root = Path(__file__).resolve().parents[1] / "work" / "sharing-tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as scratch:
            media = Path(scratch) / "media"
            media.mkdir()
            image = BytesIO()
            Image.new("RGB", (30, 40), "#9944ee").save(image, "PNG")
            for suffix in ("zip", "cbz"):
                with zipfile.ZipFile(media / ("Generated."+suffix), "w") as archive:
                    archive.writestr("1.png", image.getvalue())
            library = Library(media, Path(scratch) / "data", "", 0)
            try:
                library.transcoding.auto_start = library.image_search.auto_start = False
                library.register({"username":"generated-admin", "password":"generated-admin-password"}, "fixture")
                library.save_library({"name":"Generated archive library", "path":str(media), "media_type":"photo"})
                deadline = time.monotonic()+20
                while library.scanning and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertFalse(library.scanning)
                books = library.listing()["books"]
                self.assertEqual({book["format"] for book in books}, {"zip", "cbz"})
                for book in books:
                    original = hashlib.sha256((media / ("Generated."+book["format"])).read_bytes()).digest()
                    created = library.sharing.create({"book_id":book["id"], "password":"generated-sharing-password"})
                    guest = library.sharing.unlock(created["share"]["token"], created["password"])
                    packet, mime = library.sharing.image(created["share"]["token"], guest["session"], 0)
                    self.assertEqual(mime, "application/octet-stream")
                    self.assertEqual(packet[:4], b"GSH1")
                    library.sharing.revoke(created["share"]["id"])
                    library.sharing.delete(created["share"]["id"])
                    self.assertEqual(library.book(book["id"])["page_count"], 1)
                    self.assertEqual(hashlib.sha256((media / ("Generated."+book["format"])).read_bytes()).digest(), original)
            finally:
                library.close()


class SharePasswordVaultTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "work" / "sharing-vault-tests"
        root.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.data = self.root / "data"
        self.data.mkdir()

    def test_atomic_concurrent_creation_keeps_one_complete_key_and_no_temporary_files(self):
        def create(_):
            return SharePasswordVault(self.data, allow_create=True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            vaults = list(pool.map(create, range(6)))
        key = (self.data / KEY_NAME).read_bytes()
        self.assertEqual(len(key), 32)
        self.assertTrue(all(vault.key() == key for vault in vaults))
        self.assertEqual([path.name for path in self.data.iterdir()], [KEY_NAME])
        if os.name == "posix":
            self.assertEqual((self.data / KEY_NAME).stat().st_mode & 0o077, 0)

    def test_vault_does_not_overwrite_invalid_existing_key(self):
        path = self.data / KEY_NAME
        path.write_bytes(b"invalid-key")
        vault = SharePasswordVault(self.data, allow_create=True)
        self.assertIsNone(vault.key())
        self.assertEqual(path.read_bytes(), b"invalid-key")

    def test_key_symlink_is_not_read_or_replaced(self):
        outside = self.root / "outside.key"
        value = secrets.token_bytes(32)
        outside.write_bytes(value)
        target = self.data / KEY_NAME
        try:
            target.symlink_to(outside)
        except OSError as error:
            self.skipTest("Symbolic links require local permissions: " + str(error))
        vault = SharePasswordVault(self.data, allow_create=True)
        self.assertIsNone(vault.key())
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_bytes(), value)

    def test_key_hardlink_is_not_read_or_replaced(self):
        outside = self.root / "outside.key"
        value = secrets.token_bytes(32)
        outside.write_bytes(value)
        target = self.data / KEY_NAME
        try:
            os.link(outside, target)
        except OSError as error:
            self.skipTest("Hard links are unavailable: " + str(error))
        vault = SharePasswordVault(self.data, allow_create=True)
        self.assertIsNone(vault.key())
        self.assertEqual(target.read_bytes(), value)
        self.assertEqual(outside.read_bytes(), value)

    def test_data_directory_symlink_does_not_create_a_key_at_target(self):
        alias = self.root / "data-alias"
        try:
            alias.symlink_to(self.data, target_is_directory=True)
        except OSError as error:
            self.skipTest("Directory symbolic links require local permissions: " + str(error))
        vault = SharePasswordVault(alias, allow_create=True)
        self.assertIsNone(vault.key())
        self.assertFalse((self.data / KEY_NAME).exists())


if __name__ == "__main__":
    unittest.main()
