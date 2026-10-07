from __future__ import annotations

from sqlmesh.core.config.base import BaseConfig


class RenderConfig(BaseConfig):
    """Configuration for rendering model queries.

    Args:
        use_project_index: Whether to use the persistent project index when rendering.
    """

    use_project_index: bool = False
