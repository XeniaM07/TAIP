# TAIP Lab 04 — Module Boundaries

This is a **proposed** module decomposition based on the Lab 03 analysis. Owners must confirm all contracts before merging pattern declarations.

| Module | Owner | Responsibility | Patterns |
|---|---|---|---|
| `problem_management` | Scrum Master (AI 1) | Draft problems, editable constraints, confirmed version boundary | Command, Memento |
| `ai_integration` | AI 2 | Common interfaces for three model providers | Adapter, Factory Method |
| `mathematical_verification` | AI 3 | Independent verification and unsupported-case routing | Strategy, Chain of Responsibility |
| `benchmark` | AI 4 | Independent, versioned model benchmark and result notifications | Template Method, Observer |
| `orchestration` | Distributed Systems | Execution coordination, per-model failure representation, overall lifecycle | Facade, State |

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

Dependencies on concrete classes across module boundaries should be avoided; use public interfaces. If an interface is needed by multiple modules, agree on its home before implementing it.

## Core invariants from Lab 03

1. Model execution is authorized only after explicit Save & Confirm.
2. The three models receive the same confirmed snapshot version.
3. Confirmed snapshots cannot be silently overwritten. A new confirmation creates a new version.
4. Verification status distinguishes verified, failed, and inconclusive.
5. One provider failure does not remove the other model results.
6. Benchmarking remains distinct from verification of a single user problem.

This setup only **represents** these concepts; enforcement is future implementation work.

## Caveats

- `ProblemSnapshot` is a frozen dataclass and its constraints are a tuple of frozen `Constraint` objects. This is a shallow structural immutability design appropriate for the declared fields.
- `ModelStatus` and `ExecutionStatus` are different because model requests may fail independently.
- Data fields and provider identities are provisional until agreed by the team.
- Factory Method requires a creator hierarchy, and Template Method requires an intended shared workflow; merely naming classes does not demonstrate the patterns.
