"""EPUB distribution checks; these do not create a native release."""
from __future__ import annotations

import ast
from pathlib import Path
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EPUBPackagingTests(unittest.TestCase):
    def test_source_export_includes_epub_and_excludes_private_documents(self):
        exporter = runpy.run_path(str(ROOT / 'packaging' / 'package_source.py'))
        names = exporter['source_files']()
        expected = {
            'epub_reader.py', 'static/epub-reader.js', 'static/epub-reader.css',
            'tests/test_epub_reader.py', 'tests/test_epub_reader_ui.js',
            'tests/test_epub_api.py', 'tests/test_epub_packaging.py', 'docs/epub.md',
        }
        self.assertTrue(expected <= set(names), expected - set(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertFalse({'docs/next-version.md', 'VERIFICATION.md'} & set(names))

    def test_docker_context_contains_engine_and_browser_resources(self):
        checker = runpy.run_path(str(ROOT / 'packaging' / 'check_docker_context.py'))
        result = checker['validate'](ROOT)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['private_data_excluded'])
        self.assertIn('epub_reader.py', result['copy_sources'])
        self.assertTrue({'epub_reader.py', 'static/epub-reader.js',
                         'static/epub-reader.css'} <= set(result['files']))

    def test_both_native_specs_include_engine_and_static_tree(self):
        for name in ('gravelle.spec', 'gravelle-macos.spec'):
            with self.subTest(spec=name):
                tree = ast.parse((ROOT / 'desktop' / name).read_text(encoding='utf-8'))
                analyses = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id == 'Analysis']
                self.assertEqual(len(analyses), 1)
                options = {keyword.arg: keyword.value for keyword in analyses[0].keywords}
                self.assertIn('epub_reader', ast.literal_eval(options['hiddenimports']))
                data_expression = options['datas']
                data_list = data_expression.left if isinstance(data_expression, ast.BinOp) else data_expression
                destinations = [ast.literal_eval(entry.elts[1]) for entry in data_list.elts]
                self.assertIn('static', destinations)


if __name__ == '__main__':
    unittest.main()
