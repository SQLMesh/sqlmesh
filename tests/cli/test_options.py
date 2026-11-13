# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import click
import pytest
from click.testing import CliRunner

from sqlmesh.cicd.bot import bot
from sqlmesh.cli.main import cli


def _root_context(command=cli, args=()):
    return command.make_context("sqlmesh", [*args, "info" if command is cli else "github"])


@pytest.fixture(autouse=True)
def clear_cli_env(monkeypatch):
    for name in (
        "SQLMESH_PATHS",
        "SQLMESH_CONFIG",
        "SQLMESH_GATEWAY",
        "SQLMESH_IGNORE_WARNINGS",
        "SQLMESH_DOTENV_PATH",
        "SQLMESH_DOTENV",
        "SQLMESH_PLAN_START",
        "SQLMESH_TABLE_DIFF_LIMIT",
        "SQLMESH_STATE_EXPORT_OUTPUT_FILE",
        "SQLMESH_RENDER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("command", [cli, bot])
@pytest.mark.parametrize(
    "paths",
    [("/work/project",), ("relative project",), ("/work/analytics project", "/work/分析")],
)
def test_paths_env(command, paths, monkeypatch):
    monkeypatch.setenv("SQLMESH_PATHS", os.pathsep.join(paths))
    with _root_context(command) as ctx:
        assert ctx.params["paths"] == paths


@pytest.mark.parametrize("command", [cli, bot])
@pytest.mark.parametrize("option", ["-p", "--paths"])
def test_paths_cli_overrides_env(command, option, monkeypatch):
    monkeypatch.setenv("SQLMESH_PATHS", "/work/environment")
    with _root_context(command, [option, "first project", option, "second"]) as ctx:
        assert ctx.params["paths"] == ("first project", "second")


@pytest.mark.parametrize("value", [None, ""])
def test_paths_env_default(value, monkeypatch):
    if value is not None:
        monkeypatch.setenv("SQLMESH_PATHS", value)
    paths = next(param for param in cli.params if param.name == "paths")
    with _root_context() as ctx:
        assert ctx.params["paths"] == tuple(paths.default)


def test_paths_env_preserves_globs_and_empty_segments(monkeypatch):
    values = ("", "projects/*", "", "last project", "")
    monkeypatch.setenv("SQLMESH_PATHS", os.pathsep.join(values))
    with _root_context() as ctx:
        assert ctx.params["paths"] == values


def test_paths_env_does_not_require_existing_destination(tmp_path, monkeypatch):
    destination = tmp_path / "new project"
    monkeypatch.setenv("SQLMESH_PATHS", str(destination))
    with cli.make_context("sqlmesh", ["init", "duckdb"]) as ctx:
        assert ctx.params["paths"] == (str(destination),)
    assert not destination.exists()


@pytest.mark.parametrize(
    "separator, value, expected",
    [
        (":", "/work/first project:/work/second", ["/work/first project", "/work/second"]),
        (
            ";",
            r"C:\work\first project;D:\work\second",
            [r"C:\work\first project", r"D:\work\second"],
        ),
    ],
)
def test_paths_env_platform_separator(separator, value, expected, monkeypatch):
    paths = next(param for param in cli.params if param.name == "paths")
    monkeypatch.setattr(paths.type, "envvar_list_splitter", separator)
    assert paths.type.split_envvar_value(value) == expected


def test_root_config_env_and_cli_priority(monkeypatch):
    monkeypatch.setenv("SQLMESH_CONFIG", "environment_config")
    with _root_context() as ctx:
        assert ctx.params["config"] == "environment_config"
    with _root_context(args=["--config", "explicit_config"]) as ctx:
        assert ctx.params["config"] == "explicit_config"


@pytest.mark.parametrize(
    "variable, args, name, value",
    [
        ("SQLMESH_GATEWAY", ["--gateway", "explicit"], "gateway", "environment"),
        ("SQLMESH_IGNORE_WARNINGS", ["--ignore-warnings"], "ignore_warnings", "false"),
    ],
)
def test_existing_explicit_env_names(variable, args, name, value, monkeypatch):
    monkeypatch.setenv(variable, value)
    with _root_context() as ctx:
        assert ctx.params[name] == (False if value == "false" else value)
    with _root_context(args=args) as ctx:
        assert ctx.params[name] == (True if value == "false" else "explicit")


def test_existing_dotenv_env_name(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.touch()
    explicit = tmp_path / "explicit.env"
    explicit.touch()
    monkeypatch.setenv("SQLMESH_DOTENV_PATH", str(env_file))
    monkeypatch.setenv("SQLMESH_DOTENV", str(tmp_path / "ignored.env"))
    with _root_context() as ctx:
        assert ctx.params["dotenv"] == env_file
    with _root_context(args=["--dotenv", str(explicit)]) as ctx:
        assert ctx.params["dotenv"] == explicit


@pytest.mark.parametrize(
    "command, variable, name, value, expected, args",
    [
        ("plan", "SQLMESH_PLAN_START", "start", "2020-01-01", "2020-01-01", []),
        ("table_diff", "SQLMESH_TABLE_DIFF_LIMIT", "limit", "12", 12, ["source:target", "model"]),
    ],
)
def test_command_env_prefix(command, variable, name, value, expected, args, monkeypatch):
    monkeypatch.setenv(variable, value)
    with _root_context() as parent:
        child_command = cli.get_command(parent, command)
        with child_command.make_context(command, list(args), parent=parent) as child:
            assert child.params[name] == expected
        with child_command.make_context(command, [*args, f"--{name}", "3"], parent=parent) as child:
            assert child.params[name] == (3 if isinstance(expected, int) else "3")


def test_command_env_invalid_integer(monkeypatch):
    monkeypatch.setenv("SQLMESH_TABLE_DIFF_LIMIT", "invalid")
    with _root_context() as parent:
        command = cli.get_command(parent, "table_diff")
        with pytest.raises(click.BadParameter, match="valid integer"):
            command.make_context("table_diff", ["source:target", "model"], parent=parent)


def test_nested_command_env_prefix(tmp_path, monkeypatch):
    output_file = tmp_path / "state.json"
    monkeypatch.setenv("SQLMESH_STATE_EXPORT_OUTPUT_FILE", str(output_file))
    with _root_context() as parent:
        state = cli.get_command(parent, "state")
        with state.make_context("state", ["export"], parent=parent) as state_ctx:
            command = state.get_command(state_ctx, "export")
            with command.make_context("export", [], parent=state_ctx) as child:
                assert child.params["output_file"] == output_file


def test_positional_arguments_have_no_auto_env_binding(monkeypatch):
    monkeypatch.setenv("SQLMESH_RENDER_MODEL", "some_model")
    with _root_context() as parent:
        command = cli.get_command(parent, "render")
        with pytest.raises(click.MissingParameter):
            command.make_context("render", [], parent=parent)


@pytest.mark.parametrize("explicit", [False, True])
def test_bot_shared_paths_reach_command_boundary(tmp_path, monkeypatch, explicit):
    monkeypatch.setenv("SQLMESH_PATHS", str(tmp_path / "environment project"))
    captured = {}

    @click.command()
    @click.pass_obj
    def capture(obj):
        captured.update(obj)

    monkeypatch.setitem(bot.commands, "capture", capture)
    args = ["-p", str(tmp_path / "explicit project")] if explicit else []
    result = CliRunner().invoke(bot, [*args, "capture"])
    assert result.exit_code == 0, result.output
    assert captured["paths"] == (
        str(tmp_path / ("explicit project" if explicit else "environment project")),
    )
