# Spec notes

Context captured during the discovery and planning conversation that didn't make it into `spec.md`. The spec is the source of truth for what we're building; these are the things a reader would benefit from if the conversation were lost — the audit table, the design alternatives we rejected, the latent issues we noticed, and the verification methodology that backed the spec's confidence.

## CLI command survey

When the user asked "are there other commands that should be local-only?", I worked through every `@cli.command` in `sqlmesh/cli/main.py` and classified each by whether it touches state and whether it touches the warehouse. The table below captures the full audit. Only `format` and `lint` made it into this spec; everything else is either already handled (existing skip tuples), genuinely needs state/warehouse, or has an awkward edge case that ruled it out.

| Command | Current Context mode | Touches state? | Touches warehouse? | Local-only candidate? |
|---|---|---|---|---|
| `init` | `SKIP_CONTEXT_COMMANDS` | No | No | Already covered |
| `ui` | `SKIP_CONTEXT_COMMANDS` | No | No | Already covered |
| `clean` | `SKIP_LOAD_COMMANDS` | No (clears caches) | No | Already effectively local |
| `create_external_models` | `SKIP_LOAD_COMMANDS` | No | **Yes** (queries DB for schemas) | No |
| `destroy` | `SKIP_LOAD_COMMANDS` | Yes | Yes | No |
| `environments` | `SKIP_LOAD_COMMANDS` | Yes | No | No |
| `invalidate` | `SKIP_LOAD_COMMANDS` | Yes | No | No |
| `janitor` | `SKIP_LOAD_COMMANDS` | Yes | Yes | No |
| `migrate` | `SKIP_LOAD_COMMANDS` | Yes | No | No |
| `rollback` | `SKIP_LOAD_COMMANDS` | Yes | No | No |
| `run` | `SKIP_LOAD_COMMANDS` | Yes | Yes | No |
| `table_name` | `SKIP_LOAD_COMMANDS` | Yes (looks up env) | No | No |
| **`format`** | full load | No | No | **Yes** — this spec |
| **`lint`** | full load | No | Possibly via rules — verified no | **Yes** — this spec |
| `dag` | full load | No without `--select-model`; yes with | No | Edge case — see below |
| `render` | full load | Yes (`self.snapshots`, deployability index) | Yes (seed engine adapter) | No |
| `evaluate` | full load | Yes | Yes | No |
| `diff` | full load | Yes (env diff) | No | No |
| `plan` | full load | Yes | Yes | No |
| `create_test` | full load | No | Yes (executes input queries) | No |
| `test` | full load | No | Local test engine | No (uses warehouse-like adapter) |
| `audit` | full load | Yes | Yes | No |
| `check_intervals` | full load | Yes | No | No |
| `fetchdf` | full load | Yes | Yes | No |
| `info` | full load | Optional via `--skip-connection` | Optional | Already has skip |
| `table_diff` | full load | Yes | Yes | No |
| `rewrite` | full load | Unverified — not investigated deeply | Unverified | Out of scope |
| `dlt_refresh` | full load | No (writes models from DLT) | No | Plausible but not investigated |

`rewrite` and `dlt_refresh` are the two commands where the audit is shallow. They were filed under "out of scope" rather than "verified not a candidate". If someone picks up local-only work later, those are the first ones to check.

## Why `dag` was dropped

`dag` looked like a clean third candidate at first. `Context.render_dag` (`context.py:2170`) just writes HTML of `self.get_dag(select_models)`, and the linter rules and engine adapter aren't referenced anywhere in that path. But tracing through `Context.get_dag` (`context.py:2121`) surfaced a latent issue:

When `select_models` is provided, `get_dag` calls `self._new_selector().expand_model_selections(...)`. `Context._new_selector()` (`context.py:2947`) constructs a `Selector` with `self.state_reader` as its first argument. Reading `self.state_reader` evaluates the lazy property (`context.py:621`), which evaluates `self.state_sync` (`context.py:609-619`), which boots the state backend, runs `get_versions(validate=False)`, optionally migrates, and runs `get_versions()` again. **The act of constructing the Selector opens a state connection**, even though `Selector.expand_model_selections` (`selector.py:191+`) never actually uses the state reader — only `_load_env_models` (`selector.py:153`) does, and `get_dag` never reaches that path.

Three options were considered:

1. **Drop `dag`** — what we chose. Tightest spec. `dag` is lower-traffic than `format`/`lint` in CI.
2. **Include `dag` partially** — fix the no-`--select-model` path only, leave `dag --select-model X` broken offline. Would be a subtle, surprising behavior split for the same command. Rejected.
3. **Include `dag` fully** — also defer `state_reader` evaluation inside `Selector`. Would touch every other call site of `_new_selector` (used by `plan`, `diff`, `run`, etc., via state methods). Wider blast radius, departs from "minimal change". Rejected.

The Selector wart is real but out of scope for this PR.

## Mechanism alternatives considered

Before landing on the `load_state` kwarg, three mechanisms were on the table:

1. **`load_state` kwarg on Context, new `LOCAL_ONLY_COMMANDS` tuple in CLI** — chosen. Mirrors the existing `load: bool = True` shape and the existing `SKIP_*_COMMANDS` tuples. One kwarg, one tuple, two if-guards.
2. **Extract the state-merge block into a private `_merge_remote_state()` method, call selectively** — cleaner separation of concerns inside `Context`, but `load()` is called from `Context.__init__`, so the CLI can't directly influence whether the new method runs. We'd still need a kwarg on `__init__`, which means the same surface area plus an extracted method. No net win.
3. **Add `format`/`lint` to `SKIP_LOAD_COMMANDS` and have those commands load models themselves** — reuses existing infrastructure but requires a new `_load_models_only()` method that duplicates much of `load()`. Bigger surface area for bugs, and doesn't match how the other `SKIP_LOAD` commands work (they genuinely don't need models).

## Naming alternatives for `load_state`

The chosen name `load_state` mirrors the existing `load: bool = True`. Other names considered:

- `state` — too vague; conflicts with `state_sync` / `state_reader` properties and the `state` subcommand group.
- `require_state` — frames it as a requirement rather than an action; less symmetric with `load`.
- `offline` — describes user intent ("offline mode") but inverts polarity vs every other boolean in this area, and is broader than what we're doing (we still hit the filesystem and load models).

The user also asked whether an enum (`LoadMode.NONE` / `LoadMode.LOCAL` / `LoadMode.FULL`) would be better, since `load=False, load_state=True` is a nonsensical combination. The answer was no: that combination is inert (when `load=False`, `load()` is never called, so `load_state` has no effect), and the repo's existing pattern in this area is plain booleans (`load=True`, the two `SKIP_*` tuples). An enum would stand out as a new abstraction without preventing a real bug.

## Verification methodology

To verify a command is local-only after the fix, the procedure is:

1. Start from the `@cli.command` in `sqlmesh/cli/main.py`. Identify the `Context` method(s) it calls.
2. Read those methods in full. Recursively follow every function/method call into helpers.
3. Look for any read of: `self.state_sync`, `self.state_reader`, `self._state_sync`, `self.engine_adapter`, `self._get_engine_adapter`, `self.connection_config`, `self.snapshots`. These are the seams that boot a connection. Also look for direct calls to `.get_environment`, `.get_versions`, `.get_snapshots`, or other state-sync API methods on any object.
4. Pay attention to **property side effects**, not just method calls. `self.state_reader` is a property that boots the state backend on access — just *passing* it as an argument to another constructor triggers the connection (this is how `dag --select-model` gets caught).
5. For linter rules specifically, check what's reachable through `self.context` from rule classes. The `Rule` base class (`linter/rule.py:75`) stores `self.context = context`, so any attribute read on `self.context` from inside a rule is a code path to audit.
6. Cross-check with existing tests: do they construct a full `Context` with a live DB, or do they get away with a DuckDB stub? Tests that pass without real credentials are evidence about what's actually required at runtime — though note that DuckDB-backed state still hides the boot sequence behind a local file, so test passage isn't proof of "no state access".

A subtle trap: when a researcher reports "command X is NOT CLEAN", check whether they're describing current behavior (before the fix) or post-fix behavior. The researcher's first verification of `lint`/`dag` reported them as "NOT CLEAN" because today they're wired through `load=True` and thus hit state during `Context.load()` — true but not the question being asked. The actual question is whether the command's *own* code path (everything downstream of `load()`) reaches for state. For `lint`, it doesn't. For `dag`, it does (via Selector). That distinction is what determines whether the spec's mechanism is sufficient.

## Test seam

The spec proposes patching `EngineAdapterStateSync.get_versions` as the assertion seam. The reasoning: `get_versions` is the first state-touching method called when the lazy `state_sync` property resolves (`context.py:613`). Patching it to raise turns any accidental state access into a loud failure that the test can detect. If the implementer finds that another method gets called first in practice (e.g., during state-sync construction itself), they should adjust to patch whichever method actually fires first — the principle is "any state access raises", not specifically "`get_versions` raises".

A lighter alternative: configure the project's state connection to point at an unreachable host (`localhost:1`, or a Postgres config with an obviously-wrong port). This exercises the real connection code and produces a meaningful error if the fix regresses. Both flavors of test have value; the spec leaves room for the implementer to pick.

## `update_model_schemas` is local

The `update_schemas=True` branch of `load()` (`context.py:723`) calls `update_model_schemas(self.dag, models=self._models, cache_dir=self.cache_dir)`. This was specifically verified: `sqlmesh/core/model/schema.py:22` constructs a `MappingSchema` against the local models dict and uses `OptimizedQueryCache` (a local-filesystem cache). It never references `self.engine_adapter` or any state API. So `format` and `lint` get fully-resolved local schemas without paying for any connection.

If anyone later writes a linter rule that needs cross-project column resolution (a model imported from a sibling project that's only present in remote state), that rule would silently produce different results under `load_state=False`. Not a concern today — no such rule exists — but worth flagging if rule authors ever start reaching for remote schemas.

## What the researcher got right and where it misled

The research docs in this folder (`01_format_command.md`, `02_cli_bootstrap.md`, `03_contributing.md`) are accurate as references for the current behavior of the codebase. The first round of `lint`/`dag` verification was *also* accurate as a description of current behavior, but its verdict ("NOT CLEAN") was framed against today's wiring rather than against the proposed fix. When delegating verification work to a researcher subagent in this kind of "are these commands ready for X change?" conversation, it's worth explicitly framing the question as "given that we'll make change Y, would the command still touch state?" rather than just "does the command touch state?"
