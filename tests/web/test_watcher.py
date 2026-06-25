from __future__ import annotations

import typing as t
from pathlib import Path

import pytest
from watchfiles import Change

from sqlmesh.core.context import Context
from web.server import watcher as watcher_module
from web.server.settings import Settings

pytestmark = pytest.mark.web


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a project in a temporary directory and return its path."""
    project = tmp_path / "real_project"
    (project / "models").mkdir(parents=True)
    (project / "config.py").write_text(
        "from sqlmesh.core.config import Config, ModelDefaultsConfig\n"
        "config = Config(model_defaults=ModelDefaultsConfig(dialect=''))\n"
    )
    (project / "models" / "existing.sql").write_text(
        "MODEL (name existing, kind FULL);\nSELECT 1 AS c"
    )
    return project


@pytest.fixture
def symlinked_project(tmp_path: Path, project: Path) -> Path:
    """Create a symlink to a project in a temporary directory and return its path."""
    link = tmp_path / "linked_project"
    link.symlink_to(project, target_is_directory=True)
    return link


@pytest.mark.parametrize("event_path_is_resolved", [True, False])
@pytest.mark.asyncio
async def test_watch_project_tracks_new_model_through_symlink(
    symlinked_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_path_is_resolved: bool,
) -> None:
    """
    Test thata a model added while the project is reached through a symlink
    is picked up by ``context.refresh()`` without crashing the watcher.

    event_path_is_resolved is True on macOS/FSEvents and False on Linux/inotify,
    so we need to test both cases.
    """
    context = Context(paths=str(symlinked_project))
    context.load()

    new_file = symlinked_project / "models" / "new_model.sql"
    new_file.write_text("MODEL (name new_model, kind FULL);\nSELECT 2 AS c")
    event_path = str(new_file.resolve()) if event_path_is_resolved else str(new_file)

    async def fake_awatch(*args: t.Any, **kwargs: t.Any) -> t.AsyncIterator[t.Set[t.Any]]:
        yield {(Change.added, event_path)}

    monkeypatch.setattr(watcher_module, "get_settings", lambda: Settings(project_path=symlinked_project))
    monkeypatch.setattr(watcher_module, "get_context", lambda *a, **k: context)
    monkeypatch.setattr(watcher_module, "awatch", fake_awatch)

    await watcher_module.watch_project()
    context.refresh()
    assert context.get_model("new_model") is not None
