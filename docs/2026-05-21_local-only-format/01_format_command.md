# SQLMesh Format CLI Command Reference

This document provides a technical overview and architectural reference for how the `sqlmesh format` command is defined, processed, and executed within the SQLMesh codebase.

## 1. CLI Entry Point & Click Command Definition
The `format` command is defined as a Click CLI command in the file **`sqlmesh/cli/main.py`** at lines **343–380**:

```python
@cli.command("format")
@click.argument("paths", nargs=-1)
@click.option(
    "-t",
    "--transpile",
    type=str,
    help="Transpile project models to the specified dialect.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Whether or not to check formatting (but not actually format anything).",
    default=None,
)
@click.option(
    "--rewrite-casts/--no-rewrite-casts",
    is_flag=True,
    help="Rewrite casts to use the :: syntax.",
    default=None,
)
@click.option(
    "--append-newline",
    is_flag=True,
    help="Include a newline at the end of each file.",
    default=None,
)
@opt.format_options
@click.pass_context
@error_handler
@cli_analytics
def format(
    ctx: click.Context, paths: t.Optional[t.Tuple[str, ...]] = None, **kwargs: t.Any
) -> None:
    """Format all SQL models and audits."""
    if not ctx.obj.format(**{k: v for k, v in kwargs.items() if v is not None}, paths=paths):
        ctx.exit(1)
```

The command takes an optional positional argument `paths` to limit formatting to specific files or directories, and routes all keyword arguments down to `Context.format(...)`.

---

## 2. The Execution Call Chain
When a user executes `sqlmesh format` in the terminal, the execution traverses the following call chain:

1. **`sqlmesh/cli/main.py` (Line 343)**: The Click CLI parses options/arguments and invokes the `format(...)` command handler.
2. **`sqlmesh/cli/main.py` (Line 377)**: The CLI command delegates execution to the core context format method by calling `ctx.obj.format(...)`. Here, `ctx.obj` is an instance of the `Context` class.
3. **`sqlmesh/core/context.py` (Line 1220)**: `Context.format(...)` performs the orchestration:
   - Filters down models and audits from the loaded context (`self._models.values()` and `self._audits.values()`) that are physical `.sql` files on disk (and match specified `paths` filters, if any).
   - Skips targets where `target.formatting is False` (configured in model/audit metadata).
   - Iterates through the files, opening each one to read its content as `before` (Line 1247).
   - Calls `self._format(target, before, ...)` to generate the newly formatted content as `after`.
4. **`sqlmesh/core/context.py` (Line 1280)**: `Context._format(...)` parses and delegates rendering:
   - Calls `parse(before, default_dialect=self.config_for_node(target).dialect)` to tokenize and parse the SQL file into a list of SQLGlot AST expressions (defined in `sqlmesh/core/dialect.py` at line 907).
   - If transpilation is requested via `--transpile`, it updates the model's/audit's metadata dialect in-place within the first AST expression (the `MODEL` or `AUDIT` metadata block) (Lines 1290–1294).
   - Resolves formatting configuration settings from the node's path config: `format_config = self.config_for_node(target).format` (Line 1296).
   - Calls `format_model_expressions(expressions, ...)` to format the parsed expressions.
5. **`sqlmesh/core/dialect.py` (Line 754)**: `format_model_expressions(...)` performs the pretty-printing:
   - If `rewrite_casts` is set (or configured by default), runs a recursive AST transformer (`cast_to_colon`) to rewrite `exp.Cast` AST nodes as double-colon cast nodes (`DColonCast` / `::`), unless they have custom dialect arguments (Lines 771–796).
   - Executes `.sql(pretty=True, dialect=dialect, **kwargs)` on each AST expression to generate pretty-printed SQL string representations, forwarding general SQLGlot generator options.
   - Joins multiple statements together with double newlines and semicolons (`";\n\n"`).
6. **`sqlmesh/core/context.py` (Lines 1259–1262)**:
   - If the `--check` flag is active, compares `before` and `after`, and registers mismatched paths in `unformatted_file_paths` without modifying the disk.
   - Otherwise, seeks back to `0`, writes `after` to the file, and truncates.

---

## 3. Command Options & Configuration
The formatting CLI command accepts the following CLI arguments and flags:

### Primary CLI Flags:
* **`paths`** (Positional, `nargs=-1`): Limits formatting check or update to specified file or directory paths.
* **`-t`, `--transpile TEXT`**: Transpiles models/audits to a specific target dialect (e.g., `snowflake`, `bigquery`), writing both formatted SQL and updated metadata back to the source files.
* **`--check`**: Evaluates formatting without modifying files. Exits with code `1` if any file needs reformatting.
* **`--rewrite-casts / --no-rewrite-casts`**: Toggles rewriting standard SQL `CAST(x AS TYPE)` syntax to the shorter `x::TYPE` syntax.
* **`--append-newline`**: Ensures a trailing newline is appended to the end of every formatted file.

### Common SQLGlot Generator Flags (defined under `@opt.format_options` decorator in `sqlmesh/cli/options.py:60`):
* **`--normalize`**: Normalizes all unquoted identifiers to lowercase.
* **`--pad INTEGER`**: Determines padding size (spaces) used in formatting.
* **`--indent INTEGER`**: Determines indentation size (spaces) used in formatting.
* **`--normalize-functions TEXT`**: Formats function names to a specific case (`'upper'` or `'lower'`).
* **`--leading-comma`**: Formats SELECT list commas as leading instead of trailing.
* **`--max-text-width INTEGER`**: Sets maximum line character width before wrapping.

### Configuration via Config File / Environment variables:
Options can be set in the main config file under `format` (mapped to `FormatConfig` in `sqlmesh/core/config/format.py`), or as model defaults (`ModelDefaultsConfig(formatting=False)` in `sqlmesh/core/config/model.py`), or through environment variables (e.g., `SQLMESH__FORMAT__LEADING_COMMA=true`).

---

## 4. File Discovery & File Types Touched
The `format` command is strictly designed to discover and modify **SQL models and audits** (.sql files only).

* **Discovery**: SQLMesh loads model definitions via its loader system. The standard loader `SqlMeshLoader` (`sqlmesh/core/loader.py:481`) globs SQL files recursively in the project's `models/` (`c.MODELS`) and `audits/` directories.
* **Filtering**: In `Context.format()`, targets are filtered using the list comprehension:
  ```python
  filtered_targets = [
      target
      for target in chain(self._models.values(), self._audits.values())
      if target._path is not None
      and target._path.suffix == ".sql"
      and (not paths or any(target._path.samefile(p) for p in paths))
  ]
  ```
* **Excluded File Types**: The command does **not** touch Python models (`.py`), YAML model definitions (`.yaml`, `.yml`), macros, configurations, or schemas.

---

## 5. Under-the-Hood AST Round-Trip
The formatting implementation performs a full **round-trip parsing** of the SQL source code:
1. **Source to AST**: The raw source string is parsed using `sqlmesh.core.dialect.parse`, which returns a list of SQLGlot AST node structures (`exp.Expr`).
2. **AST Mutation**:
   - Updates meta-expressions (the `MODEL` or `AUDIT` block) to adjust dialect settings if transpilation is requested.
   - Converts `Cast` AST nodes into `DColonCast` AST nodes for cast normalization.
3. **AST to Target SQL**: SQLGlot's `.sql(...)` generator produces the final formatted output from the modified AST using the configured dialect generator rules.
4. **Statement Stitching**: Statements are stitched back together using double newlines and semicolons.

Because it is an AST-based round-trip, comments that are detached from AST nodes may be relocated or discarded, and invalid SQL syntax anywhere within a formatted file will result in parsing failures.

---

## 6. Project Context & External Warehouse Dependencies
Running `sqlmesh format` **requires loading the full project context and connecting to the warehouse/state sync backend**. It is not a pure local formatting command.

### Evidence:
1. **No Context Skip**: In `sqlmesh/cli/main.py` lines 25–43, `format` is **not** included in `SKIP_LOAD_COMMANDS` or `SKIP_CONTEXT_COMMANDS`. Therefore, `Context` is instantiated on command invocation with `load=True`.
2. **Full Project Loading**: Instantiating the context executes `context.load()`, which parses **every** script, model, and audit in the project recursively. A single syntax error in an unrelated model will block the formatting command from starting.
3. **Warehouse Connection/State Sync Hit**:
   - Inside `Context.load()` (**`sqlmesh/core/context.py` at lines 677–689**), SQLMesh checks the production environment definition from state sync:
     ```python
     prod = self.state_reader.get_environment(c.PROD)
     ```
   - Resolving `self.state_reader` forces the evaluation of the `self.state_sync` property (**`sqlmesh/core/context.py` at line 621**).
   - If a state sync backend is uninitialized, the context evaluates `_new_state_sync()` (**line 2944**) which calls `_scheduler.create_state_sync(self)` to establish a connection to the configured database.
   - Under database-backed state storage (e.g., Snowflake, BigQuery, Databricks), SQLMesh makes network/database connections to query state tables (and may run schema migrations if `schema_version == 0`).
   - Consequently, running `sqlmesh format` requires valid warehouse credentials and an active database connection.

---

## 7. Existing Format Tests
The following files cover format command behavior:

* **`tests/core/test_format.py`**:
  * `test_format_files`: Builds a temporary directory with multiple SQL models, audits, and inline audits. Asserts that formatting check detects unformatted files, verifies transpiling to BigQuery (rewriting both dialect metadata and query-specific syntax successfully), and validates formatting of nested/inline audits.
  * `test_ignore_formating_files`: Validates that configuring `formatting false` in `MODEL` metadata, `AUDIT` metadata, or as a global configuration model default successfully skips formatting for those files.
* **`tests/cli/test_cli.py`**:
  * `test_format_leading_comma_default`: Runs the CLI `sqlmesh format` command using a Click runner. Ensures environment variables (e.g., `SQLMESH__FORMAT__LEADING_COMMA`) are respected, and that explicit CLI flags (such as `--leading-comma`) override any environment variable configs.
* **`tests/core/test_dialect.py`**:
  * `test_format_model_expressions`, `test_macro_format`, `test_format_body_macros`: Test that AST structures, custom macro syntaxes (e.g., `@WITH`, `@EACH`), comments, and lists are formatted into correct SQL syntax.
* **`tests/core/test_model.py` / `tests/core/test_audit.py`**:
  * `test_formatting_flag_serde` / `test_audit_formatting_flag_serde`: Verify the `formatting` boolean field is excluded from JSON serializations but safely preserved across Pydantic models.

---

## 8. Contributor Quirks & Non-Obvious Behavior
* **Unrelated Errors Block Execution**: Because the entire context is loaded on startup, any syntax/validation error in **any** file in the project will cause `sqlmesh format` to crash immediately—even if the user only specified a single valid file via the `paths` parameter.
* **In-place Metadata Modification**: Passing the `--transpile` flag will physically rewrite the `dialect` property within the model's SQL block `MODEL (...)` in the source file on disk. This is a mutating write that changes the dialect SQLMesh uses to load that model in all subsequent executions.
* **Credentials/Connection Dependency**: Due to loading state definitions during context initialization, local formatting can fail if database credentials are invalid or if there is no active internet connection to a remote data warehouse.
