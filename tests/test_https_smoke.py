"""Safety guards for the disposable HTTPS release smoke."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('gravelle_https_release_smoke', ROOT / 'packaging/smoke_https.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class HTTPSReleaseSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_container_origin_rejects_remote_targets_credentials_paths_and_redirect_tricks(self):
        self.assertEqual(smoke.loopback_origin('http://127.0.0.1:8080/'), 'http://127.0.0.1:8080')
        for value in ('http://example.com:8080', 'http://192.0.2.1:8080', 'http://localhost:8080',
                      'https://127.0.0.1:8443', 'http://u:p@127.0.0.1:8080', 'http://127.0.0.1:8080/path',
                      'http://127.0.0.1:8080?next=remote', 'http://127.0.0.1:8080/#fragment',
                      'http://127.0.0.1:8080@remote.example', 'http://127.0.0.1:0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                smoke.loopback_origin(value)

    def test_generated_library_refuses_existing_files_and_detects_later_changes(self):
        folder = smoke.prepare_library(self.root / 'new')
        original = smoke.fixture_digest(folder)
        with self.assertRaises(RuntimeError):
            smoke.prepare_library(folder)
        self.assertEqual(smoke.fixture_digest(folder), original)
        (folder / smoke.IMAGE).write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            smoke.fixture_digest(folder)

    def test_empty_database_guard_refuses_previous_accounts_and_network_configuration(self):
        database = self.root / 'library.sqlite3'
        with closing(sqlite3.connect(database)) as connection, connection:
            for name in ('users', 'owner_account', 'media_libraries', 'books', 'auth_sessions'):
                connection.execute(f'CREATE TABLE {name}(value TEXT)')
            connection.execute('CREATE TABLE network_settings(id INTEGER,settings TEXT)')
            network = {'connection_mode': 'http', 'security_entry_enabled': False, 'security_entry_code': '',
                       'share_port_enabled': False, 'public_share_url': '', 'certificate_path': '',
                       'certificate_password_ciphertext': ''}
            connection.execute('INSERT INTO network_settings VALUES(1,?)', (json.dumps(network),))
        smoke.empty_database(self.root)
        for table in ('users', 'owner_account', 'media_libraries', 'books', 'auth_sessions'):
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(f'INSERT INTO {table} VALUES(?)', ('old-data',))
            with self.subTest(table=table), self.assertRaises(RuntimeError):
                smoke.empty_database(self.root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(f'DELETE FROM {table}')
        network['share_port_enabled'] = True
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('UPDATE network_settings SET settings=?', (json.dumps(network),))
        with self.assertRaises(RuntimeError):
            smoke.empty_database(self.root)

    def test_container_mode_requires_explicit_confirmation_before_network_access(self):
        from types import SimpleNamespace
        arguments = SimpleNamespace(executable=None, disposable_url='http://127.0.0.1:8080',
                                    confirm_disposable=False, data=None, library=None,
                                    work_root=self.root / 'work', report=self.root / 'report.json')
        report = smoke.run(arguments)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['checks'], [])
        self.assertIn('--confirm-disposable', report['error'])


if __name__ == '__main__':
    unittest.main()
