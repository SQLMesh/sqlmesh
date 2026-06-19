from __future__ import annotations

import typing as t

from sqlmesh.core.config.base import BaseConfig


class UIConfig(BaseConfig):
    """The UI configuration for SQLMesh.

    Args:
        format_on_save: Whether to format the SQL code on save or not.
        node_colors: A mapping of model tags to hex color strings used
            to color-code nodes in the lineage DAG visualization.
    """

    format_on_save: bool = True
    node_colors: t.Dict[str, str] = {}
