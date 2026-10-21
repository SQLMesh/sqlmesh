from __future__ import annotations

import typing as t

from sqlmesh.core.config.base import BaseConfig


class FormatConfig(BaseConfig):
    """The format configuration for SQL code.

    Args:
        normalize: Whether to normalize the SQL code or not.
        pad: The number of spaces to use for padding.
        indent: The number of spaces to use for indentation.
        normalize_functions: How to normalize function name casing.

            * ``False`` (default) — preserves the original spelling of custom and audit
              function names.  SQLGlot built-in functions (e.g. ``COUNT``, ``SUM``) may
              still be uppercased because the parser discards the original token.
            * ``"upper"`` — uppercases all function names, including custom audit
              references.
            * ``"lower"`` — lowercases all function names, including built-in ones.
            * ``True`` — defers to SQLGlot's generator default, which uppercases all
              function names including custom ones.
            * ``None`` — excluded from the serialized generator options by Pydantic's
              ``exclude_none`` behaviour, so ``format_model_expressions`` falls back to
              its own ``False`` default.  Setting this in YAML as ``null`` or omitting
              the key is therefore equivalent to ``false``; it does **not** defer to
              SQLGlot's generator default the way ``True`` does.
        leading_comma: Whether to use leading commas or not.
        max_text_width: The maximum text width in a segment before creating new lines.
        append_newline: Whether to append a newline to the end of the file or not.
        no_rewrite_casts: Preserve the existing casts, without rewriting them to use the :: syntax.
    """

    normalize: bool = False
    pad: int = 2
    indent: int = 2
    normalize_functions: t.Union[str, bool, None] = False
    leading_comma: bool = False
    max_text_width: int = 80
    append_newline: bool = False
    no_rewrite_casts: bool = False

    @property
    def generator_options(self) -> t.Dict[str, t.Any]:
        """Options which can be passed through to the SQLGlot Generator class.

        Returns:
            The generator options.
        """
        return self.dict(exclude={"append_newline", "no_rewrite_casts"})
