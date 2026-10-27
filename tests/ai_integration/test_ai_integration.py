"""Public structural contracts for AI-01 and AI-02, without provider calls."""

import ast
import inspect
import typing
import unittest
from importlib import import_module

from taip.common.contracts import (
    ModelRequest,
    ModelResponse,
    ModelStatus,
    ProblemSnapshot,
)


PROVIDERS = (
    ("OpenAIAdapter", "OpenAIAdapterCreator", "OpenAIClient"),
    ("GeminiAdapter", "GeminiAdapterCreator", "GeminiClient"),
    ("ThirdModelAdapter", "ThirdModelAdapterCreator", "ThirdModelClient"),
)
PRODUCTION_MODULES = ("adapters", "provider_clients", "creators", "__init__")


def is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


class AIIntegrationStructureTests(unittest.TestCase):
    def require_module(self, name: str):
        """Make an absent declaration a useful test failure, not a loader error."""
        qualified_name = "taip.ai_integration"
        if name != "__init__":
            qualified_name += "." + name
        try:
            return import_module(qualified_name)
        except ModuleNotFoundError as error:
            if error.name == qualified_name:
                self.fail(f"Required declaration module is absent: {qualified_name}")
            raise

    def require_class(self, module, name: str):
        declaration = getattr(module, name, None)
        self.assertTrue(
            inspect.isclass(declaration),
            f"Missing class: {module.__name__}.{name}",
        )
        return declaration

    def assert_signature(self, operation, parameters, hints) -> None:
        signature = inspect.signature(operation)
        self.assertEqual(
            tuple(signature.parameters),
            tuple(name for name, _ in parameters),
        )
        for name, kind in parameters:
            with self.subTest(parameter=name):
                self.assertEqual(signature.parameters[name].kind, kind)
                self.assertIs(signature.parameters[name].default, inspect.Parameter.empty)
        self.assertEqual(typing.get_type_hints(operation), hints)
        self.assertFalse(inspect.iscoroutinefunction(operation))

    def test_adapter_interface_is_abstract_and_cannot_be_instantiated(self) -> None:
        adapters = self.require_module("adapters")
        base = self.require_class(adapters, "AIModelAdapter")
        self.assertTrue(inspect.isabstract(base))
        self.assertEqual(
            base.__abstractmethods__,
            {"generate_solution", "get_model_identifier"},
        )
        with self.assertRaises(TypeError):
            base()

    def test_adapter_operations_use_shared_dtos_and_caller_owned_run_id(self) -> None:
        adapters = self.require_module("adapters")
        base = self.require_class(adapters, "AIModelAdapter")
        declarations = (base,) + tuple(
            self.require_class(adapters, row[0]) for row in PROVIDERS
        )
        for declaration in declarations:
            with self.subTest(adapter=declaration.__name__):
                self.assert_signature(
                    declaration.generate_solution,
                    (
                        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                        ("request", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                        ("run_id", inspect.Parameter.KEYWORD_ONLY),
                    ),
                    {"request": ModelRequest, "run_id": str, "return": ModelResponse},
                )
                self.assert_signature(
                    declaration.get_model_identifier,
                    (("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),),
                    {"return": str},
                )

    def test_each_adapter_is_concrete_and_declares_typed_composition(self) -> None:
        adapters = self.require_module("adapters")
        clients = self.require_module("provider_clients")
        base = self.require_class(adapters, "AIModelAdapter")
        for adapter_name, _, client_name in PROVIDERS:
            with self.subTest(adapter=adapter_name):
                adapter = self.require_class(adapters, adapter_name)
                self.assertTrue(issubclass(adapter, base))
                self.assertFalse(inspect.isabstract(adapter))
                self.assertEqual(
                    typing.get_type_hints(adapter),
                    {"model_id": str, "client": self.require_class(clients, client_name)},
                )
                self.assertIn("generate_solution", adapter.__dict__)
                self.assertIn("get_model_identifier", adapter.__dict__)

    def test_creator_interface_is_abstract_and_cannot_be_instantiated(self) -> None:
        creators = self.require_module("creators")
        adapters = self.require_module("adapters")
        creator = self.require_class(creators, "ModelAdapterCreator")
        self.assertTrue(inspect.isabstract(creator))
        self.assertEqual(creator.__abstractmethods__, {"create_adapter"})
        with self.assertRaises(TypeError):
            creator()
        self.assert_signature(
            creator.create_adapter,
            (("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),),
            {"return": self.require_class(adapters, "AIModelAdapter")},
        )

    def test_concrete_creators_compose_clients_and_declare_covariant_products(self) -> None:
        creators = self.require_module("creators")
        adapters = self.require_module("adapters")
        clients = self.require_module("provider_clients")
        creator_base = self.require_class(creators, "ModelAdapterCreator")
        for adapter_name, creator_name, client_name in PROVIDERS:
            with self.subTest(creator=creator_name):
                creator = self.require_class(creators, creator_name)
                self.assertTrue(issubclass(creator, creator_base))
                self.assertFalse(inspect.isabstract(creator))
                self.assertEqual(
                    typing.get_type_hints(creator),
                    {"model_id": str, "client": self.require_class(clients, client_name)},
                )
                self.assertIn("create_adapter", creator.__dict__)
                self.assert_signature(
                    creator.create_adapter,
                    (("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),),
                    {"return": self.require_class(adapters, adapter_name)},
                )

    def test_provider_dependencies_are_local_protocols(self) -> None:
        clients = self.require_module("provider_clients")
        for name in (
            "OpenAITextResponse",
            "OpenAIResponses",
            "OpenAIClient",
            "GeminiTextResponse",
            "GeminiModels",
            "GeminiClient",
            "ThirdModelClient",
        ):
            with self.subTest(protocol=name):
                protocol = self.require_class(clients, name)
                self.assertIn(typing.Protocol, protocol.__mro__)
                self.assertEqual(protocol.__module__, clients.__name__)

    def test_provider_text_and_client_properties_are_read_only_and_typed(self) -> None:
        clients = self.require_module("provider_clients")
        for owner_name, property_name, return_type in (
            ("OpenAITextResponse", "output_text", str),
            ("OpenAIClient", "responses", self.require_class(clients, "OpenAIResponses")),
            ("GeminiTextResponse", "text", str | None),
            ("GeminiClient", "models", self.require_class(clients, "GeminiModels")),
        ):
            with self.subTest(owner=owner_name, property=property_name):
                owner = self.require_class(clients, owner_name)
                descriptor = owner.__dict__.get(property_name)
                self.assertIsInstance(descriptor, property)
                self.assertIsNone(descriptor.fset)
                self.assert_signature(
                    descriptor.fget,
                    (("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),),
                    {"return": return_type},
                )

    def test_provider_generation_ports_declare_narrow_text_signatures(self) -> None:
        clients = self.require_module("provider_clients")
        for owner_name, operation_name, input_name, return_type in (
            (
                "OpenAIResponses", "create", "input",
                self.require_class(clients, "OpenAITextResponse"),
            ),
            (
                "GeminiModels", "generate_content", "contents",
                self.require_class(clients, "GeminiTextResponse"),
            ),
            ("ThirdModelClient", "generate", "prompt", str),
        ):
            with self.subTest(protocol=owner_name):
                owner = self.require_class(clients, owner_name)
                self.assert_signature(
                    getattr(owner, operation_name),
                    (
                        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                        ("model", inspect.Parameter.KEYWORD_ONLY),
                        (input_name, inspect.Parameter.KEYWORD_ONLY),
                    ),
                    {"model": str, input_name: str, "return": return_type},
                )

    def test_adapter_boundary_reuses_immutable_shared_dtos(self) -> None:
        adapters = self.require_module("adapters")
        base = self.require_class(adapters, "AIModelAdapter")
        hints = typing.get_type_hints(base.generate_solution)
        self.assertIs(hints["request"], ModelRequest)
        self.assertIs(hints["return"], ModelResponse)
        snapshot = ProblemSnapshot("p-1", 1, "Solve x = 2", "Find x", ())
        request = hints["request"](model_id="model-1", snapshot=snapshot)
        response = hints["return"](
            model_id="model-1",
            run_id="run-1",
            output=None,
            status=ModelStatus.PENDING,
        )
        self.assertIs(request.snapshot, snapshot)
        with self.assertRaises(AttributeError):
            request.model_id = "changed"
        with self.assertRaises(AttributeError):
            response.run_id = "changed"

    def test_ai_model_adapter_has_one_canonical_definition(self) -> None:
        definitions = []
        for name in PRODUCTION_MODULES:
            module = self.require_module(name)
            tree = ast.parse(inspect.getsource(module))
            definitions.extend(
                name
                for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef) and node.name == "AIModelAdapter"
            )
        self.assertEqual(definitions, ["adapters"])

    def test_production_sources_contain_declarations_without_executable_bodies(self) -> None:
        for name in PRODUCTION_MODULES:
            with self.subTest(module=name):
                tree = ast.parse(inspect.getsource(self.require_module(name)))
                for node in ast.walk(tree):
                    self.assertNotIsInstance(node, ast.Call, "Lab 04 must not execute calls")
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        body = node.body
                        if body and is_docstring(body[0]):
                            body = body[1:]
                        self.assertEqual(
                            len(body), 1,
                            f"{node.name} must have only an ellipsis body",
                        )
                        self.assertIsInstance(body[0], ast.Expr)
                        self.assertIsInstance(body[0].value, ast.Constant)
                        self.assertIs(body[0].value.value, Ellipsis)
                    if isinstance(node, ast.AnnAssign):
                        self.assertIsNone(
                            node.value,
                            "Composition attributes must not be initialized",
                        )
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
                            ast.literal_eval(statement.value)
                            continue
                        self.assertIsInstance(
                            statement,
                            (
                                ast.Import, ast.ImportFrom, ast.ClassDef,
                                ast.FunctionDef, ast.AnnAssign,
                            ),
                        )

    def test_production_imports_preserve_module_boundaries_and_typed_payloads(self) -> None:
        allowed_imports = {
            "__future__",
            "abc",
            "typing",
            "taip.common.contracts",
            "taip.ai_integration.adapters",
            "taip.ai_integration.provider_clients",
            "taip.ai_integration.creators",
        }
        for name in PRODUCTION_MODULES:
            with self.subTest(module=name):
                tree = ast.parse(inspect.getsource(self.require_module(name)))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertIn(alias.name, allowed_imports)
                    elif isinstance(node, ast.ImportFrom):
                        imported_module = node.module or ""
                        if node.level:
                            self.assertEqual(node.level, 1)
                            imported_module = "taip.ai_integration." + imported_module
                        self.assertIn(imported_module, allowed_imports)
                        self.assertTrue(all(alias.name != "*" for alias in node.names))
                        if imported_module == "typing":
                            self.assertTrue(
                                all(
                                    alias.name not in {"Any", "runtime_checkable"}
                                    for alias in node.names
                                )
                            )
                    elif isinstance(node, ast.Name):
                        self.assertNotIn(node.id, {"Any", "object", "runtime_checkable"})
                    elif isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, {"Any", "runtime_checkable"})


if __name__ == "__main__":
    unittest.main()