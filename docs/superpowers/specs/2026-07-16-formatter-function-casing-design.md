# Formatter Function-Casing Regression Design

## Scope

Restore the pre-0.236.0 formatter behavior: absent formatter configuration preserves function spelling in model metadata and query SQL.

## Design

`FormatConfig.normalize_functions` will accept `str | bool | None` and default to `False`.  SQLMesh serializes non-`None` formatter values into SQLGlot generator options, so `False` is passed directly to SQLGlot while metadata continues to be generated with `dialect=None`.

Existing `"upper"` and `"lower"` string values remain unchanged. An explicit `None` remains accepted and preserves its current omission behavior.

## Tests

Add model-formatting regression coverage for lower-case audit references and mixed-case query functions. Verify the default preserves spelling, `"upper"` normalizes both metadata and queries to upper case, `"lower"` normalizes both to lower case, and the configuration accepts `False`.

## Documentation

Update the configuration reference and `FormatConfig` type description to list `False` as the default casing-preservation mode and retain the two string normalization modes.
