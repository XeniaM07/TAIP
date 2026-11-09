# TAIP

Python project for the **AI-Assisted Mathematical Problem Solving and Evidence Verification Platform**. It includes the structural TAIP modules and a Lab 06 mock application with a FastAPI backend and React client.

## Scope

The original Lab 04 work defines class, attribute, method-signature, and relationship declarations for the five TAIP modules. Lab 06 adds a local mock-backed workflow application; real provider APIs and timetable optimization remain future work.

## Requirements

- Python 3.12+
- Git

## Project layout

```text
src/taip/
  common/                     Shared immutable data contracts
  problem_management/         Constraint editing and versioning (Command, Memento)
  ai_integration/             AI provider adapters (Adapter, Factory Method)
  mathematical_verification/  Independent checks (Strategy, Chain of Responsibility)
  benchmark/                  Independent evaluation (Template Method, Observer)
  orchestration/              Execution workflow (Facade, State)
  application/                FastAPI workflow and expiring in-memory sessions
frontend/                     Minimal React client
docs/                         Module boundaries and collaboration rules
tests/                        Structural contract tests
```

## Local checks

From the repository root:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
```

On Windows PowerShell, set the import path with `$env:PYTHONPATH = "src"` before the unittest command, or install the project locally using `python -m pip install -e .`.

## Local application (Lab 06)

The API uses deterministic mock providers by default. Sessions and all three model workspaces live in a thread-safe in-memory repository, expire after two hours without activity, and are cleared when the server restarts. There is no account system or persistent database.

Install test tooling with `uv sync --extra test` before running the unit and API tests.

Run the tests with `uv run --extra test python -m unittest discover -s tests -v`.

Install the backend and start it from the repository root:

```powershell
uv sync
uv run uvicorn taip.application.api:app --reload
```

Install and start the React client in a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. API docs are at `http://127.0.0.1:8000/docs`. The workflow is create session → extract draft → edit/save → confirm → solve → verify; the comparison panel summarizes all three independent model workspaces. The JSON editor lets you change timetable entities and constraints. The mock extractor intentionally returns a small sample timetable for any prompt; it does not interpret natural language. Replace `MockProviderFactory` with implementations of `AIProviderPort` to connect real extractors and solvers without changing the HTTP contract. The verifier is injected through `VerificationPort`.

### Architecture and monitoring

`WorkflowService` is the application Facade. It receives the session repository, provider factory, and verifier through interfaces. Each model has its own draft, confirmed snapshot, solution, verification result, timing, and lifecycle state. The API exposes only session-scoped operations; UUID session identifiers are unguessable references, not user authentication. `InMemorySessionRepository` uses a lock for concurrent access and removes inactive sessions during repository access. Logging around workflow operations records identifiers, state changes, durations, and outcomes without logging problem text. The existing `TimetableVerifier` uses the `monitored_verify` decorator as an AOP-style cross-cutting concern and records verification duration and findings.

The canonical endpoint contracts are available in OpenAPI at `/docs`: `POST /api/sessions`, `POST /api/sessions/{session_id}/extract`, `GET|PATCH /api/sessions/{session_id}/drafts/{model_id}`, model `confirm`, `solve`, and `verify` actions, and `GET /api/sessions/{session_id}/compare`.

## Documents

- [Architecture and module ownership](docs/ARCHITECTURE.md)
- [Contribution rules](CONTRIBUTING.md)


## Future work

Real AI provider SDKs, a production timetable optimizer, durable storage, authentication, and deployment remain future work. The application currently has mock extraction and solving providers, temporary anonymous sessions, and decorator-based monitoring for verification.

## AI disclosure

The initial structural scaffolding and shared contract draft were prepared with assistance from OpenAI ChatGPT (GPT-6), based on the Lab 04 handout, the team's Lab 03 requirements, and iterative discussions about task assignment. The team must review these declarations and record the exact prompts and changes in its submission, as required by Lab 04.
