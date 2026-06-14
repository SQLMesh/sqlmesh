import typing as t
import pytest
from pathlib import Path
import shutil
from click.testing import Result
import time_machine
from sqlmesh_dbt.operations import create
from tests.cli.test_cli import FREEZE_TIME
from tests.dbt.conftest import EmptyProjectCreator

pytestmark = pytest.mark.slow


def test_run(jaffle_shop_duckdb: Path, invoke_cli: t.Callable[..., Result]):
    result = invoke_cli(["run"])

    assert result.exit_code == 0
    assert not result.exception

    assert "Model batches executed" in result.output


def test_run_with_selectors(jaffle_shop_duckdb: Path, invoke_cli: t.Callable[..., Result]):
    with time_machine.travel(FREEZE_TIME):
        # do an initial run to create the objects
        # otherwise the selected subset may depend on something that hasnt been created
        result = invoke_cli(["run"])
        assert result.exit_code == 0
        assert "main.orders" in result.output

    result = invoke_cli(["run", "--select", "raw_customers+", "--exclude", "orders"])

    assert result.exit_code == 0
    assert not result.exception

    assert "main.stg_customers" in result.output
    assert "main.stg_orders" in result.output
    assert "main.stg_payments" in result.output
    assert "main.customers" in result.output

    assert "main.orders" not in result.output

    assert "Model batches executed" in result.output


def test_run_with_changes_and_full_refresh(
    create_empty_project: EmptyProjectCreator, invoke_cli: t.Callable[..., Result]
):
    project_path, models_path = create_empty_project(project_name="test")

    engine_adapter = create(project_path).context.engine_adapter
    engine_adapter.execute("create table external_table as select 'foo' as a, 'bar' as b")

    (models_path / "model_a.sql").write_text("select a, b from external_table")
    (models_path / "model_b.sql").write_text("select a, b from {{ ref('model_a') }}")

    # populate initial env
    result = invoke_cli(["run"])
    assert result.exit_code == 0
    assert not result.exception

    assert engine_adapter.fetchall("select a, b from model_b") == [("foo", "bar")]

    engine_adapter.execute("insert into external_table (a, b) values ('baz', 'bing')")
    (project_path / "models" / "model_b.sql").write_text(
        "select a, b, 'changed' as c from {{ ref('model_a') }}"
    )

    # Clear dbt's partial parse cache to ensure file changes are detected
    # Without it dbt may use stale cached model definitions, causing flakiness
    partial_parse_file = project_path / "target" / "sqlmesh_partial_parse.msgpack"
    if partial_parse_file.exists():
        partial_parse_file.unlink()

    cache_dir = project_path / ".cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)

    # run with --full-refresh. this should:
    # - fully refresh model_a (pick up the new records from external_table)
    # - deploy the local change to model_b (introducing the 'changed' column)
    result = invoke_cli(["run", "--full-refresh"])
    assert result.exit_code == 0
    assert not result.exception

    assert engine_adapter.fetchall("select a, b from model_a") == [("foo", "bar"), ("baz", "bing")]
    assert engine_adapter.fetchall("select a, b, c from model_b") == [
        ("foo", "bar", "changed"),
        ("baz", "bing", "changed"),
    ]


def test_run_with_threads(jaffle_shop_duckdb: Path, invoke_cli: t.Callable[..., Result]):
    result = invoke_cli(["run", "--threads", "4"])
    assert result.exit_code == 0
    assert not result.exception

    assert "Model batches executed" in result.output
