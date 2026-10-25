# Contribution Guidelines — Lab 04

## Python style

- Class names: `PascalCase`; functions, files and attributes: `snake_case`; constants: `UPPER_SNAKE_CASE`.
- Use type hints for public methods and attributes.
- Use `abc.ABC` / `@abstractmethod` or `typing.Protocol` when an interface is needed.
- Do not implement functional behavior in Lab 04. Use method stubs (`...` / `pass`) where appropriate.
- Import contracts from `taip.common.contracts` and do not duplicate definitions.
- Avoid circular imports and dependencies on provider-specific classes in coordination interfaces.

## Git

- Branch names: `feature/<ticket-id>-<short-name>` (example: `feature/ai-01-adapter`).
- Commits: concise imperative messages (example: `feat: declare AI adapter interfaces`).
- One primary owner per ticket; create a Pull Request to `main`.
- PR description: related Trello ticket, classes/interfaces introduced, pattern roles, dependencies, and manual verification performed.
- Another team member reviews a PR before merging, including the Scrum Master's work.

## Structural checks

1. Classes, attributes, method signatures and relationships have English names.
2. Each pattern's participants and relationships are clearly identifiable.
3. Shared types match `common/contracts.py`.
4. `python -m compileall -q src tests` passes.
5. `PYTHONPATH=src python -m unittest discover -s tests -v` passes (PowerShell: `$env:PYTHONPATH = "src"`).
6. Functional behavior, provider calls and mathematical algorithms are not introduced.

## AI usage

Document any AI tool and known model/version, prompts including relevant follow-ups, and how suggestions were used and modified. This is required by the Lab 04 handout when AI assistance is used.

## AOP

Keep cross-cutting concerns (logging, tracing, access checks) separate from core module declarations. Confirm how AOP must be demonstrated in future labs; a Python decorator alone does not automatically meet every possible AOP requirement.
