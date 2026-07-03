---
name: test-driven-module-extension
description: Workflow command scaffold for test-driven-module-extension in Noahstark23-polybot.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /test-driven-module-extension

Use this workflow when working on **test-driven-module-extension** in `Noahstark23-polybot`.

## Goal

Adds a new module or extends an existing one, always accompanied by new or updated tests to ensure correctness and coverage.

## Common Files

- `src/*/*.py`
- `tests/test_*.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Create or update a module under src/ (e.g., src/clients/, src/analytics/, src/monitoring/).
- Add or update corresponding test files under tests/.
- If needed, update configuration or registry files.
- Run tests and ensure linter passes.

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.