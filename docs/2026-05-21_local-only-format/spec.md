# Spec: local-only `format` and `lint`

## Context

`sqlmesh format` is a pure text transformation — it parses each `.sql` model and audit file with SQLGlot, optionally rewrites casts or transpiles the dialect, and writes the pretty-printed result back to disk. `sqlmesh lint` is the same shape: it walks loaded model ASTs through linter rules and reports violations. Neither needs a database. But today, both run an authenticated state-sync query before doing any of their actual work.

The reason is wiring, not intent. Neither command is listed in `SKIP_LOAD_COMMANDS` or `SKIP_CONTEXT_COMMANDS` (`sqlmesh/cli/main.py:31,43`), so the CLI builds a `Context` with `load=True`. `Context.load()` then runs a state-merge block (`sqlmesh/core/context.py:678-697`) that calls `self.state_reader.get_environment(c.PROD)` to pull remote snapshots and environment statements into the local model registry. Touching `state_reader` resolves the lazy `state_sync` property, which connects to the state database and runs `get_versions` (`sqlmesh/core/context.py:609-620`). If state lives in Postgres, Snowflake, BigQuery, or any other shared backend, that's an authenticated network call. In CI, where credentials are typically not provisioned for formatting or lint jobs, this turns a purely local check into a connection-required step.

Both commands' actual code paths are clean: `Context.format()` (`context.py:1220`) uses `self._models`, `self._audits`, `target._path`, `target.formatting`, `target.dialect`, and `self.config_for_node()` — nothing else. `Context.lint_models()` (`context.py:3210`) iterates over `self._models.values()` and runs each through `Linter.lint_model`; the four places the built-in rules touch `self.context` (`linter/rules/builtin.py:140,161,185,247`) all read from local in-memory data (`models_with_tests`, `get_model`, `extract_references_from_query`, `self.context.path`). Neither command, nor its helpers, ever references `state_sync`, `state_reader`, `engine_adapter`, or `snapshots`. The state connection both commands open today is dead weight from `Context.load()`'s remote-snapshot merge.

## Goals

- `sqlmesh format` and `sqlmesh lint` run end-to-end without opening any network connection to the state-sync backend, any other database, or any external service, and without requiring any authentication.
- Neither command requires state credentials, warehouse credentials, or any other auth material to be present in the environment or config. A CI job with zero secrets provisioned can run both successfully against a real project.
- The fix is one focused mechanism — a single new `Context` parameter and a single new CLI tuple — not a per-command refactor.
- Existing behavior of every other CLI command is unchanged. No regression in `plan`, `run`, `render`, `diff`, etc.
- The new behavior is covered by tests that fail without the fix (e.g., constructing a `Context` against a project whose state connection points at an unreachable database, and asserting `format` and `lint` still succeed).

## Non-goals

- Making `dag` local-only. It's a plausible candidate but its `--select-model` path constructs a `Selector` (`context.py:2947`) that evaluates `self.state_reader` at construction time, even though `Selector.expand_model_selections` never uses it. Fixing that cleanly would require deferring state-reader resolution inside `Selector` and reviewing every other call site of `_new_selector`. We're not doing it here.
- Making `render`, `rewrite`, `create_test`, `test`, or any other CLI command local-only. They have real state or warehouse dependencies in their actual code paths, not just in `Context.load()`.
- Refactoring `Context.load()` beyond gating the existing state-merge block. The block's logic is unchanged — only whether it runs.
- Suppressing anonymized analytics or any other implicit network activity on CLI invocation. That's governed by the existing `disable_anonymized_analytics` setting and is orthogonal to this change.
- Adding an env-var or config knob to restore the old behavior for `format`/`lint`. The spec commits to the new behavior, consistent with how `SKIP_LOAD_COMMANDS` and `SKIP_CONTEXT_COMMANDS` are handled today (`cli/main.py:31,43`).
- Changing the public Python API beyond the one new `Context(load_state=...)` parameter. No new method, no new module, no rename.

## Key discoveries

- `format` and `lint` are wired through `Context` with `load=True`. The CLI's two existing escape tuples don't cover them: neither command appears in `SKIP_LOAD_COMMANDS` or `SKIP_CONTEXT_COMMANDS` (`sqlmesh/cli/main.py:31,43`).
- The state-sync connection both commands open is opened exclusively inside `Context.load()`'s remote-snapshot merge block (`sqlmesh/core/context.py:678-697`). Two calls touch state: `self.state_reader.get_environment(c.PROD)` at line 678, and the snapshot/environment-statement merge gated by `if any(self._projects):` at lines 681-697.
- Reading `self.state_reader` is the trigger. The property returns `self.state_sync` (`context.py:621`), which lazily instantiates the state backend, runs `get_versions(validate=False)`, optionally migrates, and runs `get_versions()` again (`context.py:609-619`). All of that is one authenticated round trip to the configured state database.
- The engine adapter is genuinely lazy. `create_engine_adapter()` wraps the connection in a `ConnectionPool` that doesn't invoke its factory until a cursor is requested (`sqlmesh/utils/connection_pool.py:342`, `sqlmesh/core/engine_adapter/base.py:194`). Neither `Context.format()` nor `Context.lint_models()` ever calls a method that requests a cursor, so no warehouse connection is opened by these commands' own code paths — only by `load()`'s state-merge block.
- `Context.format()` (`context.py:1220`) uses `self._models`, `self._audits`, `target._path`, `target.formatting`, `target.dialect`, and `self.config_for_node()`. Verified by reading the method end to end.
- `Context.lint_models()` (`context.py:3210`) iterates `self._models.values()` and delegates to per-project `Linter.lint_model`. The four places the built-in rules access `self.context` (`linter/rules/builtin.py:140,161,185,247`) are all reads of local in-memory data: `models_with_tests`, `get_model`, `extract_references_from_query`, and `self.context.path`. No rule references `state_sync`, `state_reader`, `engine_adapter`, or `snapshots` (verified by `rg` across `sqlmesh/core/linter/`).
- `update_model_schemas` in the `update_schemas=True` branch of `load()` (`context.py:723`) is local-only — it constructs a `MappingSchema` against `self._models` and uses `OptimizedQueryCache` on the local filesystem. No engine adapter involvement.
- The repo already has the right shape for the change. `cli/main.py` has two precedents — `SKIP_LOAD_COMMANDS` and `SKIP_CONTEXT_COMMANDS` — for marking subcommands that need lighter Context construction, and both flow through to `Context.__init__` via plain boolean parameters. Adding a third tuple with the same shape matches the existing pattern.

## Proposed approach

Add a third boolean parameter, `load_state: bool = True`, to `GenericContext.__init__`. Store it on the instance. In `Context.load()`, guard the existing remote-snapshot merge block (`context.py:678-697`) on this flag — when `load_state` is `False`, skip the two `self.state_reader.get_environment(c.PROD)` calls and the snapshot/environment-statement merge between them. The flag is meaningful only when `load=True`; when `load=False`, `load()` isn't called and the flag is inert. Document that in the docstring.

In `sqlmesh/cli/main.py`, add a new constant alongside the existing two:

```python
LOCAL_ONLY_COMMANDS = ("format", "lint")
```

In `cli(...)`, after the existing `SKIP_CONTEXT_COMMANDS` and `SKIP_LOAD_COMMANDS` checks, set `load_state = False` when `ctx.invoked_subcommand in LOCAL_ONLY_COMMANDS`. Pass it through to the `Context(...)` constructor. The existing `load=True` path is unchanged for these commands — models are still parsed, schemas still updated, audits still loaded — only the state-merge step is skipped.

No other code changes. `Context.format()`, `Context.lint_models()`, the linter rules, and `update_model_schemas` are already local-only; this spec just removes the unnecessary state-merge that `load()` runs before they get a chance to execute.

Tests: add a regression test that constructs a `Context` against a project whose state connection points at an unreachable database (e.g., a Postgres `ConnectionConfig` pointing at `localhost:1` or a similar guaranteed-fail target), with `load_state=False`, and asserts that `format` and `lint` complete successfully. Also add a CLI-level test that invokes `sqlmesh format` and `sqlmesh lint` against the same project and asserts neither opens a state connection — likely by patching `EngineAdapterStateSync.get_versions` and asserting it's never called.

## Key decisions & tradeoffs

- **`load_state: bool = True` over an enum.** Matches the existing `load: bool = True` shape on `Context`. The nonsensical combination `load=False, load_state=True` is inert (load() never runs), so an enum would add abstraction without preventing a real bug.
- **New `LOCAL_ONLY_COMMANDS` tuple over reusing `SKIP_LOAD_COMMANDS`.** `format` and `lint` *do* need models loaded; they just don't need state merged in. Reusing `SKIP_LOAD_COMMANDS` would require a parallel "models-only loader" and duplicate `load()`'s parsing logic. A third tuple is one line and reads exactly as the existing pattern.
- **Gate the state-merge block, don't extract it.** Pulling lines 678-697 of `context.py` into a private method would be cleaner in isolation but would touch every caller of `load()` and obscure the diff. A single `if self._load_state and any(self._projects):` guard keeps the diff small and the change reviewable.
- **No escape hatch.** Consistent with how `SKIP_LOAD_COMMANDS` and `SKIP_CONTEXT_COMMANDS` are handled. The state-merge work is unused by `format`/`lint`, so there's no scenario where restoring it would be correct behavior for these commands.
- **`Context(load_state=...)` is part of the public API.** It's a Python-level parameter on `GenericContext`, so programmatic users of SQLMesh can construct a local-only Context too. Cost is zero (one kwarg, one docstring line) and the symmetry with `load` is worth it.
- **`load_state=False` is set by the CLI, not inferred at runtime.** We could try to detect at runtime whether a command will touch state, but that's a much larger change. The CLI knows statically which commands are local-only; a constant tuple is the right place for that knowledge.

## Risks / unknowns

- **Test fixtures that assume state is queried during load.** Existing tests that build a `Context` with `load=True` may rely on remote snapshots being merged in. Our change defaults `load_state=True`, so these tests should be unaffected — but if any test runs `format` or `lint` through the CLI runner against a project that depends on cross-project snapshot merging for the formatted/linted models to resolve, that test may need a fixture adjustment. Likely none exist, but worth confirming during implementation.
- **`update_model_schemas` cache state.** `load(update_schemas=True)` runs after the state-merge block. When the block is skipped, the local `MappingSchema` won't contain remote snapshot columns. For `format` and `lint` this is fine — neither needs resolved column types from remote projects — but if a linter rule is ever added later that relies on cross-project column resolution, it would silently produce different results under `load_state=False`. Note in the linter rule contract, but no code change needed today.
- **CLI test isolation.** Asserting "no state connection was opened" is easier to assert by patching than by mocking the network. The cleanest seam is `EngineAdapterStateSync.get_versions` — patching it to raise should be enough; we then assert `format`/`lint` succeed and the patched method is never called. If that seam turns out to be wrong (e.g., a different method gets called first), we'll adjust during implementation.
- **Multi-project monorepos.** The state-merge block's purpose is to import remote snapshots for *other* projects when the current load covers only a subset (`context.py:681-697`, gated on `any(self._projects)`). For `format`/`lint`, this means a model that depends on a model defined in a sibling project not currently being loaded won't have its upstream resolved. That's already the behavior for any model `format`/`lint` touches — they operate per-file, not on rendered downstream queries — so there should be no observable difference. Flagging in case a reviewer raises it.
- **Snowflake default-auth state connections.** `PostgresConnectionConfig` accepts `password=None` from a missing `{{ env_var() }}` (Pydantic coercion), so a Postgres state config validates with no secrets and the gate then prevents the connection. `SnowflakeConnectionConfig._validate_authenticator` (`sqlmesh/core/config/connection.py:633-638`) is stricter: under default authentication it raises `ConfigError("User and password must be provided")` at config-load time if both are missing. That validation runs before our `load_state` gate can take effect, so a project whose state lives in Snowflake with default-auth still requires user/password env vars to be set (any non-empty values — they won't be used). This is pre-existing Snowflake validator behavior, not something this change introduces, but it's the one realistic CI configuration where "zero secrets" doesn't hold. Workarounds: use the Snowflake key-pair or OAuth authenticators (which have their own validators with different requirements), or set placeholder env vars in the CI job.

## Dependencies

- Research docs:
  - `docs/2026-05-21_local-only-format/01_format_command.md` — how `format` works end to end.
  - `docs/2026-05-21_local-only-format/02_cli_bootstrap.md` — what runs on any CLI invocation, where state is touched.
  - `docs/2026-05-21_local-only-format/03_contributing.md` — DCO sign-off, `make style`, `make fast-test`, PR template, CI matrix.
  - `docs/2026-05-21_local-only-format/spec_notes.md` — discovery and planning context that didn't fit in the spec: full CLI command audit, why `dag` was dropped, mechanism alternatives considered, verification methodology.
- No external library or service dependencies. Pure internal change.
- No other in-flight specs or branches to coordinate with — the working branch `fix/local-only-format` is fresh off `main`.
