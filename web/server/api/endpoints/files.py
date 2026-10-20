from __future__ import annotations

import os
import typing as t
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from starlette.status import HTTP_204_NO_CONTENT, HTTP_404_NOT_FOUND

from sqlmesh.core import constants as c
from sqlmesh.core.context import Context
from sqlmesh.core.dialect import format_model_expressions, parse
from web.server import models
from web.server.console import api_console
from web.server.exceptions import ApiException
from web.server.settings import (
    Settings,
    get_context,
    get_path_to_model_mapping,
    get_settings,
)
from web.server.utils import replace_file, validate_path

router = APIRouter()


@router.get("", response_model=models.Directory)
def get_files(settings: Settings = Depends(get_settings)) -> models.Directory:
    """Get all project files."""
    return _get_directory(settings.project_path, settings)


@router.get("/{path:path}", response_model=models.File)
def get_file(
    path: str = Depends(validate_path), settings: Settings = Depends(get_settings)
) -> models.File:
    """Get a file, including its contents."""
    try:
        file_path = Path(path)
        file = _get_file_with_content(settings.project_path / file_path, file_path.as_posix())
    except FileNotFoundError:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND)

    return file


@router.post("/{path:path}", response_model=t.Optional[models.File])
async def write_file(
    response: Response,
    path: str = Depends(validate_path),
    content: str = Body("", embed=True),
    new_path: t.Optional[str] = Body(None, embed=True),
    settings: Settings = Depends(get_settings),
    context: t.Optional[Context] = Depends(get_context),
) -> t.Optional[models.File]:
    """Create, update, or rename a file."""
    path_or_new_path = path
    if new_path:
        path_or_new_path = validate_path(new_path, settings)
        replace_file(settings.project_path / path, settings.project_path / path_or_new_path)
    else:
        full_path = settings.project_path / path
        config, _ = context.config_for_path(Path(path_or_new_path)) if context else (None, None)
        if (
            config
            and config.ui.format_on_save
            and content
            and Path(path_or_new_path).suffix == ".sql"
        ):
            format_file_status = models.FormatFileStatus(
                status=models.Status.INIT, path=path_or_new_path
            )
            path_to_model_mapping = get_path_to_model_mapping(settings=settings)
            model = path_to_model_mapping.get(Path(full_path))
            default_dialect = config.dialect
            dialect = model.dialect if model and model.is_sql else default_dialect
            try:
                expressions = parse(content, default_dialect=default_dialect)
                content = format_model_expressions(
                    expressions, dialect, **config.format.generator_options
                )
                if config.format.append_newline:
                    content += "\n"
                format_file_status.status = models.Status.SUCCESS
            except Exception:
                format_file_status.status = models.Status.FAIL
                error = ApiException(
                    message="Unable to format SQL file",
                    origin="API -> files -> write_file",
                ).to_dict()
                api_console.log_event(event=models.EventName.WARNINGS, data=error)
            api_console.log_event(
                event=models.EventName.FORMAT_FILE, data=format_file_status.dict()
            )
        full_path.write_text(content, encoding="utf-8")

    response.status_code = HTTP_204_NO_CONTENT

    return None


@router.delete("/{path:path}")
async def delete_file(
    response: Response,
    path: str = Depends(validate_path),
    settings: Settings = Depends(get_settings),
) -> None:
    """Delete a file."""
    try:
        (settings.project_path / path).unlink()
        response.status_code = HTTP_204_NO_CONTENT
    except FileNotFoundError:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND)
    except IsADirectoryError:
        raise ApiException(
            message="File is a directory",
            origin="API -> files -> delete_file",
        )


def _get_directory(path: str | Path, settings: Settings) -> models.Directory:
    context = get_context(settings)
    ignore_patterns = context.config.ignore_patterns if context else c.IGNORE_PATTERNS

    def walk_path(
        path: str | Path,
    ) -> tuple[list[models.Directory], list[models.File]]:
        directories = []
        files = []

        with os.scandir(path) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                if (
                    entry.name == "__pycache__"
                    or entry.name.startswith(".")
                    or any(entry_path.match(pattern) for pattern in ignore_patterns)
                ):
                    continue

                relative_path = entry_path.relative_to(settings.project_path)

                if entry.is_dir(follow_symlinks=False):
                    _directories, _files = walk_path(entry.path)
                    directories.append(
                        models.Directory(
                            name=entry.name,
                            path=str(relative_path.as_posix()),
                            directories=_directories,
                            files=_files,
                        )
                    )
                elif entry.is_file(follow_symlinks=False):
                    files.append(models.File(name=entry.name, path=str(relative_path.as_posix())))
        return sorted(directories, key=lambda x: x.name), sorted(files, key=lambda x: x.name)

    directories, files = walk_path(path)
    relative_path = Path(path).relative_to(settings.project_path).as_posix()

    return models.Directory(
        name=os.path.basename(path),
        path="" if relative_path == "." else relative_path,
        directories=directories,
        files=files,
    )


def _get_file_with_content(file_path: Path, relative_path: str) -> models.File:
    """Get a file, including its contents."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise e
    except Exception:
        error = ApiException(
            message="Unable to get file content",
            origin="API -> files -> _get_file_with_content",
        ).to_dict()
        api_console.log_event(event=models.EventName.WARNINGS, data=error)
        content = None

    return models.File(
        name=file_path.name,
        path=str(Path(relative_path).as_posix()),
        content=content,
    )
