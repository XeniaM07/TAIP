# TAIP — Timetable Contracts v1

**Status:** proposed contract freeze for team review (not yet merged).  
**Scope:** Romanian school timetabling MVP; Python 3.12.  
**Source of truth for the new workflow:** `src/taip/common/timetable_contracts.py` and `src/taip/common/timetable_protocols.py`.

## 1. Compatibility with existing laboratories

Do **not** delete or rewrite `src/taip/common/contracts.py`. The existing modules and 132 pre-existing tests use its generic `Constraint`, `ProblemSnapshot`, `ModelRequest`, `ModelResponse`, `VerificationResult`, and `ExecutionRun` classes. The new timetable contracts live in separate files to preserve those imports. The old orchestration assumes **one shared snapshot**; the new timetabling workflow uses **one independently confirmed snapshot per AI model**. These are two distinct flows until explicit migration/integration. Passing old tests proves non-regression, not full integration.

The five existing module boundaries and their design patterns remain: `problem_management` (Command/Memento), `ai_integration` (Adapter/Factory Method), `mathematical_verification` (Strategy/Chain of Responsibility), `benchmark` (Template Method/Observer), and `orchestration` (Facade/State).

## 2. Domain and data types

All canonical types are frozen, slotted Python dataclasses unless they are enums or Protocols. Collections in the domain contracts are tuples; confirmed snapshots should be treated as append-only historical versions.

| Type | Key fields | Purpose |
| --- | --- | --- |
| `OriginalProblem` | `problem_id`, `prompt` | Romanian natural-language request |
| `SchoolClass` | `class_id`, `name`, `student_count` | School class |
| `Teacher` | `teacher_id`, `name`, `subject_ids` | Teacher |
| `Subject` | `subject_id`, `name` | Subject |
| `Room` | `room_id`, `name`, `capacity`, `features` | Room |
| `TimeSlot` | `slot_id`, `day`, `period`, optional times | One school period |
| `TeachingRequirement` | `requirement_id`, class/subject/teacher IDs, `sessions_per_week` | Required weekly lesson count |
| `TimetableConstraint` | `constraint_id`, `type`, `severity`, typed `parameters`, `weight`, `source_text` | Hard/soft rule |
| `TimetableData` | tuples of entities, requirements, constraints | Domain dataset |
| `ExtractionProposal` | `proposal_id`, `problem_id`, `model_id`, `data`, missing/unsupported info | One AI's extraction, not necessarily valid |
| `ProblemDraft` | `draft_id`, `proposal_id`, `problem_id`, `model_id`, `revision`, `data` | Editable model-specific state |
| `TimetableProblemSnapshot` | `snapshot_id`, `draft_id`, `problem_id`, `model_id`, `version`, `original_prompt`, `data` | Explicitly confirmed version |
| `LessonAssignment` | `requirement_id`, zero-based `occurrence`, `slot_id`, `room_id` | One scheduled lesson |
| `TimetableSolution` | `solution_id`, `snapshot_id`, `model_id`, assignments | Model output |
| `TimetableVerificationResult` | solution/snapshot IDs, status, violations, optional soft penalty | Independent verification |
| `ModelRunResult`, `ComparisonRun` | per-model IDs, durations, edit counts, verification status | Benchmark comparison |

`Weekday` supports Monday–Friday. `ConstraintSeverity` is `hard` or `soft`. `VerificationStatus` is `verified`, `failed`, or `inconclusive`. `ConstraintType` and its matching parameter dataclasses are defined in `timetable_contracts.py`; consumers must not invent additional constraint names in JSON.

## 3. Required scheduling semantics

MVP lessons each occupy **exactly one** `TimeSlot`; no class subdivisions or multi-period blocks. The timetable verifier must independently check:

1. Every `TeachingRequirement` has exactly `sessions_per_week` assignments, with unique occurrences `0..sessions_per_week-1`.
2. Every assignment refers to an existing requirement, slot and room.
3. A teacher cannot teach two lessons in the same slot.
4. A school class cannot have two lessons in the same slot.
5. A room cannot host two lessons in the same slot.
6. Explicit hard teacher/room/class unavailability, required room features, room capacity when known, and hard maximum daily class periods must be enforced.
7. Soft constraints (teacher preferences, teacher/class gaps, daily class load balance) contribute to an explained penalty rather than making an otherwise feasible timetable invalid.

Missing required data or unresolved references block confirmation. An AI must report missing information rather than silently inventing a teacher, class, room, slot, or lesson count. Unsupported requests must be surfaced as `unsupported_requirements`; they must not silently disappear. Validation of these invariants is **not yet implemented** merely by defining frozen dataclasses.

## 4. Five module interfaces

The canonical public Protocols are in `src/taip/common/timetable_protocols.py`. Implementations must match their method names, parameters, and return types.

| Module / owner | Port | Responsibilities |
| --- | --- | --- |
| Problem Management | `ProblemManagementPort` | `create_draft`, `get_draft`, `apply_edit`, `undo`, `redo`, `validate`, `confirm`, `get_snapshot`; Command + Memento, model-specific version history |
| AI Integration | `AIProviderPort`, `AIProviderFactoryPort` | `extract(OriginalProblem)`, `solve(TimetableProblemSnapshot)`; independent adapters, strict response parsing, errors and timeouts, mock providers |
| Mathematical Verification | `VerificationPort` | `verify(snapshot, solution)`; check all hard invariants independently of provider, compute soft penalty, explain violations |
| Benchmark | `BenchmarkPort` | `record_run`, `compare`; track extraction/solving times, corrections, valid results, failures; JSON/CSV export may be added by implementation |
| Orchestration | `OrchestrationPort` | `submit_problem`, `extract_for_model`, `confirm_draft`, `solve_and_verify`, `get_comparison`; per-model sessions and API/UI integration |

The new providers planned for the application are Gemini via Google AI Studio, Llama via Groq, and Qwen via OpenRouter. Their exact model identifiers must be confirmed when API integration is implemented. Existing `GeminiAdapter`, `OpenAIAdapter`, `ThirdModelAdapter` declarations belong to the older workflow and must not be renamed blindly.

## 5. Lifecycle and isolation

`OriginalProblem` → three separate `ExtractionProposal`s → three independently editable `ProblemDraft`s → explicit confirmation of each → three `TimetableProblemSnapshot`s → per-snapshot `TimetableSolution` → independent `TimetableVerificationResult` → `ComparisonRun`.

- A solution must match its snapshot's `snapshot_id` and `model_id`.
- A model's draft and confirmation must not mutate another model's draft/snapshot.
- Edits after confirmation require a new snapshot version; never overwrite confirmed data.
- Failure of one provider must not erase other models' outcomes.
- Compare end-to-end extraction quality, correction effort, validity, and latency. **Do not attribute differences in solution quality solely to the model when the confirmed snapshots differ.** For a controlled solver comparison, provide the same snapshot to all providers in a separate experiment.

## 6. Serialization and validation boundary

The dataclasses define **Python in-memory contracts**, not an automatic JSON codec. Enum values, tuples, nested parameter variants, and optional fields require explicit serialization/deserialization and validation at the API/storage boundary. The JSON fixtures are illustrative wire examples, not proof that `from_dict` or strict schema validation already exists.

Before frontend/API integration, the team must agree on and test: JSON representation for `TimetableConstraint.parameters`, required/optional fields, enum decoding, error responses, deterministic round-trip, unique IDs, cross-references, and the allowed severity/type combinations. The Protocols describe signatures, not implementations.

## 7. Fixtures and tests

The reference fixtures live in `tests/fixtures/timetable/`:

- `original_problem.json`
- `extraction_ai1.json`, `extraction_ai2.json`, `extraction_ai3.json`
- `confirmed_snapshot.json`
- `valid_solution.json`, `invalid_solution.json`

New contract tests: `tests/common/test_timetable_contracts.py`. The initial expected suite is **139 tests** (132 existing + 7 new). These tests currently check structural examples and basic invariants, **not** the full mathematical verifier, persistence, or end-to-end new workflow.

Run from repository root:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
py -3.12 -m unittest discover -s tests -v
```

## 8. Freeze and integration checklist

Before approving the contract PR, all five owners should review this document and the Python Protocols, particularly per-model snapshots, constraint parameter JSON, error handling, and verification semantics. Changes to the shared contracts after approval require a team-reviewed PR. Each module should then implement against these Protocols using mocks and add module-specific tests. Merge only after all tests pass and the new workflow has at least one integration test.
