"""Verify TAIP module imports follow the documented dependency direction."""

import ast
import unittest
from pathlib import Path


ALLOWED_DEPENDENCIES = {
    "common": {"common"},
    "problem_management": {"common", "problem_management"},
    "ai_integration": {"common", "ai_integration"},
    "mathematical_verification": {"common", "mathematical_verification"},
    "orchestration": {
        "common",
        "ai_integration",
        "mathematical_verification",
        "orchestration",
    },
}


class ModuleBoundaryTests(unittest.TestCase):
    def test_imports_respect_architecture_and_have_no_module_cycles(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        source_root = repository_root / "src" / "taip"
        dependencies: dict[str, set[str]] = {
            module: set() for module in ALLOWED_DEPENDENCIES
        }

        for path in source_root.rglob("*.py"):
            relative_path = path.relative_to(source_root)
            module_name = relative_path.parts[0]
            if module_name == "benchmark":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules = [node.module]
                else:
                    continue
                for imported_module in imported_modules:
                    if not imported_module.startswith("taip."):
                        continue
                    dependency = imported_module.split(".")[1]
                    self.assertIn(
                        dependency,
                        ALLOWED_DEPENDENCIES[module_name],
                        f"{path.relative_to(repository_root)} imports disallowed "
                        f"module {dependency}",
                    )
                    dependencies[module_name].add(dependency)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(module: str) -> None:
            if module in visiting:
                self.fail(f"Circular module dependency detected at {module}")
            if module in visited:
                return
            visiting.add(module)
            for dependency in dependencies[module] - {module}:
                visit(dependency)
            visiting.remove(module)
            visited.add(module)

        for module in dependencies:
            visit(module)


if __name__ == "__main__":
    unittest.main()
