---
name: feature-gate-implementation
description: Workflow command scaffold for feature-gate-implementation in Noahstark23-polybot.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /feature-gate-implementation

Use this workflow when working on **feature-gate-implementation** in `Noahstark23-polybot`.

## Goal

Implements a new major feature gate (F0, F1, F2, F3) including core logic, supporting modules, configuration, and tests. Each gate adds a new functional layer and is validated by tests and operational scripts.

## Common Files

- `src/*/*.py`
- `src/utils/config.py`
- `src/runner.py`
- `tests/test_*.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Create or update main implementation modules under src/ (e.g., new engine, service, or manager).
- Update or add configuration logic in src/utils/config.py.
- Add or update related database models or registry files if needed.
- Write or update supporting scripts or monitoring/analytics modules.
- Add or update corresponding tests under tests/ (unit and integration).

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.