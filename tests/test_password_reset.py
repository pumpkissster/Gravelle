"""Offline recovery only operates on disposable databases in these tests."""
from contextlib import closing
import getpass
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from accounts import check_password, hash_password
from password_reset import DataDirectoryLock, PasswordResetError, main, reset_admin_password


class PasswordResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_hash = hash_password("Original-password-123")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "资料"
        self.data.mkdir()
        self.database = self.data / "library.sqlite3"

    def fixture(self, modern=False, admin=True):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript("""
                CREATE TABLE owner_account (
                    id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,updated_at REAL,
                    avatar_file TEXT,display_name TEXT,created_at REAL);
                CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER,expires_at REAL);
                CREATE TABLE books(id TEXT PRIMARY KEY,title TEXT,path TEXT);
                CREATE TABLE book_metadata(book_id TEXT PRIMARY KEY,title TEXT,cover_page INTEGER);
                CREATE TABLE progress(book_id TEXT,user_id INTEGER,page INTEGER);
                CREATE TABLE bookmarks(id TEXT,user_id INTEGER,note TEXT);
                CREATE TABLE favorites(book_id TEXT,user_id INTEGER,favorite INTEGER);
                INSERT INTO books VALUES('book','写真','/original/folder');
                INSERT INTO book_metadata VALUES('book','手工名称',4);
                INSERT INTO progress VALUES('book',1,8),('book',2,19);
                INSERT INTO bookmarks VALUES('mark',1,'保留笔记');
                INSERT INTO favorites VALUES('book',1,1);
                INSERT INTO auth_sessions VALUES('admin-token',1,99999999999),
                    ('member-token',2,99999999999),('setup-token',NULL,99999999999);
            """)
            if admin:
                connection.execute("INSERT INTO owner_account VALUES(1,'admin',?,17,'photo.png','保留显示名',12)",
                                   (self.original_hash,))
            if modern:
                connection.execute("""CREATE TABLE users (
                    id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,updated_at REAL,
                    avatar_file TEXT,display_name TEXT,created_at REAL,
                    role TEXT,enabled INTEGER,permissions TEXT)""")
                if admin:
                    connection.execute("INSERT INTO users VALUES(1,'admin',?,17,'photo.png','保留显示名',12,'admin',1,'{}')",
                                       (self.original_hash,))
                connection.execute("INSERT INTO users VALUES(2,'member',?,18,'member.png','成员',13,'user',1,?)",
                                   (self.original_hash, json.dumps({"edit_metadata": True})))

    def snapshot(self, path=None):
        with closing(sqlite3.connect(path or self.database)) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            return {name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for (name,) in tables}

    def test_legacy_reset_preserves_all_metadata_and_backup_has_original_account_and_sessions(self):
        self.fixture()
        original = self.snapshot()
        result = reset_admin_password(self.data, "New-password-123", "New-password-123")
        after = self.snapshot()
        self.assertEqual(self.snapshot(Path(result["backup"])), original)
        for name in ("books", "book_metadata", "progress", "bookmarks", "favorites"):
            self.assertEqual(after[name], original[name])
        with closing(sqlite3.connect(self.database)) as connection:
            account = connection.execute("SELECT * FROM owner_account WHERE id=1").fetchone()
            self.assertTrue(check_password("New-password-123", account[2]))
            self.assertFalse(check_password("Original-password-123", account[2]))
            self.assertGreater(account[3], 17)
            self.assertEqual(account[4:], original["owner_account"][0][4:])
            self.assertEqual(connection.execute("SELECT token_hash FROM auth_sessions ORDER BY token_hash").fetchall(),
                             [("member-token",), ("setup-token",)])
            self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone())
        self.assertEqual(result["username"], "admin")

    def test_modern_reset_updates_only_root_admin_and_owner_mirror(self):
        self.fixture(modern=True)
        original = self.snapshot()
        result = reset_admin_password(self.data, "New-password-123", "New-password-123")
        after = self.snapshot()
        self.assertEqual(self.snapshot(Path(result["backup"])), original)
        self.assertEqual(after["users"][1], original["users"][1])
        self.assertEqual(after["users"][0][4:], original["users"][0][4:])
        self.assertEqual(after["users"][0][2:4], after["owner_account"][0][2:4])
        self.assertTrue(check_password("New-password-123", after["users"][0][2]))

    def test_invalid_or_mismatched_password_does_not_touch_database_or_create_backup(self):
        self.fixture()
        original = self.database.read_bytes()
        for password, confirmation in (("short", "short"), ("x" * 257, "x" * 257),
                                       ("valid-password", "other-password"), ("bad\ud800password", "bad\ud800password")):
            with self.subTest(length=len(password)), self.assertRaises(PasswordResetError):
                reset_admin_password(self.data, password, confirmation)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse((self.data / "backups").exists())

    def test_missing_database_or_directory_is_never_created(self):
        for data in (self.data, self.data / "does-not-exist"):
            with self.subTest(data=data), self.assertRaisesRegex(PasswordResetError, "资料库不存在"):
                reset_admin_password(data, "valid-password", "valid-password")
            self.assertFalse((data / "library.sqlite3").exists())
        self.assertFalse((self.data / "does-not-exist").exists())

    def test_missing_admin_is_not_registered_and_not_backed_up(self):
        for modern in (False, True):
            with self.subTest(modern=modern):
                self.database.unlink(missing_ok=True)
                self.fixture(modern=modern, admin=False)
                before = self.snapshot()
                with self.assertRaisesRegex(PasswordResetError, "没有主管理员"):
                    reset_admin_password(self.data, "valid-password", "valid-password")
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.data / "backups").exists())

    def test_users_table_never_falls_back_to_legacy_account_when_root_role_invalid(self):
        self.fixture(modern=True)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE users SET role='user' WHERE id=1")
        original = self.snapshot()
        with self.assertRaisesRegex(PasswordResetError, "没有主管理员"):
            reset_admin_password(self.data, "valid-password", "valid-password")
        self.assertEqual(self.snapshot(), original)

    def test_failed_backup_cannot_change_password_or_sessions(self):
        self.fixture(modern=True)
        original = self.snapshot()
        with patch("password_reset._backup", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(PasswordResetError, "disk full"):
            reset_admin_password(self.data, "valid-password", "valid-password")
        self.assertEqual(self.snapshot(), original)
        # Exception cleanup releases both SQLite and the process lock.
        with DataDirectoryLock(self.data), closing(sqlite3.connect(self.database)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()

    def test_failed_commit_rolls_back_password_but_retains_completed_backup(self):
        self.fixture(modern=True)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TRIGGER prevent_session_delete BEFORE DELETE ON auth_sessions BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        original = self.snapshot()
        with self.assertRaisesRegex(PasswordResetError, "test rollback"):
            reset_admin_password(self.data, "valid-password", "valid-password")
        self.assertEqual(self.snapshot(), original)
        backups = list((self.data / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.snapshot(backups[0]), original)

    def test_existing_database_is_opened_with_mode_rw(self):
        self.fixture()
        original = sqlite3.connect
        connections = []
        def track(database, *args, **kwargs):
            connections.append((database, kwargs.get("uri")))
            return original(database, *args, **kwargs)
        with patch("password_reset.sqlite3.connect", side_effect=track):
            reset_admin_password(self.data, "valid-password", "valid-password")
        self.assertTrue(connections)
        self.assertTrue(all(uri and name.endswith("?mode=rw") for name, uri in connections))

    def test_successive_backups_are_unique_and_complete(self):
        self.fixture()
        first = reset_admin_password(self.data, "First-password", "First-password")
        second = reset_admin_password(self.data, "Second-password", "Second-password")
        self.assertNotEqual(first["backup"], second["backup"])
        self.assertEqual(len(list((self.data / "backups").glob("*.sqlite3"))), 2)
        with closing(sqlite3.connect(second["backup"])) as connection:
            encoded = connection.execute("SELECT password_hash FROM owner_account WHERE id=1").fetchone()[0]
        self.assertTrue(check_password("First-password", encoded))

    def test_cli_hides_both_prompts_and_never_prints_password(self):
        self.fixture()
        output = io.StringIO()
        with patch("password_reset.getpass.getpass", side_effect=["CLI-password-123"] * 2) as prompt, \
                patch("sys.stdout", output):
            self.assertEqual(main(["--data", str(self.data)]), 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertNotIn("CLI-password-123", output.getvalue())
        self.assertIn("重新登录", output.getvalue())

    def test_cli_rejects_getpass_echo_fallback_and_cleartext_argument(self):
        self.fixture()
        original = self.snapshot()
        with patch("password_reset.getpass.getpass", side_effect=getpass.GetPassWarning("echo")), \
                patch("sys.stderr", io.StringIO()) as output:
            self.assertEqual(main(["--data", str(self.data)]), 1)
        self.assertIn("交互终端", output.getvalue())
        with patch("password_reset.getpass.getpass") as prompt, patch("sys.stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as error:
            main(["--data", str(self.data), "--password", "not-accepted"])
        self.assertEqual(error.exception.code, 2)
        prompt.assert_not_called()
        self.assertEqual(self.snapshot(), original)


class DataDirectoryLockTests(unittest.TestCase):
    def test_os_lock_multiple_instances_release_twice_and_context_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            first, second = DataDirectoryLock(data), DataDirectoryLock(data)
            self.addCleanup(first.release)
            self.addCleanup(second.release)
            self.assertTrue(first.acquire())
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            with self.assertRaisesRegex(PasswordResetError, "先停止服务"):
                with second:
                    self.fail("a held directory lock must refuse entry")
            first.release()
            first.release()
            self.assertTrue(second.acquire())
            second.release()
            self.assertTrue(first.path.is_file())
            with first:
                self.assertFalse(second.acquire())
            with second:
                pass

    def test_other_process_and_crash_release_lock_without_deleting_file(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            script = "from pathlib import Path; import sys; from password_reset import DataDirectoryLock; lock=DataDirectoryLock(Path(sys.argv[1])); assert lock.acquire(); print('locked',flush=True); sys.stdin.read()"
            process = subprocess.Popen([sys.executable, "-u", "-c", script, str(data)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, cwd=Path(__file__).resolve().parents[1])
            self.addCleanup(lambda: process.poll() is None and process.kill())
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(PasswordResetError, "先停止服务"):
                    with DataDirectoryLock(data):
                        self.fail("must not acquire a lock held by another process")
                process.kill()
                process.wait(timeout=5)
                with DataDirectoryLock(data):
                    pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


class ServiceLockIntegrationTests(unittest.TestCase):
    def test_live_library_rejects_reset_and_second_library_then_close_releases(self):
        from server import Library
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            first = Library(root, data, "", 0)
            try:
                first.register({"username": "admin", "password": "Original-password-123"}, "127.0.0.1")
                with self.assertRaisesRegex(PasswordResetError, "先停止服务"):
                    reset_admin_password(data, "valid-password", "valid-password")
                with self.assertRaisesRegex(RuntimeError, "正在使用"):
                    Library(root, data, "", 0)
            finally:
                first.close()
                first.close()
            result = reset_admin_password(data, "valid-password", "valid-password")
            self.assertTrue(Path(result["backup"]).is_file())
            reopened = Library(root, data, "", 0)
            reopened.close()

    def test_failed_library_initialization_releases_directory_lock(self):
        from server import Library
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            with patch.object(Library, "_initialize_runtime", side_effect=RuntimeError("fixture error")), \
                    self.assertRaisesRegex(RuntimeError, "fixture error"):
                Library(root, data, "", 0)
            with DataDirectoryLock(data):
                pass

    def test_controller_reset_stops_its_live_child_and_preserves_root_profile(self):
        from desktop.core import LauncherConfig
        from desktop.runtime import ServerController
        import socket
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photos = root / "photos"
            photos.mkdir()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            controller = ServerController(root / "data", root / "server.log")
            try:
                controller.start(LauncherConfig(str(photos), port))
                with closing(sqlite3.connect(controller.data_path / "library.sqlite3")) as connection, connection:
                    encoded = hash_password("Original-password-123")
                    connection.execute("""INSERT INTO users (id,username,normalized_username,display_name,
                        password_hash,created_at,updated_at,role) VALUES(1,'admin','admin','原资料',?,1,1,'admin')""", (encoded,))
                    connection.execute("INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES('fixture-token',1,1,99999999999)")
                child = controller.process
                result = controller.reset_admin_password("New-password-123", "New-password-123")
                self.assertFalse(controller.running)
                self.assertEqual(child.returncode, 0)
                self.assertTrue(Path(result["backup"]).is_file())
                with closing(sqlite3.connect(controller.data_path / "library.sqlite3")) as connection:
                    account = connection.execute("SELECT display_name,password_hash FROM users WHERE id=1").fetchone()
                    self.assertEqual(account[0], "原资料")
                    self.assertTrue(check_password("New-password-123", account[1]))
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_sessions WHERE user_id=1").fetchone()[0], 0)
            finally:
                controller.stop()


class LauncherResetTests(unittest.TestCase):
    def test_console_entry_uses_shared_cli_without_tk_or_library_initialization(self):
        from desktop.launcher import main as launcher_main
        with tempfile.TemporaryDirectory() as folder, patch("desktop.launcher._restore_child_stdio"), \
                patch("password_reset.main", return_value=0) as reset_cli:
            self.assertEqual(launcher_main(["--reset-admin-password", "--data", folder]), 0)
        reset_cli.assert_called_once_with(["--data", folder])

    def test_console_entry_requires_explicit_data_and_rejects_plaintext_password(self):
        from desktop.launcher import main as launcher_main
        for arguments in (["--reset-admin-password"], ["--reset-admin-password", "--data", "unused", "--password", "unused"]):
            with self.subTest(arguments=arguments), patch("password_reset.main") as reset_cli, \
                    patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                launcher_main(arguments)
            self.assertEqual(error.exception.code, 2)
            reset_cli.assert_not_called()

    def test_controller_validates_before_stop_and_stops_only_its_owned_handle(self):
        from desktop.core import LauncherError
        from desktop.runtime import ServerController
        controller = ServerController(Path("unused"), Path("unused.log"))
        actions = []
        controller.stop = Mock(side_effect=lambda: actions.append("owned-stop"))
        with self.assertRaises(LauncherError):
            controller.reset_admin_password("short", "short")
        controller.stop.assert_not_called()
        with patch("password_reset.reset_admin_password", side_effect=lambda *args: actions.append("reset") or {"backup": "fixture"}):
            self.assertEqual(controller.reset_admin_password("valid-password", "valid-password"), {"backup": "fixture"})
        self.assertEqual(actions, ["owned-stop", "reset"])

    def test_controller_reports_external_service_lock_without_terminating_external_process(self):
        from desktop.core import LauncherError
        from desktop.runtime import ServerController
        with tempfile.TemporaryDirectory() as folder:
            controller = ServerController(Path(folder), Path(folder) / "server.log")
            controller.stop = Mock()
            with patch("password_reset.reset_admin_password", side_effect=PasswordResetError("请先停止服务")), \
                    self.assertRaisesRegex(LauncherError, "先停止服务"):
                controller.reset_admin_password("valid-password", "valid-password")
            controller.stop.assert_called_once_with()
            self.assertIsNone(controller.process)


class ResetDialogGeometryTests(unittest.TestCase):
    def test_fields_footer_actions_and_close_are_reachable_on_small_and_4k_displays(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("Tk is unavailable on this test host")
        from desktop.core import SingleInstanceLock
        from desktop.launcher import LauncherWindow
        from types import SimpleNamespace
        for screen_width, screen_height, scale in ((520, 480, 1), (3840, 2160, 2)):
            with self.subTest(screen=(screen_width, screen_height), scale=scale), tempfile.TemporaryDirectory() as folder:
                try:
                    root = tk.Tk()
                except tk.TclError as error:
                    self.skipTest("Tk has no display: " + str(error))
                try:
                    root.tk.call("tk", "scaling", scale * 96 / 72)
                    root.winfo_screenwidth = lambda: screen_width
                    root.winfo_screenheight = lambda: screen_height
                    storage = Path(folder)
                    with patch.object(LauncherWindow, "_worker"), \
                            patch.dict("sys.modules", {"server": SimpleNamespace(VERSION="test")}):
                        window = LauncherWindow(root, storage / "launcher.json", storage, SingleInstanceLock(storage / "launcher.lock"))
                    root.update()
                    dialog = window.reset_password()
                    root.update()
                    def descendants(widget):
                        for child in widget.winfo_children():
                            yield child
                            yield from descendants(child)
                    entries = [widget for widget in descendants(dialog) if widget.winfo_class() == "TEntry"]
                    actions = [widget for widget in descendants(dialog) if widget.winfo_class() == "TButton"]
                    self.assertEqual(len(entries), 2)
                    self.assertEqual({widget.cget("text") for widget in actions}, {"取消", "保存新密码"})
                    for widget in entries + actions:
                        self.assertTrue(widget.winfo_ismapped())
                        x = widget.winfo_rootx() - dialog.winfo_rootx()
                        y = widget.winfo_rooty() - dialog.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + widget.winfo_width(), dialog.winfo_width())
                        self.assertLessEqual(y + widget.winfo_height(), dialog.winfo_height())
                    self.assertTrue(all(entry.cget("show") for entry in entries))
                    self.assertLessEqual(dialog.winfo_height(), screen_height)
                    self.assertLessEqual(dialog.winfo_width(), screen_width)
                    # Check the title-bar close callback inside this Tk app;
                    # never inspect or automate another native application.
                    close_command = dialog.protocol("WM_DELETE_WINDOW")
                    self.assertTrue(close_command)
                    dialog.tk.call(close_command)
                    self.assertFalse(dialog.winfo_exists())
                    self.assertTrue(root.winfo_exists())
                finally:
                    for callback in root.tk.call("after", "info"):
                        root.after_cancel(callback)
                    root.destroy()


if __name__ == "__main__":
    unittest.main()
