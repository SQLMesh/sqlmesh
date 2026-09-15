from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sqlmesh.cli.main import _sqlmesh_version
from web.server import models
from web.server.console import api_console
from web.server.settings import Settings, get_context, get_settings

router = APIRouter()


@router.get(
    "",
    response_model=models.Meta,
    response_model_exclude_unset=True,
)
def get_api_meta(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> models.Meta:
    """Get the metadata"""
    has_running_task = False

    if models.Modules.PLANS in settings.modules:
        has_running_task = hasattr(request.app.state, "task") and not request.app.state.task.done()

        api_console.log_event_plan_overview()
        api_console.log_event_plan_apply()
        api_console.log_event_plan_cancel()

        if not has_running_task and api_console.is_cancelling_plan():
            api_console.finish_plan_cancellation()

    node_colors: dict[str, str] = {}
    context = get_context(settings)
    if context:
        node_colors = context.config.ui.node_colors

    return models.Meta(
        version=_sqlmesh_version(),
        has_running_task=has_running_task,
        node_colors=node_colors,
    )
