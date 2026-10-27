# TAIP Agent Skills

These rules apply to every coding agent working on the TAIP repository, regardless of module, task, or development stage.

Before modifying code, read this file and `docs/ARCHITECTURE.md`. Follow existing project conventions and respect the boundaries between modules.

## 1. Simplicity and Readability

- Prefer the simplest solution that correctly satisfies the requirements.
- Use descriptive names, focused classes, and small, understandable methods.
- Avoid unnecessary abstractions, duplicated code, premature optimization, and excessive configurability.
- Prefer early returns and guard clauses over deeply nested conditionals.
- Remove unused imports, dead code, debug statements, and obsolete comments.
- Comments should explain why a decision was made, not repeat what the code does.

## 2. Types and Data Contracts

- Add meaningful type hints to public methods, function signatures, and attributes.
- Prefer domain-specific types and enums over loosely typed strings or dictionaries.
- Use dataclasses or other appropriate models for structured data.
- Validate untrusted input at system boundaries and keep internal data structures well-defined.
- Avoid `Any` and untyped dictionaries when a more precise type is practical.
- Keep shared domain contracts in their designated authoritative module.
- Do not redefine shared types in individual modules.
- Treat immutable data as immutable throughout the relevant workflow.

## 3. Architecture and Module Boundaries

- Follow the responsibilities and dependency rules defined in `docs/ARCHITECTURE.md`.
- Keep business logic, orchestration, integrations, persistence, and presentation concerns appropriately separated.
- Depend on public interfaces rather than concrete implementations wherever practical.
- Avoid circular imports and dependencies on another module's internal implementation details.
- Place shared functionality in an appropriate shared module instead of duplicating it.
- Keep changes localized to the modules responsible for the requested functionality.
- Update the architecture documentation when an approved architectural decision changes.
- Do not introduce new architectural layers without a clear reason.

## 4. Design Patterns and Abstractions

- Use design patterns when they solve a real design problem or are explicitly required by the project.
- Make the relationships between pattern participants clear and understandable.
- Do not introduce unnecessary abstractions merely to use a pattern.
- Preserve existing pattern responsibilities when extending related classes.
- Prefer composition over inheritance unless inheritance expresses a meaningful relationship.
- Use interfaces and abstract classes where they improve substitutability or define a genuine contract.
- Do not treat a class name or inheritance declaration alone as proof that a design pattern is correctly implemented.

## 5. Error Handling and Reliability

- Handle expected failure cases explicitly.
- Use appropriate exception types and meaningful error messages.
- Never silently convert a failure into an apparently successful result.
- Avoid bare `except` clauses and swallowed exceptions.
- Do not catch exceptions unless the code can handle them appropriately or add useful context before propagating them.
- Keep failures isolated when independent operations should be able to succeed separately.
- Represent missing, failed, unsupported, and uncertain results distinctly when the domain requires it.
- Avoid introducing retries, fallbacks, or recovery mechanisms without a clear requirement.

## 6. AI Integration and Mathematical Verification

- Treat external model responses and user-provided prompts as untrusted input.
- Keep provider-specific behavior behind stable integration interfaces.
- Do not assume that a fluent or plausible AI response is mathematically correct.
- Keep mathematical verification independent of the AI model producing the candidate answer.
- Distinguish verified results, failed verification, and inconclusive verification.
- Never label an answer as verified when the available evidence is insufficient.
- Represent unsupported problem categories and unavailable verification methods explicitly.
- Keep benchmark evaluation separate from the verification of an individual user problem.
- Preserve the identity and version of the problem, model, verification method, and benchmark configuration wherever needed for reproducibility.

## 7. Problem Lifecycle and Data Integrity

- Preserve the distinction between editable drafts and explicitly confirmed problems.
- Do not authorize model execution before the required confirmation step.
- Ensure all models participating in the same comparison receive the same confirmed problem version.
- Do not silently overwrite previously confirmed snapshots.
- Represent individual model outcomes independently so one failure does not erase other results.
- Keep execution status separate from individual model status and verification status.
- Treat changes to established data contracts as coordinated architectural changes.
- Do not claim that a rule is enforced at runtime unless the implementation actually enforces it.

## 8. Security and Privacy

- Never hardcode API keys, passwords, tokens, or other secrets.
- Use the project's approved configuration and secret-management mechanisms.
- Do not expose credentials or sensitive user data in logs, exceptions, or error responses.
- Validate and constrain untrusted input at appropriate boundaries.
- Never execute AI-generated code or commands without an explicitly designed and controlled execution environment.
- Apply authorization checks to protected operations and user-owned resources.
- Avoid collecting or retaining data that is not needed for the requested functionality.
- Treat external integrations as potentially unreliable and untrusted.

## 9. Testing and Quality Assurance

- Add or update tests when changing behavior.
- Test public contracts and observable behavior rather than unnecessary implementation details.
- Cover important success paths, failure paths, invalid input, and relevant edge cases.
- Test module boundaries and integration points where regressions are likely.
- Mock external AI providers and other services in tests that should not require real external access.
- Keep tests deterministic and independent of network availability.
- Do not weaken or remove valid tests merely to make a change pass.
- Run the relevant tests and quality checks before reporting a task as complete.
- Clearly report checks that were not run or that failed.

## 10. Documentation and Maintainability

- Give non-trivial modules a short docstring explaining their purpose and boundaries.
- Document public interfaces and non-obvious design decisions.
- Keep documentation consistent with actual behavior.
- Update relevant documentation when changing contracts, dependencies, configuration, or workflows.
- Prefer concise examples that demonstrate the intended use of an interface.
- Do not document planned functionality as if it were already implemented.
- Record unresolved architectural decisions rather than silently inventing project-wide conventions.

## 11. Dependencies and Configuration

- Prefer the Python standard library when it is sufficient.
- Add third-party dependencies only when they provide clear value for an approved requirement.
- Check existing project configuration before introducing a new tool or package.
- Avoid unnecessary changes to dependency versions, build configuration, or environment settings.
- Keep environment-specific configuration outside application logic.
- Respect the Python version and tooling specified by the repository.
- Do not introduce a framework or infrastructure component solely because it might be useful in the future.

## 12. Git and Collaboration

- Inspect the current working tree and relevant files before making changes.
- Preserve unrelated modifications and existing work from other contributors.
- Respect module ownership and coordinate changes to shared contracts.
- Keep changes focused, reviewable, and consistent with the existing architecture.
- Do not overwrite another contributor's work without agreement.
- Do not discard, reset, or revert unrelated changes.
- Explain contract changes and their impact on dependent modules.
- Report the files changed, important decisions, tests performed, and remaining limitations.

## 13. Definition of Done

Before considering a task complete:

1. The requested functionality or structural change matches the agreed requirements.
2. The implementation respects the documented architecture and shared contracts.
3. Relevant failure cases and security considerations have been addressed.
4. Appropriate tests have been added or updated.
5. Relevant tests and validation commands have been run.
6. Documentation has been updated where necessary.
7. No unrelated changes, debug artifacts, or accidental dependencies have been introduced.
8. The final report accurately distinguishes implemented functionality from future work.

When requirements are ambiguous, inspect the existing code and documentation first. If an unresolved decision materially affects the architecture, identify the ambiguity and request clarification rather than silently inventing a requirement.