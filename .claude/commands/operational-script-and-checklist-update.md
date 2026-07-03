---
name: operational-script-and-checklist-update
description: Workflow command scaffold for operational-script-and-checklist-update in Noahstark23-polybot.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /operational-script-and-checklist-update

Use this workflow when working on **operational-script-and-checklist-update** in `Noahstark23-polybot`.

## Goal

Adds or updates operational scripts and checklists to support deployment, health checks, and NO-GO gating. Ensures operational safety and deployment readiness.

## Common Files

- `scripts/*.py`
- `docs/handoff/LATEST.md`
- `.github/workflows/ci.yml`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Create or update scripts under scripts/ (e.g., smoke_test.py, check_no_go.py, clear_kill_switch.py).
- Update or add checklist documentation (e.g., docs/handoff/LATEST.md).
- If needed, update CI/CD workflow files to include new checks.
- Test scripts locally and in CI.

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.