from __future__ import annotations

import sys
import typing as t

from sqlglot import exp

if t.TYPE_CHECKING:
    TableName = t.Union[str, exp.Table]
    SchemaName = t.Union[str, exp.Table]
    SessionProperties = t.Dict[str, t.Union[exp.Expr, str, int, float, bool]]
    CustomMaterializationProperties = t.Dict[str, t.Union[exp.Expr, str, int, float, bool]]


if sys.version_info >= (3, 11):
    from typing import Self as Self
else:
    from typing_extensions import Self as Self
