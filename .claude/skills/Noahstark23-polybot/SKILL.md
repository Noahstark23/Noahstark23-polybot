```markdown
# Noahstark23-polybot Development Patterns

> Auto-generated skill from repository analysis

## Overview

This skill teaches you how to contribute to the `Noahstark23-polybot` Python codebase, which is organized around feature gates, modular extensions, and robust operational practices. You'll learn the project's coding conventions, how to implement new features or modules, extend functionality with tests, and maintain operational scripts. The repository emphasizes clear workflow steps, test-driven development, and operational safety.

## Coding Conventions

- **File Naming:** Use `snake_case` for all Python files and modules.
  - Example: `trade_engine.py`, `config_loader.py`
- **Import Style:** Prefer relative imports within the `src/` directory.
  - Example:
    ```python
    from ..utils.config import get_config
    ```
- **Export Style:** Use named exports; avoid wildcard imports.
  - Example:
    ```python
    # src/engine/arbitrage.py
    def run_arbitrage(...):
        ...
    ```
- **Commit Patterns:** Commits often use prefixes like `f0`, `f1`, `f2` to indicate feature gates, followed by a concise description.
  - Example: `f1: add authentication middleware`

## Workflows

### Feature Gate Implementation
**Trigger:** When a new milestone or functional gate is being developed and integrated (e.g., F1: auth, F2: arbitrage, F3: execution).  
**Command:** `/new-feature-gate`

1. Create or update main implementation modules under `src/` (e.g., new engine, service, or manager).
2. Update or add configuration logic in `src/utils/config.py`.
3. Add or update related database models or registry files if needed.
4. Write or update supporting scripts or monitoring/analytics modules.
5. Add or update corresponding tests under `tests/` (unit and integration).
6. Update runner or service registration logic if the new feature requires orchestration (e.g., `src/runner.py`).
7. Ensure `ruff`/linter passes.

**Example:**
```python
# src/engine/arbitrage.py
def run_arbitrage(...):
    ...

# src/utils/config.py
def get_arbitrage_config():
    ...

# tests/test_arbitrage.py
def test_run_arbitrage():
    ...
```

---

### Test-Driven Module Extension
**Trigger:** When extending system capabilities (e.g., adding a new client, analytics, or monitoring module).  
**Command:** `/add-module-with-tests`

1. Create or update a module under `src/` (e.g., `src/clients/`, `src/analytics/`, `src/monitoring/`).
2. Add or update corresponding test files under `tests/`.
3. If needed, update configuration or registry files.
4. Run tests and ensure linter passes.

**Example:**
```python
# src/clients/new_client.py
class NewClient:
    ...

# tests/test_new_client.py
def test_new_client_behavior():
    ...
```

---

### Operational Script and Checklist Update
**Trigger:** When operational requirements change or new deployment gates/checks are needed.  
**Command:** `/update-ops-scripts`

1. Create or update scripts under `scripts/` (e.g., `smoke_test.py`, `check_no_go.py`, `clear_kill_switch.py`).
2. Update or add checklist documentation (e.g., `docs/handoff/LATEST.md`).
3. If needed, update CI/CD workflow files to include new checks (e.g., `.github/workflows/ci.yml`).
4. Test scripts locally and in CI.

**Example:**
```python
# scripts/smoke_test.py
def main():
    ...

if __name__ == "__main__":
    main()
```

---

## Testing Patterns

- **Test Files:** Located under `tests/` and named with the pattern `test_*.py`.
- **Test Structure:** Each test file targets a single module or feature.
- **Framework:** No explicit framework detected; use standard Python `unittest` or `pytest` conventions.
- **Example:**
    ```python
    # tests/test_arbitrage.py
    def test_run_arbitrage():
        result = run_arbitrage(...)
        assert result is not None
    ```

## Commands

| Command                | Purpose                                                      |
|------------------------|--------------------------------------------------------------|
| /new-feature-gate      | Start a new feature gate implementation workflow             |
| /add-module-with-tests | Add or extend a module with corresponding tests              |
| /update-ops-scripts    | Update operational scripts and deployment checklists         |
```
