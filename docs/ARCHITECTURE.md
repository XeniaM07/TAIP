# TAIP Lab 04 — Module Boundaries

This is a **proposed** module decomposition based on the Lab 03 analysis. Owners must confirm all contracts before merging pattern declarations.

| Module | Owner | Responsibility | Patterns |
|---|---|---|---|
| `problem_management` | Scrum Master (AI 1) | Draft problems, editable constraints, confirmed version boundary | Command, Memento |
| `ai_integration` | AI 2 | Common interfaces for three model providers | Adapter, Factory Method |
| `mathematical_verification` | AI 3 | Independent verification and unsupported-case routing | Strategy, Chain of Responsibility |
| `benchmark` | AI 4 | Independent, versioned model benchmark and result notifications | Template Method, Observer |
| `orchestration` | Distributed Systems | Execution coordination, per-model failure representation, overall lifecycle | Facade, State |
| `application` | Application layer | HTTP API, workflow Facade, session repository, provider and verification composition | Facade, Dependency Injection |

## Contract ownership

`src/taip/common/contracts.py` is the **single source of truth** for:

- `Constraint`, `ProblemSnapshot`
- `ModelRequest`, `ModelResponse`, `ModelStatus`
- `VerificationResult`, `VerificationStatus`
- `ExecutionRun`, `ExecutionStatus`

All module owners import shared contracts from `taip.common.contracts`; do **not** redefine them in individual modules. Changes require a team-reviewed Pull Request.

## Allowed dependencies

- `common`: depends on no other TAIP module.
- `problem_management`: may import `common`.
- `ai_integration`: may import `common`.
- `mathematical_verification`: may import `common`.
- `benchmark`: may import `common` and use published model interface types, without importing orchestration internals.
- `orchestration`: may import `common`, `ai_integration`, and public verification interfaces.
- `application`: composes `common`, `problem_management`, and public mathematical verification interfaces; it is the outermost TAIP layer and may expose them over HTTP.

Dependencies on concrete classes across module boundaries should be avoided; use public interfaces. If an interface is needed by multiple modules, agree on its home before implementing it.

## Declared integration points

- `MathematicalProblem.confirm(version)` declares the draft-to-confirmed-snapshot boundary. `ProblemVersionHistory` declares storage and retrieval by problem ID and version. `ProblemSnapshot` is immutable; the method declarations do not enforce confirmation or append-only versioning at runtime.
- `ModelExecutionCoordinator` composes three `ModelAdapterCreator` interfaces and declares adapter/request creation for one `ExecutionRun`. The run contains one shared `ProblemSnapshot`; each `ModelRequest` carries that snapshot.
- `VerificationHandler` composes its corresponding `VerificationStrategy`. `VerificationCoordinator` depends on the public handler abstraction rather than a concrete handler.
- `ExecutionRunContext` relates the run to three independent `ModelExecution` records and an `ExecutionState`. The result collector returns those model records so a failed model remains represented. `PartialState` corresponds to `ExecutionStatus.PARTIAL`.

The original Lab 04 diagrams capture planned relationships between components; the Lab 06 application below wires the timetable workflow through the published interfaces.

## Lab 06 application flow

`taip.application.api` defines the FastAPI contract. `WorkflowService` is the application Facade and receives a `SessionRepository`, `ProviderFactory`, and `VerificationPort`; provider and verifier implementations can be replaced without changing route schemas. Each session owns three `ModelWorkspace` instances, each with an isolated `ProblemManagementPort` backed by an in-memory repository, plus its own draft, immutable confirmed snapshot, solution, and verification result. The outer session repository is lock-protected and purges sessions after two hours without access. The API requires the session UUID on all operations, has no account/authentication or persistent storage, and returns 404 for missing or expired sessions.

The lifecycle states are `PENDING → EXTRACTING → DRAFT → CONFIRMED → SOLVING → COMPLETED`, with `FAILED` recorded on extraction/solving errors. The mock provider produces a small static sample timetable; prompts are retained for correlation but are not semantically parsed. Timetable verification uses the existing Strategy/Chain and `monitored_verify` decorator. Application events log session/model identifiers, transitions, durations, and errors, without prompt contents. The React client is in `frontend/` and calls only `/api` contracts.

## Core invariants from Lab 03

1. Model execution is authorized only after explicit Save & Confirm.
2. The three models receive the same confirmed snapshot version.
3. Confirmed snapshots cannot be silently overwritten. A new confirmation creates a new version.
4. Verification status distinguishes verified, failed, and inconclusive.
5. One provider failure does not remove the other model results.
6. Benchmarking remains distinct from verification of a single user problem.

This setup only **represents** these concepts; enforcement is future implementation work.
