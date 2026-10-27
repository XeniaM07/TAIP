"""Ensure production methods remain declarations for Lab 04."""

import ast
import unittest
from pathlib import Path


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


class DeclarationOnlyTests(unittest.TestCase):
    def test_non_benchmark_production_methods_have_stub_bodies(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        source_root = repository_root / "src" / "taip"
        production_files = (
            path
            for path in source_root.rglob("*.py")
            if "benchmark" not in path.relative_to(source_root).parts
        )

        for path in production_files:
            with self.subTest(file=path.relative_to(repository_root)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    body = node.body[1:] if node.body and _is_docstring(node.body[0]) else node.body
                    self.assertEqual(len(body), 1, f"{node.name} must be a stub")
                    self.assertIsInstance(body[0], ast.Expr)
                    self.assertIsInstance(body[0].value, ast.Constant)
                    self.assertIs(body[0].value.value, Ellipsis)


if __name__ == "__main__":
    unittest.main()
