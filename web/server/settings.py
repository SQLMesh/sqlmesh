from __future__ import annotations

import contextlib
import logging
import os
import threading
import typing as t
from functools import lru_cache
from pathlib import Path

from fastapi import Depends
from pydantic import Field

from sqlmesh.core.context import Context
from sqlmesh.core.model.definition import Model
from sqlmesh.utils.pydantic import PydanticModel
from web.server import models
from web.server.exceptions import ApiException
from web.server.models import Mode

logger = logging.getLogger(__name__)

get_loaded_context_lock = threading.Lock()

MODE_TO_MODULES = {
    models.Mode.IDE: {
        models.Modules.EDITOR,
        models.Modules.FILES,
        models.Modules.DATA_CATALOG,
        models.Modules.ERRORS,
        models.Modules.PLANS,
    },
    models.Mode.CATALOG: {models.Modules.DATA_CATALOG},
    models.Mode.PLAN: {
        models.Modules.PLANS,
        models.Modules.DATA_CATALOG,
        models.Modules.LINEAGE,
        models.Modules.ERRORS,
    },
}


class Settings(PydanticModel):
    project_path: Path = Field(
        default_factory=lambda: Path(os.getenv("PROJECT_PATH", "examples/sushi"))
    )
    config: str = Field(default_factory=lambda: os.getenv("CONFIG", ""))
    gateway: t.Optional[str] = Field(default_factory=lambda: os.getenv("GATEWAY"))
    ui_mode: Mode = Field(
        default_factory=lambda: Mode[os.getenv("UI_MODE", Mode.IDE.value).upper()]
    )

    @property
    def modules(self) -> t.Set[models.Modules]:
        return MODE_TO_MODULES[
            models.Mode.CATALOG if self.ui_mode == models.Mode.DOCS else self.ui_mode
        ]


@lru_cache()
def get_settings() -> Settings:
    return Settings()


@lru_cache()
def _get_context(path: str | Path, config: str, gateway: str) -> Context:
    from sqlmesh.core.console import set_console
    from web.server.main import api_console

    set_console(api_console)
    return Context(paths=str(path), config=config, gateway=gateway, load=False)


@lru_cache()
def _get_loaded_context(path: str | Path, config: str, gateway: str) -> Context:
    context = _get_context(path, config, gateway)
    context.load()
    return context


@lru_cache()
def _get_path_to_model_mapping(context: Context) -> dict[Path, Model]:
    return {model._path: model for model in context._models.values() if model._path}


def get_path_to_model_mapping(
    settings: Settings = Depends(get_settings),
) -> dict[Path, Model]:
    try:
        with contextlib.contextmanager(get_loaded_context)(settings) as context:
            return _get_path_to_model_mapping(context)
    except Exception:
        logger.exception("Error creating a context")
        return {}


def get_loaded_context(
    settings: Settings = Depends(get_settings),
) -> t.Generator[Context, None, None]:
    try:
        with get_loaded_context_lock:
            yield _get_loaded_context(settings.project_path, settings.config, settings.gateway)
    except Exception:
        raise ApiException(
            message="Unable to create a loaded context",
            origin="API -> settings -> get_loaded_context",
        )


def get_context(settings: Settings = Depends(get_settings)) -> t.Optional[Context]:
    try:
        return _get_context(settings.project_path, settings.config, settings.gateway)
    except Exception:
        return None


def get_context_or_raise(settings: Settings = Depends(get_settings)) -> Context:
    context = get_context(settings)
    if not context:
        raise ApiException(
            message="Unable to create a context",
            origin="API -> settings -> get_context",
        )
    return context


def invalidate_context_cache() -> None:
    _get_context.cache_clear()
    _get_loaded_context.cache_clear()
    _get_path_to_model_mapping.cache_clear()
