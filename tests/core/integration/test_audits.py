from __future__ import annotations

import typing as t
from textwrap import dedent
import pytest
from pathlib import Path
import time_machine
from sqlglot import exp
from IPython.utils.capture import capture_output
from pytest_mock.plugin import MockerFixture

from sqlmesh.core.config import (
    Config,
    ModelDefaultsConfig,
)
from sqlmesh.core.context import Context
from sqlmesh.core.snapshot import SnapshotEvaluator
from sqlmesh.utils.errors import (
    PlanError,
)
from tests.utils.test_helpers import use_terminal_console
from tests.utils.test_filesystem import create_temp_file

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 15:00:00 UTC")
@use_terminal_console
def test_audit_only_metadata_change(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Add a new audit
    model = context.get_model("sushi.waiter_revenue_by_day")
    audits = model.audits.copy()
    audits.append(("number_of_rows", {"threshold": exp.Literal.number(1)}))
    model = model.copy(update={"audits": audits})
    context.upsert_model(model)

    plan = context.plan_builder("prod", skip_tests=True).build()
    assert len(plan.new_snapshots) == 2
    assert all(s.change_category.is_metadata for s in plan.new_snapshots)
    assert not plan.missing_intervals

    with capture_output() as output:
        context.apply(plan)

    assert "Auditing models" in output.stdout
    assert model.name in output.stdout


@use_terminal_console
def test_audits_running_on_metadata_changes(tmp_path: Path):
    def setup_senario(model_before: str, model_after: str):
        models_dir = Path("models")
        create_temp_file(tmp_path, models_dir / "test.sql", model_before)

        # Create first snapshot
        context = Context(paths=tmp_path, config=Config())
        context.plan("prod", no_prompts=True, auto_apply=True)

        # Create second (metadata) snapshot
        create_temp_file(tmp_path, models_dir / "test.sql", model_after)
        context.load()

        with capture_output() as output:
            with pytest.raises(PlanError):
                context.plan("prod", no_prompts=True, auto_apply=True)

        assert 'Failed models\n\n  "model"' in output.stdout

        return output

    # Ensure incorrect audits (bad data, incorrect definition etc) are evaluated immediately
    output = setup_senario(
        "MODEL (name model); SELECT NULL AS col",
        "MODEL (name model, audits (not_null(columns=[col]))); SELECT NULL AS col",
    )
    assert "'not_null' audit error: 1 row failed" in output.stdout

    output = setup_senario(
        "MODEL (name model); SELECT NULL AS col",
        "MODEL (name model, audits (not_null(columns=[this_col_does_not_exist]))); SELECT NULL AS col",
    )
    assert (
        'Binder Error: Referenced column "this_col_does_not_exist" not found in \nFROM clause!'
        in output.stdout
    )


@pytest.mark.slow
def test_default_audits_applied_in_plan(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)

    # Create a model with data that will pass the audits
    create_temp_file(
        tmp_path,
        models_dir / "orders.sql",
        dedent("""
            MODEL (
                name test.orders,
                kind FULL
            );

            SELECT
                1 AS order_id,
                'customer_1' AS customer_id,
                100.50 AS amount,
                '2024-01-01'::DATE AS order_date
            UNION ALL
            SELECT
                2 AS order_id,
                'customer_2' AS customer_id,
                200.75 AS amount,
                '2024-01-02'::DATE AS order_date
        """),
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(
            dialect="duckdb",
            audits=[
                "not_null(columns := [order_id, customer_id])",
                "unique_values(columns := [order_id])",
            ],
        )
    )

    context = Context(paths=tmp_path, config=config)

    # Create and apply plan, here audits should pass
    plan = context.plan("prod", no_prompts=True)
    context.apply(plan)

    # Verify model has the default audits
    model = context.get_model("test.orders")
    assert len(model.audits) == 2

    audit_names = [audit[0] for audit in model.audits]
    assert "not_null" in audit_names
    assert "unique_values" in audit_names

    # Verify audit arguments are preserved
    for audit_name, audit_args in model.audits:
        if audit_name == "not_null":
            assert "columns" in audit_args
            columns = [col.name for col in audit_args["columns"].expressions]
            assert "order_id" in columns
            assert "customer_id" in columns
        elif audit_name == "unique_values":
            assert "columns" in audit_args
            columns = [col.name for col in audit_args["columns"].expressions]
            assert "order_id" in columns


@pytest.mark.slow
def test_default_audits_fail_on_bad_data(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)

    # Create a model with data that violates NOT NULL constraint
    create_temp_file(
        tmp_path,
        models_dir / "bad_orders.sql",
        dedent("""
            MODEL (
                name test.bad_orders,
                kind FULL
            );

            SELECT
                1 AS order_id,
                NULL AS customer_id,  -- This violates NOT NULL
                100.50 AS amount,
                '2024-01-01'::DATE AS order_date
            UNION ALL
            SELECT
                2 AS order_id,
                'customer_2' AS customer_id,
                200.75 AS amount,
                '2024-01-02'::DATE AS order_date
        """),
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(
            dialect="duckdb", audits=["not_null(columns := [customer_id])"]
        )
    )

    context = Context(paths=tmp_path, config=config)

    # Plan should fail due to audit failure
    with pytest.raises(PlanError):
        context.plan("prod", no_prompts=True, auto_apply=True)


@pytest.mark.slow
def test_default_audits_with_model_specific_audits(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)
    audits_dir = tmp_path / "audits"
    audits_dir.mkdir(exist_ok=True)

    create_temp_file(
        tmp_path,
        audits_dir / "range_check.sql",
        dedent("""
            AUDIT (
                name range_check
            );

            SELECT * FROM @this_model
            WHERE @column < @min_value OR @column > @max_value
        """),
    )

    # Create a model with its own audits in addition to defaults
    create_temp_file(
        tmp_path,
        models_dir / "products.sql",
        dedent("""
            MODEL (
                name test.products,
                kind FULL,
                audits (
                    range_check(column := price, min_value := 0, max_value := 10000)
                )
            );

            SELECT
                1 AS product_id,
                'Widget' AS product_name,
                99.99 AS price
            UNION ALL
            SELECT
                2 AS product_id,
                'Gadget' AS product_name,
                149.99 AS price
        """),
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(
            dialect="duckdb",
            audits=[
                "not_null(columns := [product_id, product_name])",
                "unique_values(columns := [product_id])",
            ],
        )
    )

    context = Context(paths=tmp_path, config=config)

    # Create and apply plan
    plan = context.plan("prod", no_prompts=True)
    context.apply(plan)

    # Verify model has both default and model-specific audits
    model = context.get_model("test.products")
    assert len(model.audits) == 3

    audit_names = [audit[0] for audit in model.audits]
    assert "not_null" in audit_names
    assert "unique_values" in audit_names
    assert "range_check" in audit_names

    # Verify audit execution order, default audits first then model-specific
    assert model.audits[0][0] == "not_null"
    assert model.audits[1][0] == "unique_values"
    assert model.audits[2][0] == "range_check"


@pytest.mark.slow
def test_default_audits_with_custom_audit_definitions(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)
    audits_dir = tmp_path / "audits"
    audits_dir.mkdir(exist_ok=True)

    # Create custom audit definition
    create_temp_file(
        tmp_path,
        audits_dir / "positive_amount.sql",
        dedent("""
            AUDIT (
                name positive_amount
            );

            SELECT * FROM @this_model
            WHERE @column <= 0
        """),
    )

    # Create a model
    create_temp_file(
        tmp_path,
        models_dir / "transactions.sql",
        dedent("""
            MODEL (
                name test.transactions,
                kind FULL
            );

            SELECT
                1 AS transaction_id,
                'TXN001' AS transaction_code,
                250.00 AS amount,
                '2024-01-01'::DATE AS transaction_date
            UNION ALL
            SELECT
                2 AS transaction_id,
                'TXN002' AS transaction_code,
                150.00 AS amount,
                '2024-01-02'::DATE AS transaction_date
        """),
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(
            dialect="duckdb",
            audits=[
                "not_null(columns := [transaction_id, transaction_code])",
                "unique_values(columns := [transaction_id])",
                "positive_amount(column := amount)",
            ],
        )
    )

    context = Context(paths=tmp_path, config=config)

    # Create and apply plan
    plan = context.plan("prod", no_prompts=True)
    context.apply(plan)

    # Verify model has all default audits including custom
    model = context.get_model("test.transactions")
    assert len(model.audits) == 3

    audit_names = [audit[0] for audit in model.audits]
    assert "not_null" in audit_names
    assert "unique_values" in audit_names
    assert "positive_amount" in audit_names

    # Verify custom audit arguments
    for audit_name, audit_args in model.audits:
        if audit_name == "positive_amount":
            assert "column" in audit_args
            assert audit_args["column"].name == "amount"


@pytest.mark.parametrize("forward_only", [False, True])
def test_audit_only_change_reads_parent_tables_of_dev_environment(
    tmp_path: Path, mocker: MockerFixture, forward_only: bool
):
    def write(path: str, contents: str) -> None:
        create_temp_file(tmp_path, Path(path), contents)

    def write_parent(tag: str) -> None:
        kind = "INCREMENTAL_BY_UNIQUE_KEY (unique_key id)"
        write("models/p.sql", f"MODEL (name db.p, kind {kind}); SELECT 1 AS id, '{tag}' AS tag")

    write_parent("p0")
    write("models/c.sql", "MODEL (name db.c, kind FULL); SELECT id FROM db.p")
    context = Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    )
    context.plan("prod", no_prompts=True, auto_apply=True)

    write_parent("p1")
    context.load()
    context.plan("dev", no_prompts=True, auto_apply=True, forward_only=forward_only)

    # An audit-only change to the child, whose audit reads the parent
    write(
        "audits/reads_parent.sql", "AUDIT (name reads_parent); SELECT * FROM db.p WHERE tag <> 'p1'"
    )
    write(
        "models/c.sql", "MODEL (name db.c, kind FULL, audits (reads_parent)); SELECT id FROM db.p"
    )
    context.load()
    plan = context.plan_builder("dev").build()
    assert [s.name for s in plan.new_snapshots] == ['"memory"."db"."c"']
    assert plan.new_snapshots[0].is_metadata

    audit_spy = mocker.spy(SnapshotEvaluator, "audit")
    evaluate_spy = mocker.spy(SnapshotEvaluator, "evaluate")
    context.apply(plan)

    # Only the child is audited, and it reads the parent's dev snapshot rather than the prod view
    assert [call.kwargs["snapshot"].name for call in audit_spy.call_args_list] == [
        '"memory"."db"."c"'
    ]
    assert not evaluate_spy.call_args_list
    assert not any(result.count for result in audit_spy.spy_return)
    dev_p = context.get_snapshot("db.p", raise_if_missing=True)
    audit_kwargs = audit_spy.call_args.kwargs
    assert audit_kwargs["snapshots"]['"memory"."db"."p"'].snapshot_id == dev_p.snapshot_id
    assert audit_kwargs["deployability_index"].is_deployable(dev_p) is not forward_only


def test_audit_only_change_with_non_parent_changed_in_same_plan(
    tmp_path: Path, mocker: MockerFixture
):
    def write(path: str, contents: str) -> None:
        create_temp_file(tmp_path, Path(path), contents)

    write("models/p.sql", "MODEL (name db.p, kind FULL); SELECT 1 AS id")
    write("models/s.sql", "MODEL (name db.s, kind FULL); SELECT 1 AS id, 's0' AS tag")
    write("models/c.sql", "MODEL (name db.c, kind FULL); SELECT id FROM db.p")
    context = Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    )
    context.plan("prod", no_prompts=True, auto_apply=True)

    # The audit reads s, which isn't a parent of c, so s's new table doesn't exist until the backfill
    write("models/s.sql", "MODEL (name db.s, kind FULL); SELECT 1 AS id, 's1' AS tag")
    write("audits/reads_s.sql", "AUDIT (name reads_s); SELECT * FROM db.s WHERE id IS NULL")
    write("models/c.sql", "MODEL (name db.c, kind FULL, audits (reads_s)); SELECT id FROM db.p")
    context.load()
    plan = context.plan_builder("dev").build()
    assert {s.name: s.is_metadata for s in plan.new_snapshots}['"memory"."db"."c"']

    audit_spy = mocker.spy(SnapshotEvaluator, "audit")
    context.apply(plan)

    # c is audited once and its audit finds nothing. spy_return would be s's, from the backfill
    c_results = [
        results
        for call, results in zip(audit_spy.call_args_list, audit_spy.spy_return_list)
        if call.kwargs["snapshot"].name == '"memory"."db"."c"'
    ]
    assert [[result.count for result in results] for results in c_results] == [[0]]


@pytest.mark.parametrize("variant", ["new_parent", "changed_parent"])
def test_audit_only_change_after_failed_prod_plan(tmp_path: Path, variant: str):
    def write(path: str, contents: str) -> None:
        create_temp_file(tmp_path, Path(path), contents)

    def fo(tag: str) -> None:
        write(
            "models/fo.sql",
            "MODEL (name db.fo, kind INCREMENTAL_BY_UNIQUE_KEY (unique_key id, forward_only true));"
            f" SELECT 1 AS id, '{tag}' AS tag",
        )

    config = Config(model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"))
    write("audits/always_fails.sql", "AUDIT (name always_fails); SELECT 1 AS x")
    fo("v0")
    write("models/p.sql", "MODEL (name db.p, kind FULL); SELECT id, 'p0' AS tag FROM db.fo")
    write("models/c.sql", "MODEL (name db.c, kind FULL); SELECT id FROM db.p")
    context = Context(paths=tmp_path, config=config)
    context.plan("prod", no_prompts=True, auto_apply=True)

    # The forward-only change makes the backfill run after the environment record is updated, and
    # the failing audit stops the plan before the virtual layer is updated
    fo("v1")
    parent, new_tag = ("q", "q1") if variant == "new_parent" else ("p", "p1")
    write(
        f"models/{parent}.sql",
        f"MODEL (name db.{parent}, kind FULL); SELECT id, '{new_tag}' AS tag FROM db.fo",
    )
    write(
        "models/c.sql",
        f"MODEL (name db.c, kind FULL, audits (always_fails)); SELECT id FROM db.{parent}",
    )
    context.load()
    with pytest.raises(PlanError):
        context.plan("prod", no_prompts=True, auto_apply=True)

    # The audit must read the parent version named by the environment record
    write(
        "audits/reads_parent.sql",
        f"AUDIT (name reads_parent); SELECT * FROM db.{parent} WHERE tag <> '{new_tag}'",
    )
    write(
        "models/c.sql",
        f"MODEL (name db.c, kind FULL, audits (reads_parent)); SELECT id FROM db.{parent}",
    )
    context.load()
    context.plan("prod", no_prompts=True, auto_apply=True)

    assert context.engine_adapter.fetchall(f"SELECT tag FROM db.{parent}") == [(new_tag,)]


def test_audit_only_change_keeps_intervals_of_snapshots_backfilled_in_same_plan(tmp_path: Path):
    def write_models(start: str, audits: str = "") -> None:
        # Both models replace their whole table when they think it's their first insert
        kind = f"INCREMENTAL_BY_UNIQUE_KEY (unique_key ds), start '{start}', cron '@daily'"
        create_temp_file(
            tmp_path,
            Path("models/p.sql"),
            f"MODEL (name db.p, kind {kind}); SELECT @start_ds AS ds",
        )
        create_temp_file(
            tmp_path,
            Path("models/c.sql"),
            f"MODEL (name db.c, kind {kind}{audits}); SELECT DISTINCT @start_ds AS ds FROM db.p",
        )

    write_models(start="2023-01-02")
    context = Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb"))
    )
    with time_machine.travel("2023-01-03 00:00:00 UTC"):
        context.plan("prod", no_prompts=True, auto_apply=True)

        # Moving the start back is a metadata change that backfills the earlier day in the same
        # plan that audits the child
        write_models(start="2023-01-01", audits=", audits (not_null(columns := (ds)))")
        context.load()
        context.plan("prod", no_prompts=True, auto_apply=True)

    expected = [("2023-01-01",), ("2023-01-02",)]
    assert sorted(context.engine_adapter.fetchall("SELECT ds FROM db.p")) == expected
    assert sorted(context.engine_adapter.fetchall("SELECT ds FROM db.c")) == expected
