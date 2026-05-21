# Plan: local-only `format` and `lint`

## Goal

Make `sqlmesh format` and `sqlmesh lint` runnable with no state-sync credentials and no reachable database, by adding a `load_state: bool = True` parameter to `GenericContext` that gates the remote-snapshot merge block in `Context.load()`, and a `LOCAL_ONLY_COMMANDS = ("format", "lint")` tuple in `cli/main.py` that flips it to `False` for those two commands. Everything else stays as it is.

## Overall validation

The PR is done when all of these are true:

- A `Context` constructed against a project whose state connection points at an unreachable host completes both `context.format()` and `context.lint_models()` without raising when `load_state=False` is passed.
- `sqlmesh format` and `sqlmesh lint` invoked through the CLI runner against a project whose `config.yaml` declares an unreachable state connection complete with `exit_code == 0`. `EngineAdapterStateSync.get_versions` is never called during the run (verified by `mocker.patch` with `assert_not_called()`).
- A guard-rail CLI test confirms `sqlmesh plan` against the same project *does* call `EngineAdapterStateSync.get_versions` — the new tuple didn't accidentally cover commands it shouldn't.
- `make py-style` is clean. Ruff, mypy, and the migration-sequence check all pass.
- `make fast-test` is green (covers the new Context-level tests).
- The targeted CLI test passes when run directly (`pytest tests/cli/test_cli.py::test_format_runs_without_state -v` and the same for lint). `make slow-test` is green overall.
- Every commit on the branch carries a `Signed-off-by:` trailer per the DCO bot. `git log --format='%(trailers:key=Signed-off-by)' main..HEAD` shows one for each commit.

## Key discoveries & assumptions

**Facts** (each verified during exploration):

- The state-merge block in `Context.load()` is two consecutive `if any(self._projects):` guards at `sqlmesh/core/context.py:677-699`. The first loads environment statements from prod (lines 677-683); the second populates the local `uncached` set and merges remote snapshots into `self._models` / `self._standalone_audits` (lines 685-699). Both touch state via `self.state_reader.get_environment(c.PROD)`.
- `GenericContext.__init__` lives at `sqlmesh/core/context.py:376` with the existing `load: bool = True` parameter at line 385. `self.load()` is called at the very end of `__init__` if `load` is true (line 473).
- The CLI top-level group `cli(...)` lives at `sqlmesh/cli/main.py:94`. The `SKIP_CONTEXT_COMMANDS` and `SKIP_LOAD_COMMANDS` tuples are at `cli/main.py:31` and `cli/main.py:43`. The Context construction call is at `cli/main.py:132-137`.
- Existing format tests live at `tests/core/test_format.py` (no module-level mark — runs under `fast`). They construct a `Context` directly with `Config()` and `tmp_path` (e.g. `test_format_files` at line 14).
- Existing linter tests live at `tests/core/linter/test_builtin.py` (also no module-level mark — fast). They use the `copy_to_temp_path` fixture from `tests/conftest.py:565` against `examples/sushi`.
- CLI runner tests live at `tests/cli/test_cli.py`. The module is marked `pytestmark = pytest.mark.slow` at line 23. The runner fixture is at line 31, returning `CliRunner(env={"COLUMNS": "80"})`. `create_example_project` helper is at line 36.
- The `Linter` is built per-project in `Context.load()` at `context.py:670-674` before the state-merge block. Linters don't depend on the state-merge having run.
- `format` and `lint` Click handlers are at `sqlmesh/cli/main.py:343-380` and `sqlmesh/cli/main.py:1168-1185` respectively. Neither needs argument changes.

**Decisions resolved during implementation:**

- The plan originally suggested configuring an unreachable Postgres state connection. This doesn't work in the dev environment because `psycopg2` is not installed; `PostgresConnectionConfig` runs `_get_engine_import_validator("psycopg2", ...)` at config-validation time (`sqlmesh/core/config/connection.py:1424`) and raises before our code path is reached. Implementation uses the plan's documented fallback: patch `sqlmesh.core.state_sync.db.facade.EngineAdapterStateSync.get_versions` with `side_effect=RuntimeError(...)` and assert it is never called.
- The state-merge blocks are guarded by `if any(self._projects):`, where `self._projects = {config.project for config in self.configs.values()}`. `Config.project` defaults to `""` (`sqlmesh/core/config/root.py:142`), and `any({""})` is `False` — so a `Config()` literal short-circuits the guard before the new `self._load_state` term is evaluated, making the test vacuous. The Context-level tests therefore set a non-empty `project` (`Config(project="local_only")` in the format test; an inline rewrite of sushi's `config.py` to add `project="sushi"` in the linter test) to ensure the guard is actually exercised.
- No existing test exercised the "format/lint succeed despite broken state" path (verified by `rg 'format.*state\|lint.*state' tests/`).

## Existing patterns & conventions to follow

**Code patterns:**

- **CLI command-class tuples.** `SKIP_LOAD_COMMANDS` and `SKIP_CONTEXT_COMMANDS` at `sqlmesh/cli/main.py:31,43` are plain module-level string tuples; the `cli(...)` group reads them inside `if ctx.invoked_subcommand in ...` checks. The new `LOCAL_ONLY_COMMANDS` follows the same shape and is checked in the same block (`cli/main.py:120-123`).
- **Boolean construction parameters.** `GenericContext.__init__` (`context.py:376-389`) uses plain `bool` parameters with `True`/`False` defaults — e.g. `load: bool = True`. Add `load_state: bool = True` in the same style, with a matching docstring entry in the class docstring at `context.py:362-373`.
- **Private instance storage.** Constructor flags are stored as private attributes prefixed with `_` (e.g. `self._loaded`, `self._loaders`, `self._models`). Store the new flag as `self._load_state`.
- **Guard composition.** Add to an existing `if`-guard with `and` rather than wrapping the block in a new outer `if`. Matches the style at `context.py:677` and keeps the diff minimal.

**Test patterns:**

- **Fast Context-level tests** construct a `Context` from a `tmp_path` plus a `Config(...)` literal (`tests/core/test_format.py:14-42`). No fixtures beyond `tmp_path` and `mocker` are needed. Tests are top-level `def test_*` functions, not classes.
- **Linter tests** use the `copy_to_temp_path` fixture (`tests/conftest.py:565`) and patch the project's `config.py` text to enable rules (`tests/core/linter/test_builtin.py:7-46`). The same fixture can be reused for the local-only lint test by writing a config with an unreachable state connection.
- **CLI tests** use the session-scoped `runner` fixture and the `create_example_project` helper (`tests/cli/test_cli.py:31-60`). The module-level `pytestmark = pytest.mark.slow` (line 23) inherits to every test added there.
- **Mocker assertions.** `tests/cli/test_cli.py` already uses `MagicMock` patterns. For "method never called", `mocker.patch(...)` followed by `mock.assert_not_called()` is the idiomatic shape in this repo (`rg "assert_not_called" tests/` shows ~20 hits).

**Contribution conventions:**

- **DCO sign-off.** Every commit needs `Signed-off-by:` — produced by `git commit -s`. The bot blocks merge otherwise. The docs commit on this branch already carries one.
- **Commit messages.** Recent commits use lowercase semantic prefixes (`fix:`, `chore:`, `docs:`) with imperative subject lines — see `git log --oneline -20` for the pattern. Combined with the global AGENTS.md format: subject ≤50 chars, body wrapped at 72 explaining why, trailers `Coding-Agent: pi` / `Model: <slug>` / `Signed-off-by: ...`.
- **Pre-commit style.** `make py-style` runs ruff (check + format, line length 100), mypy, and the migration-sequence check. Run before each commit. If only Python changed, `make py-style` is faster than `make style` (which also runs prettier/eslint).
- **Test selection.** `make fast-test` runs the fast suite plus isolated groups. CLI tests in `tests/cli/test_cli.py` are slow-marked at the module level and require `make slow-test` for the full suite, but the targeted CLI test can be invoked directly via `pytest tests/cli/test_cli.py::<name>` during development.
- **No new files unless necessary.** Per `03_contributing.md`, new Python files need the `# SPDX-License-Identifier: Apache-2.0` header. This plan adds no new source files — only modifies existing ones — so no header work needed.

## Out of scope

- **`dag` and any other CLI command.** Even though the `load_state` mechanism would make some of `dag` local-only, its `--select-model` path constructs a `Selector` that evaluates `self.state_reader` at construction time (`context.py:2947`). Fixing that requires touching `Selector` and every other `_new_selector` call site — separate change, not in this PR.
- **Refactoring `Context.load()` structure.** No extraction of the state-merge block into a private method, no rearrangement of the load sequence. The change is `and self._load_state` added to two existing guards.
- **Suppressing analytics or any other implicit network activity.** Anonymized analytics still fire on `format` and `lint` invocations after this change. That's governed by the existing `disable_anonymized_analytics` config knob and is orthogonal.
- **Tests for "command X still hits state."** No new test asserts that `plan`/`diff`/`run`/etc. continue to touch state. The default `load_state=True` preserves their behavior; the existing test suite already covers them.
- **Documentation updates.** The user-facing behavior change is "CI now works without state credentials." Discoverable from the PR description and release notes. No `docs/` page change in this PR.
- **Backporting or version-gating.** This is a forward-only change on `main`. No release-branch backport, no `@deprecated` shim, no feature flag.

## Tasks

Two tasks, two commits, one PR.

---

### Task 1: Add `load_state` to Context and gate the state-merge block

**Purpose:** Introduce the mechanism that lets a caller construct a `Context` that loads models but never touches the state backend.

**Files:**
- Modify: `sqlmesh/core/context.py`
- Modify: `tests/core/test_format.py`
- Modify: `tests/core/linter/test_builtin.py`

**Test cases** (red first, then implementation):

`tests/core/test_format.py` (fast):
- *`test_format_without_state_load`*: Patch `EngineAdapterStateSync.get_versions` to raise `RuntimeError`. Place one `.sql` model under `models/`. Construct `Context(paths=tmp_path, config=Config(project="local_only"), load_state=False)` (non-empty `project` so `any(self._projects)` is truthy and the gate is exercised). Call `context.format(check=True)`. Assert the patched mock has `assert_not_called()`. Verified non-vacuous by flipping to `load_state=True` and confirming the test fails with the patched `RuntimeError`.

`tests/core/linter/test_builtin.py` (fast):
- *`test_lint_without_state_load`*: Using `copy_to_temp_path("examples/sushi")`, rewrite the sushi `config.py` to add `project="sushi"` to the `Config(...)` call (so `any(self._projects)` is truthy). Patch `EngineAdapterStateSync.get_versions` to raise. Construct `Context(paths=[sushi_path], load_state=False)`. Call `context.lint_models(raise_on_error=False)`. Assert the patched mock has `assert_not_called()`. Verified non-vacuous the same way as the format test.

**Implementation outline:**

In `sqlmesh/core/context.py`:
- Extend the `GenericContext` class docstring args block at lines 356-368 with a one-line entry for the new parameter: a brief description noting it gates the remote-state merge inside `load()` and is only meaningful when `load=True`.
- Add `load_state: bool = True` to `GenericContext.__init__` at the *end* of the parameter list (after `selector`). Placing it last avoids shifting any existing positional arguments for callers outside this repo who may pass `users`, `config_loader_kwargs`, or `selector` positionally.
- Store as `self._load_state` on the instance during `__init__`, alongside the other private attributes (`self._loaded`, `self._loaders`, etc., around lines 396-415).
- In `Context.load()`, tighten the two `if any(self._projects):` guards at lines 677 and 685 by ANDing `self._load_state` into each predicate. No other changes to `load()` body. `update_schemas`, `Linter.from_rules`, and the `analytics.collector.on_project_loaded` call are unaffected.

**Verification:**
- Automated: `pytest tests/core/test_format.py::test_format_without_state_load tests/core/linter/test_builtin.py::test_lint_without_state_load -v` — new tests pass.
- Automated: `pytest tests/core/test_format.py tests/core/linter/ -v` — existing tests in these files still pass.
- Automated: `make py-style` — ruff, mypy, migration-sequence all clean.
- Manual: read the diff of `context.py` and confirm the only non-test changes are: one docstring line, one constructor parameter, one attribute assignment, two `and` insertions.

**Commit:** `git commit -s` with subject `feat: add load_state flag to Context` (37 chars). Trailers: `Coding-Agent: pi`, `Model: <slug>`, `Signed-off-by: ...`.

---

### Task 2: Wire `LOCAL_ONLY_COMMANDS` in the CLI

**Purpose:** Make `sqlmesh format` and `sqlmesh lint` construct their `Context` with `load_state=False`, completing the user-facing behavior.

**Files:**
- Modify: `sqlmesh/cli/main.py`
- Modify: `tests/cli/test_cli.py`

**Test cases** (red first, then implementation):

`tests/cli/test_cli.py` (slow, via module-level `pytestmark`):

All three tests share a setup factored into `_setup_local_only_project(tmp_path, mocker)`:
1. `create_example_project(tmp_path, template=ProjectTemplate.EMPTY)` to scaffold a real project.
2. Prepend `project: cli_test\n\n` to the generated `config.yaml` so `Config.project` is non-empty and `any(self._projects)` is truthy. Without this, the existing outer guard short-circuits regardless of `self._load_state` and the tests are vacuous.
3. Write one `.sql` model under `models/` so the CLI doesn't short-circuit with "no models found".
4. `mocker.patch("sqlmesh.core.state_sync.db.facade.EngineAdapterStateSync.get_versions", side_effect=RuntimeError("state should not be accessed"))`. (No `state_connection: postgres` block in `config.yaml` — same `psycopg2` problem as Task 1; YAML validation through Pydantic fires the import validator. The patch is the only mechanism.)

- *`test_format_runs_without_state`*: Run setup. Invoke `runner.invoke(cli, ["--paths", str(tmp_path), "format"])` (no `--check` — lets format write in place, exit_code 0 whenever the call returned). Assert `result.exit_code == 0` and `mock.assert_not_called()`.
- *`test_lint_runs_without_state`*: Run setup. Invoke `runner.invoke(cli, ["--paths", str(tmp_path), "lint"])`. Assert `result.exit_code == 0` and `mock.assert_not_called()`.
- *`test_plan_still_loads_state`* (required guard-rail): Run setup. *Additionally* spy on `Context.__init__` via `mocker.spy(Context, "__init__")`. Invoke `runner.invoke(cli, ["--paths", str(tmp_path), "plan"], input="n\n")`. Assert that the spy was called and that every call's `load_state` kwarg was `True` (defaults to `True` when omitted). This is stronger than asserting `mock.called` on `get_versions` — a regression where someone added `"plan"` to `LOCAL_ONLY_COMMANDS` would still hit state later via `context.plan(...)`, so `get_versions.called` alone wouldn't catch it. The spy proves the Context constructor itself was called with `load_state=True` for `plan`. Verified by temporarily appending `"plan"` to `LOCAL_ONLY_COMMANDS`; the spy-based assertion fails.

**Implementation outline:**

In `sqlmesh/cli/main.py`:
- Add a new module-level constant `LOCAL_ONLY_COMMANDS = ("format", "lint")` immediately after `SKIP_CONTEXT_COMMANDS` (line 43), matching the surrounding tuple style.
- Inside `cli(...)` (around line 117), compute `load_state = ctx.invoked_subcommand not in LOCAL_ONLY_COMMANDS` *outside* the `if len(paths) == 1:` block. Unlike `SKIP_LOAD_COMMANDS` (whose `load = False` toggle is inside the single-path conditional), `load_state` must apply regardless of how many `--paths` were provided — multi-project monorepo invocations of `format`/`lint` need to be local-only too.
- Pass `load_state=load_state` as an additional keyword argument in the `Context(...)` call at lines 132-137.

**Verification:**
- Automated: `pytest tests/cli/test_cli.py::test_format_runs_without_state tests/cli/test_cli.py::test_lint_runs_without_state tests/cli/test_cli.py::test_plan_still_loads_state -v` — all three new tests pass.
- Automated: `pytest tests/cli/test_cli.py::test_format_leading_comma_default -v` — existing format CLI test still green.
- Automated: `make py-style` — clean.
- Automated: `make fast-test` — full fast suite green. Confirms Task 1 didn't regress and Task 2 didn't disturb fast tests.
- Manual: from inside the repo, run `sqlmesh --paths examples/sushi format --check` and `sqlmesh --paths examples/sushi lint` against a real sushi project. They should succeed. (Sushi uses DuckDB so this won't *prove* offline behavior, but it sanity-checks the wiring.)
- Manual: read the diff of `cli/main.py` and confirm the only changes are: one new tuple, one new variable, one `if`-check, one `load_state=` kwarg in the `Context(...)` call.

**Commit:** `git commit -s` with subject `cli: run format and lint without state` (38 chars). Trailers: `Coding-Agent: pi`, `Model: <slug>`, `Signed-off-by: ...`.
