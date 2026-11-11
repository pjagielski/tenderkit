"""Check the standalone package's import boundaries."""
import ast
from pathlib import Path
import sys
import unittest

SRC = Path(__file__).resolve().parents[2]/'src'/'tenderkit'


def imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module


class BoundaryTests(unittest.TestCase):
    def test_tenderkit_never_imports_pharma(self):
        found = {f'{p.relative_to(SRC.parent)}: {m}' for p in SRC.rglob('*.py') for m in imports(p)
                 if m.split('.')[0] == 'pharma'}
        self.assertEqual(found, set(), 'pharma uses tenderkit, never the other way round')

    def test_tenderkit_needs_only_the_standard_library(self):
        found = {f'{p.relative_to(SRC.parent)}: {m}' for p in SRC.rglob('*.py') for m in imports(p)
                 if m.split('.')[0] not in sys.stdlib_module_names | {'tenderkit'}
                 and not (p == SRC/'mcp.py' and m.split('.')[0] == 'mcp')}
        self.assertEqual(found, set(), 'a third-party package belongs in tenderkit only with a dependency declared for it')


if __name__ == '__main__':
    unittest.main()
