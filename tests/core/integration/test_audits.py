from __future__ import annotations

import typing as t
from textwrap import dedent
import pytest
from pathlib import Path
import time_machine
from sqlglot import exp
from IPython.utils.capture import capture_output

from sqlmesh.core.config import (
    Config,
    ModelDefaultsConfig,
)
from sqlmesh.core.context import Context
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


@pytest.mark.slow
def test_skip_audits_bypasses_blocking_audit(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)

    create_temp_file(
        tmp_path,
        models_dir / "bad_model.sql",
        dedent("""
            MODEL (
                name test.bad_model,
                kind FULL
            );

            SELECT NULL AS customer_id
        """),
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(
            dialect="duckdb", audits=["not_null(columns := [customer_id])"]
        )
    )

    context = Context(paths=tmp_path, config=config)

    # Without skip_audits, the blocking audit causes a PlanError
    with pytest.raises(PlanError):
        context.plan("prod", no_prompts=True, auto_apply=True)

    # With skip_audits=True, the plan succeeds even though the audit would fail
    context.plan("prod", no_prompts=True, auto_apply=True, skip_audits=True)
