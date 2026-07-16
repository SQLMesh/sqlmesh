# Formatter Function-Casing Implementation Plan
> *Estimated costs for executing this plan:*
> **Current Model** (GPT-5.6 Terra): 24k input / 7.2k output | **$0.17 – $0.25**
> **Auto Model**: (Same token usage) | **$0.07 – $0.11**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve function spelling by default when SQLMesh formats model metadata and query SQL.

**Architecture:** `FormatConfig` is serialized to SQLGlot generator options. Set its `normalize_functions` default to the explicit boolean `False`, which survives SQLMesh's `exclude_none` serialization and changes only SQLGlot's generator option. `format_model_expressions` continues to render metadata with `dialect=None`.

**Tech Stack:** Python, Pydantic configuration models, SQLGlot formatter, pytest.

## Global Constraints

- `normalize_functions` accepts `str | bool | None`; the default is `False`.
- Existing `"upper"` and `"lower"` configuration behavior remains supported.
- Do not add a new CLI string mode for `False`.
- Retain metadata rendering with `dialect=None`; alter only the generator option.
- Every new commit includes a DCO `Signed-off-by` trailer.

---

### Task 1: Lock down formatter casing behavior

**Files:**
- Modify: `tests/core/test_dialect.py:22-168`
- Test: `tests/core/test_dialect.py`

**Interfaces:**
- Consumes: `format_model_expressions(expressions, **generator_options)` and `parse(sql)`.
- Produces: regression coverage for default, `"upper"`, and `"lower"` function casing across metadata and queries.

- [ ] **Step 1: Write the failing regression test**

```python
def test_format_model_expressions_normalize_functions():
    expressions = parse(
        """
        MODEL (
          name x,
          audits (
            unique_combination_of_columns(columns := (id)),
            not_null(columns := (id))
          )
        );

        SELECT SUM(id), count(id) FROM foo;
        """
    )

    assert format_model_expressions(expressions) == """MODEL (
  name x,
  audits (
    unique_combination_of_columns(columns := (id)),
    not_null(columns := (id))
  )
);

SELECT SUM(id), count(id) FROM foo;"""
```

- [ ] **Step 2: Extend the same test with explicit normalization assertions**

```python
assert format_model_expressions(expressions, normalize_functions="upper") == """MODEL (
  name x,
  audits (
    UNIQUE_COMBINATION_OF_COLUMNS(columns := (id)),
    NOT_NULL(columns := (id))
  )
);

SELECT SUM(id), COUNT(id) FROM foo;"""

assert format_model_expressions(expressions, normalize_functions="lower") == """MODEL (
  name x,
  audits (
    unique_combination_of_columns(columns := (id)),
    not_null(columns := (id))
  )
);

SELECT sum(id), count(id) FROM foo;"""
```

- [ ] **Step 3: Run the regression test to verify the default fails**

Run: `.venv/bin/pytest tests/core/test_dialect.py::test_format_model_expressions_normalize_functions -v`

Expected: the default output contains upper-case audit references and `COUNT(id)`.

### Task 2: Make the formatter default explicit

**Files:**
- Modify: `sqlmesh/core/config/format.py:8-38`
- Test: `tests/core/test_dialect.py`

**Interfaces:**
- Consumes: `FormatConfig.generator_options`.
- Produces: `normalize_functions=False` in generator options unless callers explicitly configure a different accepted value.

- [ ] **Step 1: Add a failing configuration validation assertion**

```python
from sqlmesh.core.config.format import FormatConfig


def test_format_config_normalize_functions_false():
    config = FormatConfig(normalize_functions=False)

    assert config.normalize_functions is False
    assert config.generator_options["normalize_functions"] is False
```

- [ ] **Step 2: Run the validation test to verify it fails**

Run: `.venv/bin/pytest tests/core/test_dialect.py::test_format_config_normalize_functions_false -v`

Expected: validation rejects `False` before the configuration change.

- [ ] **Step 3: Implement the minimal configuration change**

```python
normalize_functions: t.Union[str, bool, None] = False
```

Update the `FormatConfig` argument description to specify that `False` preserves existing casing and is the default, while `"upper"` and `"lower"` normalize casing.

- [ ] **Step 4: Run focused regression coverage**

Run: `.venv/bin/pytest tests/core/test_dialect.py::test_format_model_expressions tests/core/test_dialect.py::test_format_model_expressions_normalize_functions tests/core/test_dialect.py::test_format_config_normalize_functions_false -v`

Expected: all selected tests pass and existing `test_format_model_expressions` expectations reflect preserved lower-case audit references.

### Task 3: Document configuration and verify the branch

**Files:**
- Modify: `docs/reference/configuration.md:112-121`
- Modify: `tests/core/test_dialect.py:22-168`
- Modify: `sqlmesh/core/config/format.py:8-38`

**Interfaces:**
- Consumes: the documented `FormatConfig.normalize_functions` values.
- Produces: configuration documentation that accurately reflects the accepted type and default.

- [ ] **Step 1: Update the configuration reference**

Document the option as `string | boolean | null`: `False` (the default) preserves existing casing; `"upper"` and `"lower"` normalize all function names.

- [ ] **Step 2: Run formatter and targeted tests**

Run: `.venv/bin/ruff format --check sqlmesh/core/config/format.py tests/core/test_dialect.py && .venv/bin/pytest tests/core/test_dialect.py tests/core/test_format.py -v -m "not slow and not docker"`

Expected: formatting check and selected test modules pass.

- [ ] **Step 3: Review the diff**

Run: `git diff --check HEAD~1..HEAD && git diff -- sqlmesh/core/config/format.py tests/core/test_dialect.py docs/reference/configuration.md`

Expected: no whitespace errors; metadata formatting remains delegated to the existing `dialect=None` call site.

- [ ] **Step 4: Commit the implementation**

```bash
git add sqlmesh/core/config/format.py tests/core/test_dialect.py docs/reference/configuration.md
git commit -s -m "fix: preserve function casing during formatting"
```
