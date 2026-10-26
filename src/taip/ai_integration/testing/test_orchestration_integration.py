"""Lab 04 integration contracts, without constructing or dispatching adapters."""

import ast
import inspect
import typing
import unittest
from importlib import import_module
from importlib.util import resolve_name
from pathlib import Path

from taip.ai_integration import adapters, creators
from taip.common.contracts import (
    ExecutionRun,
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
    VerificationResult,
)
from taip.orchestration import execution, facade, states


def production_sources() -> dict[str, ast.Module]:
    """Read production declarations, excluding module-owned test packages."""
    package_root = Path(adapters.__file__).parents[1]
    sources = {}
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        if "testing" in relative.parts:
            continue
        parts = relative.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(("taip", *parts))
        sources[name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sources


def is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


class OrchestrationIntegrationTests(unittest.TestCase):
    def assert_signature(self, operation, parameters, hints) -> None:
        signature = inspect.signature(operation)
        self.assertEqual(tuple(signature.parameters), parameters)
        for parameter in signature.parameters.values():
            self.assertIs(parameter.kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            self.assertIs(parameter.default, inspect.Parameter.empty)
        self.assertEqual(typing.get_type_hints(operation), hints)
        self.assertFalse(inspect.iscoroutinefunction(operation))
        self.assertFalse(inspect.isasyncgenfunction(operation))

    def test_coordinator_declares_three_canonical_creator_dependencies(self) -> None:
        self.assertEqual(
            typing.get_type_hints(facade.ModelExecutionCoordinator),
            {
                "adapter_creators": tuple[
                    creators.ModelAdapterCreator,
                    creators.ModelAdapterCreator,
                    creators.ModelAdapterCreator,
                ],
            },
            "Coordinator must declare exactly three canonical creator dependencies",
        )
        self.assertIs(facade.ModelAdapterCreator, creators.ModelAdapterCreator)

    def test_create_adapters_declares_three_canonical_products_without_execution(self) -> None:
        operation = facade.ModelExecutionCoordinator.__dict__.get("create_adapters")
        self.assertTrue(
            inspect.isfunction(operation),
            "ModelExecutionCoordinator must declare create_adapters(self)",
        )
        self.assert_signature(
            operation,
            ("self",),
            {
                "return": tuple[
                    adapters.AIModelAdapter,
                    adapters.AIModelAdapter,
                    adapters.AIModelAdapter,
                ],
            },
        )
        self.assertTrue(inspect.getdoc(operation), "New operation needs a descriptive docstring")
        self.assertIs(facade.AIModelAdapter, adapters.AIModelAdapter)
        self.assertEqual(
            {
                name
                for name, member in facade.ModelExecutionCoordinator.__dict__.items()
                if inspect.isfunction(member)
            },
            {"create_adapters", "execute"},
        )

    def test_existing_operations_preserve_shared_dtos_and_public_context(self) -> None:
        contracts = (
            (
                facade.ModelExecutionCoordinator.execute,
                ("self", "execution_run"),
                {
                    "execution_run": ExecutionRun,
                    "return": tuple[ModelResponse, ModelResponse, ModelResponse],
                },
            ),
            (
                facade.ProblemOrchestrator.execute,
                ("self", "confirmed_snapshot"),
                {"confirmed_snapshot": ProblemSnapshot, "return": execution.ExecutionRunContext},
            ),
            (
                facade.ExecutionFacade.start_execution,
                ("self", "confirmed_snapshot"),
                {"confirmed_snapshot": ProblemSnapshot, "return": execution.ExecutionRunContext},
            ),
            (
                facade.ExecutionFacade.get_results,
                ("self", "execution_run"),
                {"execution_run": execution.ExecutionRunContext, "return": list[ModelResponse]},
            ),
            (
                facade.ExecutionFacade.get_verification_results,
                ("self", "execution_run"),
                {
                    "execution_run": execution.ExecutionRunContext,
                    "return": list[VerificationResult],
                },
            ),
            (
                execution.ExecutionResultCollector.collect,
                ("self", "execution_run"),
                {"execution_run": execution.ExecutionRunContext, "return": list[ModelResponse]},
            ),
            (
                execution.VerificationCoordinator.verify,
                ("self", "execution_run", "responses"),
                {
                    "execution_run": execution.ExecutionRunContext,
                    "responses": list[ModelResponse],
                    "return": list[VerificationResult],
                },
            ),
        )
        for operation, parameters, hints in contracts:
            with self.subTest(operation=operation.__qualname__):
                self.assert_signature(operation, parameters, hints)

    def test_facade_and_run_composition_preserve_original_public_types(self) -> None:
        compositions = (
            (
                facade.ExecutionFacade,
                {
                    "problem_orchestrator": facade.ProblemOrchestrator,
                    "result_collector": execution.ExecutionResultCollector,
                    "verification_coordinator": execution.VerificationCoordinator,
                },
            ),
            (
                facade.ProblemOrchestrator,
                {"model_execution_coordinator": facade.ModelExecutionCoordinator},
            ),
            (
                execution.ModelExecution,
                {
                    "execution_run": ExecutionRun,
                    "model_id": str,
                    "status": ModelStatus,
                    "response": ModelResponse | None,
                },
            ),
            (
                execution.ExecutionRunContext,
                {
                    "run": ExecutionRun,
                    "current_state": states.ExecutionState,
                    "model_executions": tuple[
                        execution.ModelExecution,
                        execution.ModelExecution,
                        execution.ModelExecution,
                    ],
                },
            ),
        )
        for owner, hints in compositions:
            with self.subTest(owner=owner.__name__):
                self.assertEqual(typing.get_type_hints(owner), hints)
        for name in ("start", "complete", "fail"):
            with self.subTest(context_operation=name):
                self.assert_signature(
                    getattr(execution.ExecutionRunContext, name), ("self",), {"return": type(None)}
                )

    def test_ai_interfaces_have_one_canonical_production_definition(self) -> None:
        self.assertIs(creators.AIModelAdapter, adapters.AIModelAdapter)
        self.assertIs(
            typing.get_type_hints(creators.ModelAdapterCreator.create_adapter)["return"],
            adapters.AIModelAdapter,
        )
        expected = {
            "AIModelAdapter": "taip.ai_integration.adapters",
            "ModelAdapterCreator": "taip.ai_integration.creators",
        }
        for class_name, expected_module in expected.items():
            with self.subTest(interface=class_name):
                definitions = [
                    module_name
                    for module_name, tree in production_sources().items()
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ClassDef) and node.name == class_name
                ]
                self.assertEqual(definitions, [expected_module])

    def test_orchestration_package_preserves_exports_and_canonical_identity(self) -> None:
        package = import_module("taip.orchestration")
        participants = {
            "CompletedState": states.CompletedState,
            "ExecutionFacade": facade.ExecutionFacade,
            "ExecutionResultCollector": execution.ExecutionResultCollector,
            "ExecutionRunContext": execution.ExecutionRunContext,
            "ExecutionState": states.ExecutionState,
            "FailedState": states.FailedState,
            "ModelExecution": execution.ModelExecution,
            "ModelExecutionCoordinator": facade.ModelExecutionCoordinator,
            "PendingState": states.PendingState,
            "ProblemOrchestrator": facade.ProblemOrchestrator,
            "RunningState": states.RunningState,
            "VerificationCoordinator": execution.VerificationCoordinator,
        }
        self.assertCountEqual(package.__all__, participants)
        for name, declaration in participants.items():
            with self.subTest(export=name):
                self.assertIs(getattr(package, name), declaration)

    def test_integration_import_graph_has_no_cycles(self) -> None:
        sources = production_sources()
        graph = {}
        for module_name, tree in sources.items():
            module = import_module(module_name)
            dependencies = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    dependencies.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                    if node.level:
                        name = resolve_name("." * node.level + name, module.__package__)
                    dependencies.add(name)
                    dependencies.update(f"{name}.{alias.name}" for alias in node.names)
            graph[module_name] = dependencies & sources.keys()

        visited = set()

        def visit(module_name, path):
            self.assertNotIn(module_name, path, f"Import cycle: {' -> '.join((*path, module_name))}")
            if module_name in visited:
                return
            for dependency in sorted(graph[module_name]):
                visit(dependency, (*path, module_name))
            visited.add(module_name)

        for module_name in sorted(sources):
            visit(module_name, ())

    def test_orchestration_imports_only_approved_ai_abstractions(self) -> None:
        allowed_modules = {
            "__future__", "abc", "taip.common.contracts",
            "taip.orchestration.execution", "taip.orchestration.facade",
            "taip.orchestration.states",
        }
        ai_abstractions = {
            "taip.ai_integration.adapters": {"AIModelAdapter"},
            "taip.ai_integration.creators": {"ModelAdapterCreator"},
        }
        for module_name, tree in production_sources().items():
            if not module_name.startswith("taip.orchestration"):
                continue
            module = import_module(module_name)
            for node in ast.walk(tree):
                with self.subTest(module=module_name, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertIn(alias.name, allowed_modules)
                    elif isinstance(node, ast.ImportFrom):
                        name = node.module or ""
                        if node.level:
                            name = resolve_name("." * node.level + name, module.__package__)
                        if name in ai_abstractions:
                            self.assertLessEqual(
                                {alias.name for alias in node.names}, ai_abstractions[name]
                            )
                        else:
                            self.assertIn(name, allowed_modules)
                        self.assertTrue(all(alias.name != "*" for alias in node.names))

    def test_orchestration_stays_declarative_without_module_side_effects(self) -> None:
        for module_name, tree in production_sources().items():
            if not module_name.startswith("taip.orchestration"):
                continue
            with self.subTest(module=module_name):
                for node in ast.walk(tree):
                    self.assertNotIsInstance(node, ast.Call, "Lab 04 must not execute calls")
                    self.assertNotIsInstance(node, ast.AsyncFunctionDef)
                    if isinstance(node, ast.FunctionDef):
                        body = node.body[1:] if is_docstring(node.body[0]) else node.body
                        self.assertEqual(len(body), 1, f"{node.name} must be an ellipsis stub")
                        self.assertIsInstance(body[0], ast.Expr)
                        self.assertIsInstance(body[0].value, ast.Constant)
                        self.assertIs(body[0].value.value, Ellipsis)
                        for decorator in node.decorator_list:
                            self.assertIsInstance(decorator, ast.Name)
                            self.assertIn(decorator.id, {"property", "abstractmethod"})
                    elif isinstance(node, ast.ClassDef):
                        self.assertEqual(node.decorator_list, [])
                        self.assertEqual(node.keywords, [])
                    elif isinstance(node, ast.AnnAssign):
                        self.assertIsNone(node.value, "Composition must not be initialized")
                scopes = (tree,) + tuple(
                    node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
                )
                for scope in scopes:
                    for statement in scope.body:
                        if is_docstring(statement):
                            continue
                        if isinstance(scope, ast.Module) and isinstance(statement, ast.Assign):
                            self.assertEqual(
                                [getattr(target, "id", None) for target in statement.targets],
                                ["__all__"],
                            )
                            exports = ast.literal_eval(statement.value)
                            self.assertIsInstance(exports, (list, tuple))
                            self.assertTrue(all(isinstance(name, str) for name in exports))
                            continue
                        self.assertIsInstance(
                            statement,
                            (
                                ast.Import, ast.ImportFrom, ast.ClassDef,
                                ast.FunctionDef, ast.AnnAssign,
                            ),
                        )


if __name__ == "__main__":
    unittest.main()
