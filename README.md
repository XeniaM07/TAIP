# TAIP

Structural Python project for the **AI-Assisted Mathematical Problem Solving and Evidence Verification Platform**.

## Scope

Lab 04 requires **class, attribute, method-signature, and relationship declarations**, not working application functionality. Each of the five members will contribute **at least two identifiable design patterns**. No AI API, mathematical solver, benchmark execution, or web app is implemented here.

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

## Documents

- [Architecture and module ownership](docs/ARCHITECTURE.md)
- [Contribution rules](CONTRIBUTING.md)


## Future work

FastAPI, mathematical libraries, AI providers, database, and an AOP implementation are future decisions, not part of Lab 04. Confirm the instructor's AOP expectations before choosing a specific mechanism.

## AI disclosure

The initial structural scaffolding and shared contract draft were prepared with assistance from OpenAI ChatGPT (GPT-6), based on the Lab 04 handout, the team's Lab 03 requirements, and iterative discussions about task assignment. The team must review these declarations and record the exact prompts and changes in its submission, as required by Lab 04.
