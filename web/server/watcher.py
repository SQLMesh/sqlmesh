import typing as t
from pathlib import Path

from watchfiles import Change, DefaultFilter, awatch

from sqlmesh.core import constants as c
from sqlmesh.core.context import Context
from web.server import models
from web.server.api.endpoints.files import _get_directory, _get_file_with_content
from web.server.console import api_console
from web.server.exceptions import ApiException
from web.server.settings import (
    Settings,
    get_context,
    get_settings,
    invalidate_context_cache,
)
from web.server.utils import is_relative_to


async def watch_project() -> None:
    settings = get_settings()
    context = get_context(settings)
    paths = [
        (settings.project_path / c.AUDITS).resolve(),
        (settings.project_path / c.MACROS).resolve(),
        (settings.project_path / c.MODELS).resolve(),
        (settings.project_path / c.METRICS).resolve(),
        (settings.project_path / c.SEEDS).resolve(),
    ]
    ignore_dirs = [".env"]
    cache_path = (
        context.cache_dir.resolve() if context else (settings.project_path / c.CACHE).resolve()
    )
    ignore_paths: t.List[t.Union[str, Path]] = [cache_path]
    ignore_entity_patterns = context.config.ignore_patterns if context else c.IGNORE_PATTERNS
    ignore_entity_patterns.append("^.*\\.db(\\.wal)?$")

    async for entries in awatch(
        settings.project_path,
        watch_filter=DefaultFilter(
            ignore_paths=list(DefaultFilter.ignore_paths or []) + ignore_paths,
            ignore_entity_patterns=list(DefaultFilter.ignore_entity_patterns or [])
            + ignore_entity_patterns,
            ignore_dirs=list(DefaultFilter.ignore_dirs or []) + ignore_dirs,
        ),
    ):
        changes: t.List[models.ArtifactChange] = []
        directories: t.Dict[str, models.Directory] = {}
        for change, path_str in entries:
            path = Path(path_str)
            relative_path = path.relative_to(settings.project_path)
            try:
                if change == Change.deleted or not path.exists():
                    changes.append(
                        models.ArtifactChange(
                            change=Change.deleted,
                            path=str(relative_path),
                        )
                    )
                elif change == Change.added:
                    directory = _get_directory(path.parent, settings)
                    directories[directory.path] = directory
                elif path.is_dir() and change == Change.modified:
                    directory = _get_directory(path, settings)
                    directories[directory.path] = directory
                elif path.is_file() and change == Change.modified:
                    changes.append(
                        models.ArtifactChange(
                            type=models.ArtifactType.file,
                            change=change,
                            path=str(relative_path),
                            file=_get_file_with_content(
                                settings.project_path / relative_path, str(relative_path)
                            ),
                        )
                    )
                if context:
                    in_paths = any(is_relative_to(path, p) for p in paths)
                    is_modified_new_file = change == Change.modified and any(
                        path not in loader._path_mtimes for loader in context._loaders
                    )
                    should_track_file = path.is_file() and in_paths
                    should_reset_mtime = Change.added or is_modified_new_file
                    if should_track_file and should_reset_mtime:
                        for loader in context._loaders:
                            loader._path_mtimes[path] = 0

            except Exception:
                error = ApiException(
                    message="Error updating file",
                    origin="API -> watcher -> watch_project",
                    trigger=path_str,
                ).to_dict()
                api_console.log_event(event=models.EventName.WARNINGS, data=error)

        if settings.modules.intersection(
            {
                models.Modules.FILES,
                models.Modules.DATA_CATALOG,
                models.Modules.PLANS,
                models.Modules.LINEAGE,
            }
        ) and (changes or directories):
            api_console.log_event(
                event=models.EventName.FILE,
                data={"changes": changes, "directories": directories},
            )

        if is_config_changed(entries, settings, context):
            invalidate_context_cache()
            api_console.log_event(
                event=models.EventName.WARNINGS,
                data=ApiException(
                    message="Config file changed",
                    origin="API -> watcher -> watch_project",
                    trigger="config",
                ).to_dict(),
            )


def is_config_changed(
    entries: t.Set[t.Any], settings: Settings, context: t.Optional[Context] = None
) -> bool:
    config_paths = set(context.configs) if context else {settings.project_path}

    return any(
        Path(path) in config_path.glob("config.*")
        for config_path in config_paths
        for _, path in entries
    )
