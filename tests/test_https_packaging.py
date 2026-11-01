"""Keep native TLS and PKCS#12 support in source and frozen distributions."""
import ast
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HTTPSPackagingTests(unittest.TestCase):
    def test_source_and_restricted_docker_context_contain_tls_loader(self):
        source = set(runpy.run_path(str(ROOT / 'packaging/package_source.py'))['source_files']())
        self.assertTrue({'https_settings.py', 'network_settings.py', 'static/network-settings-ui.js',
                         'tests/test_https_packaging.py'} <= source)
        self.assertFalse({'VERIFICATION.md', 'docs/next-version.md', 'https-password.key'} & source)
        context = runpy.run_path(str(ROOT / 'packaging/check_docker_context.py'))['validate'](ROOT)
        self.assertEqual(context['status'], 'passed')
        self.assertIn('https_settings.py', context['files'])
        self.assertFalse(any(name.endswith(('.pfx', '.p12', '.key')) for name in context['files']))

    def test_both_frozen_targets_collect_tls_and_pkcs12(self):
        for name in ('gravelle.spec', 'gravelle-macos.spec'):
            with self.subTest(target=name):
                tree = ast.parse((ROOT / 'desktop' / name).read_text(encoding='utf-8'))
                call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id == 'Analysis')
                imports = ast.literal_eval(next(item.value for item in call.keywords if item.arg == 'hiddenimports'))
                self.assertTrue({'https_settings', 'ssl',
                                 'cryptography.hazmat.primitives.serialization.pkcs12'} <= set(imports))


if __name__ == '__main__':
    unittest.main()
