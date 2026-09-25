from __future__ import annotations

import typing as t

from sqlmesh.core.config.base import BaseConfig
from sqlmesh.core.config.categorizer import CategorizerConfig


class PlanConfig(BaseConfig):
    """Configuration for a plan.

    Args:
        forward_only: Whether the plan should be forward-only.
        auto_categorize_changes: Whether SQLMesh should attempt to automatically categorize model changes (breaking / non-breaking)
            during plan creation.
        include_unmodified: Whether to include unmodified models in the target development environment.
        enable_preview: Whether to enable preview for forward-only models in development environments.
        no_diff: Hide text differences for changed models.
        no_prompts: Whether to disable interactive prompts for the backfill time range.
        auto_apply: Whether to automatically apply the new plan after creation.
        use_finalized_state: Whether to compare against the latest finalized environment state, or to use
            whatever state the target environment is currently in.
        always_recreate_environment: Whether to always recreate the target environment from the `create_from` environment.
        use_project_index: Whether to use the persistent project index and related planning
            optimizations.
    """

    forward_only: bool = False
    auto_categorize_changes: CategorizerConfig = CategorizerConfig()
    include_unmodified: bool = False
    enable_preview: t.Optional[bool] = None
    no_diff: bool = False
    no_prompts: bool = True
    auto_apply: bool = False
    use_finalized_state: bool = False
    always_recreate_environment: bool = False
    use_project_index: bool = False
