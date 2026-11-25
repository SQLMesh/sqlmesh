from __future__ import annotations

import typing as t
from textwrap import dedent
import pytest
from pathlib import Path
from pytest_mock.plugin import MockerFixture
import time_machine
from sqlglot import exp
from IPython.utils.capture import capture_output

from sqlmesh.core.config import (
    Config,
    ModelDefaultsConfig,
)
from sqlmesh.core.context import Context
from sqlmesh.core.plan.evaluator import BuiltInPlanEvaluator
from sqlmesh.core.snapshot.evaluator import SnapshotEvaluator
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


_MATCHES_PARENT_AUDIT = """
    AUDIT (
        name matches_parent
    );

    SELECT a.id
    FROM @this_model AS a
    LEFT JOIN @parent_model AS b
        ON a.id = b.id
    WHERE b.id IS NULL
"""

_EXPECT_VAL_AUDIT = """
    AUDIT (
        name expect_val
    );

    SELECT *
    FROM @this_model
    WHERE val != @expected
"""


def _write_files(root: Path, files: t.Dict[str, str]) -> None:
    for rel_path, content in files.items():
        create_temp_file(root, Path(rel_path), dedent(content))


def test_audit_only_resolves_new_parent_after_failed_prod_plan(
    tmp_path: Path, mocker: MockerFixture
):
    """Issue #6256: Pure metadata audit-only plan resolves a NEW parent whose prod view was not promoted."""
    _write_files(
        tmp_path,
        {
            "audits/matches_parent.sql": _MATCHES_PARENT_AUDIT,
            "models/base.sql": """
                MODEL (name test.base, kind FULL);
                SELECT 1 AS id
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    with time_machine.travel("2024-01-02 00:00:00 UTC"):
        context.plan("prod", no_prompts=True, auto_apply=True)

        # Plan 1: backfills new test.p -> test.c in before_promote, runs
        # EnvironmentRecordUpdateStage, then fails in after_promote on new forward-only model
        # test.fo before VirtualLayerUpdateStage creates views.
        _write_files(
            tmp_path,
            {
                "models/p.sql": """
                    MODEL (name test.p, kind FULL);
                    SELECT id FROM test.base
                """,
                "models/c.sql": """
                    MODEL (name test.c, kind FULL);
                    SELECT id FROM test.p
                """,
                "models/fo.sql": """
                    MODEL (
                        name test.fo,
                        kind INCREMENTAL_BY_TIME_RANGE (time_column ds, forward_only true),
                        start '2024-01-01',
                        cron '@daily',
                        audits (not_null(columns := [id]))
                    );
                    SELECT NULL AS id, @start_ds::DATE AS ds
                """,
            },
        )
        context.load()

        with pytest.raises(PlanError):
            context.plan("prod", no_prompts=True, auto_apply=True)

        assert not context.engine_adapter.table_exists("test.p")

        # Plan 2: remove failing test.fo and add a metadata-only audit on test.c referencing its
        # parent test.p -> pure metadata audit plan with zero backfill.
        (tmp_path / "models" / "fo.sql").unlink()
        _write_files(
            tmp_path,
            {
                "models/c.sql": """
                    MODEL (
                        name test.c,
                        kind FULL,
                        audits (matches_parent(parent_model := test.p))
                    );
                    SELECT id FROM test.p
                """,
            },
        )
        context.load()

        audit_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_audit_only_run_stage")
        backfill_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_backfill_stage")
        context.plan("prod", no_prompts=True, auto_apply=True)

        assert audit_stage_spy.call_count == 1
        assert backfill_stage_spy.call_count == 1
        assert not backfill_stage_spy.call_args.args[1].snapshot_to_intervals
        assert context.engine_adapter.fetchall("SELECT id FROM test.p") == [(1,)]


def test_audit_only_resolves_changed_parent_after_failed_prod_plan(
    tmp_path: Path, mocker: MockerFixture
):
    """Issue #6256: Pure metadata audit-only plan resolves a CHANGED parent instead of its stale prod view."""
    _write_files(
        tmp_path,
        {
            "audits/matches_parent.sql": _MATCHES_PARENT_AUDIT,
            "models/p.sql": """
                MODEL (name test.p, kind FULL);
                SELECT 1 AS id
            """,
            "models/c.sql": """
                MODEL (name test.c, kind FULL);
                SELECT id FROM test.p
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    with time_machine.travel("2024-01-02 00:00:00 UTC"):
        context.plan("prod", no_prompts=True, auto_apply=True)

        # Plan 1: breaking change to test.p (id=2) backfills test.p and test.c in before_promote,
        # promotes environment record, then fails in after_promote on new forward-only model
        # test.fo before VirtualLayerUpdateStage updates prod views.
        _write_files(
            tmp_path,
            {
                "models/p.sql": """
                    MODEL (name test.p, kind FULL);
                    SELECT 2 AS id
                """,
                "models/fo.sql": """
                    MODEL (
                        name test.fo,
                        kind INCREMENTAL_BY_TIME_RANGE (time_column ds, forward_only true),
                        start '2024-01-01',
                        cron '@daily',
                        audits (not_null(columns := [id]))
                    );
                    SELECT NULL AS id, @start_ds::DATE AS ds
                """,
            },
        )
        context.load()

        with pytest.raises(PlanError):
            context.plan("prod", no_prompts=True, auto_apply=True)

        # Prod view test.p is still stale (id=1) while physical tables for test.p and test.c have id=2.
        assert context.engine_adapter.fetchall("SELECT id FROM test.p") == [(1,)]

        # Plan 2: remove failing test.fo and add a metadata-only audit on test.c referencing test.p
        # (no backfill). Reading stale view test.p (id=1) would fail against test.c (id=2);
        # resolving physical table passes.
        (tmp_path / "models" / "fo.sql").unlink()
        _write_files(
            tmp_path,
            {
                "models/c.sql": """
                    MODEL (
                        name test.c,
                        kind FULL,
                        audits (matches_parent(parent_model := test.p))
                    );
                    SELECT id FROM test.p
                """,
            },
        )
        context.load()

        audit_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_audit_only_run_stage")
        backfill_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_backfill_stage")
        context.plan("prod", no_prompts=True, auto_apply=True)

        assert audit_stage_spy.call_count == 1
        assert backfill_stage_spy.call_count == 1
        assert not backfill_stage_spy.call_args.args[1].snapshot_to_intervals
        assert context.engine_adapter.fetchall("SELECT id FROM test.p") == [(2,)]


@pytest.mark.parametrize("select_models", [None, ["test.target"]])
def test_audit_only_dev_breaking_parent(tmp_path: Path, select_models: t.Optional[t.List[str]]):
    """Audit-only change in dev referencing a parent with a breaking change in dev (with & without --select-model)."""
    _write_files(
        tmp_path,
        {
            "audits/matches_parent.sql": _MATCHES_PARENT_AUDIT,
            "models/parent.sql": """
                MODEL (name test.parent, kind FULL);
                SELECT 1 AS id
            """,
            "models/target.sql": """
                MODEL (name test.target, kind FULL);
                SELECT id FROM test.parent
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    context.plan("prod", no_prompts=True, auto_apply=True)

    # Breaking change to test.parent in dev -> dev tables for test.parent and test.target have id=2,
    # while prod view test.parent still has id=1.
    _write_files(
        tmp_path,
        {
            "models/parent.sql": """
                MODEL (name test.parent, kind FULL);
                SELECT 2 AS id
            """,
        },
    )
    context.load()
    context.plan("dev", no_prompts=True, auto_apply=True)

    # Metadata-only audit on test.target referencing test.parent must resolve dev's physical table (id=2).
    _write_files(
        tmp_path,
        {
            "models/target.sql": """
                MODEL (
                    name test.target,
                    kind FULL,
                    audits (matches_parent(parent_model := test.parent))
                );
                SELECT id FROM test.parent
            """,
        },
    )
    context.load()
    context.plan("dev", select_models=select_models, no_prompts=True, auto_apply=True)


def test_audit_only_dev_paused_forward_only_grandparent_preview(
    tmp_path: Path, mocker: MockerFixture
):
    """Audit-only change in dev when grandparent is a paused forward-only model must audit dev preview tables."""
    _write_files(
        tmp_path,
        {
            "audits/expect_val.sql": _EXPECT_VAL_AUDIT,
            "models/grandparent.sql": """
                MODEL (name test.grandparent, kind FULL);
                SELECT 1 AS val
            """,
            "models/parent.sql": """
                MODEL (name test.parent, kind FULL);
                SELECT val FROM test.grandparent
            """,
            "models/child.sql": """
                MODEL (name test.child, kind FULL);
                SELECT val FROM test.parent
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    context.plan("prod", no_prompts=True, auto_apply=True)

    # Forward-only change to grandparent in dev populates non-deployable dev preview tables (val=2)
    # while prod tables retain val=1.
    _write_files(
        tmp_path,
        {
            "models/grandparent.sql": """
                MODEL (name test.grandparent, kind FULL);
                SELECT 2 AS val
            """,
        },
    )
    context.load()
    context.plan("dev", forward_only=True, no_prompts=True, auto_apply=True)
    assert context.engine_adapter.fetchall("SELECT val FROM test__dev.child") == [(2,)]
    assert context.engine_adapter.fetchall("SELECT val FROM test.child") == [(1,)]

    # Metadata-only audit on test.child in dev must audit its populated dev preview table (val=2).
    _write_files(
        tmp_path,
        {
            "models/child.sql": """
                MODEL (
                    name test.child,
                    kind FULL,
                    audits (expect_val(expected := 2, blocking := true))
                );
                SELECT val FROM test.parent
            """,
        },
    )
    context.load()

    audit_spy = mocker.spy(SnapshotEvaluator, "_audit")
    context.plan("dev", forward_only=True, no_prompts=True, auto_apply=True)

    assert audit_spy.call_count == 1
    audit_kwargs = audit_spy.call_args.kwargs
    child_snapshot = audit_kwargs["snapshot"]
    deployability_index = audit_kwargs["deployability_index"]
    assert not deployability_index.is_deployable(child_snapshot)
    audit_result = audit_spy.spy_return
    assert audit_result.count == 0
    expected_dev_table = exp.to_table(
        child_snapshot.table_name(is_deployable=False), dialect="duckdb"
    ).sql(dialect="duckdb", identify=True)
    assert expected_dev_table in audit_result.query.sql(dialect="duckdb")


def test_audit_only_embedded_parent(tmp_path: Path):
    """Audit-only change referencing an embedded parent (with transitive embedded parent)."""
    _write_files(
        tmp_path,
        {
            "audits/matches_parent.sql": _MATCHES_PARENT_AUDIT,
            "models/emb_parent.sql": """
                MODEL (name test.emb_parent, kind EMBEDDED);
                SELECT 1 AS id
            """,
            "models/emb_mid.sql": """
                MODEL (name test.emb_mid, kind EMBEDDED);
                SELECT id FROM test.emb_parent
            """,
            "models/full_child.sql": """
                MODEL (name test.full_child, kind FULL);
                SELECT id FROM test.emb_mid
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    context.plan("prod", no_prompts=True, auto_apply=True)

    _write_files(
        tmp_path,
        {
            "models/full_child.sql": """
                MODEL (
                    name test.full_child,
                    kind FULL,
                    audits (matches_parent(parent_model := test.emb_mid))
                );
                SELECT id FROM test.emb_mid
            """,
        },
    )
    context.load()
    context.plan("prod", no_prompts=True, auto_apply=True)


def test_audit_only_external_model(tmp_path: Path):
    """Audit-only change on an EXTERNAL model runs its updated audit."""
    _write_files(
        tmp_path,
        {
            "models/ext_source.sql": """
                MODEL (
                    name test.ext_source,
                    kind EXTERNAL,
                    columns (id INT),
                    audits (not_null(columns := [id]))
                );
                SELECT 1;
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    context.engine_adapter.create_schema("test")
    context.engine_adapter.execute("CREATE TABLE test.ext_source AS SELECT 1 AS id")
    context.plan("prod", no_prompts=True, auto_apply=True)

    context.engine_adapter.execute("INSERT INTO test.ext_source VALUES (1)")
    _write_files(
        tmp_path,
        {
            "models/ext_source.sql": """
                MODEL (
                    name test.ext_source,
                    kind EXTERNAL,
                    columns (id INT),
                    audits (
                        not_null(columns := [id]),
                        unique_values(columns := [id])
                    )
                );
                SELECT 1;
            """,
        },
    )
    context.load()

    with pytest.raises(PlanError):
        context.plan("prod", no_prompts=True, auto_apply=True)


def test_audit_only_preserves_incremental_by_unique_key_historical_rows(
    tmp_path: Path, mocker: MockerFixture
):
    """Running AuditOnlyRunStage + BackfillStage in the same plan must not wipe historical rows."""
    _write_files(
        tmp_path,
        {
            "models/inc_unique.sql": """
                MODEL (
                    name test.inc_unique,
                    kind INCREMENTAL_BY_UNIQUE_KEY (unique_key id),
                    start '2024-01-01',
                    cron '@daily'
                );
                SELECT @start_ds::TEXT AS id, @start_ds::DATE AS ds
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    with time_machine.travel("2024-01-02 00:00:00 UTC"):
        context.plan("prod", no_prompts=True, auto_apply=True)

    assert sorted(context.engine_adapter.fetchall("SELECT id FROM test.inc_unique")) == [
        ("2024-01-01",)
    ]

    _write_files(
        tmp_path,
        {
            "models/inc_unique.sql": """
                MODEL (
                    name test.inc_unique,
                    kind INCREMENTAL_BY_UNIQUE_KEY (unique_key id),
                    start '2024-01-01',
                    cron '@daily',
                    audits (not_null(columns := [id]))
                );
                SELECT @start_ds::TEXT AS id, @start_ds::DATE AS ds
            """,
        },
    )
    context.load()

    audit_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_audit_only_run_stage")
    backfill_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_backfill_stage")
    with time_machine.travel("2024-01-03 00:00:00 UTC"):
        context.plan("prod", run=True, no_prompts=True, auto_apply=True)

    assert audit_stage_spy.call_count == 1
    assert backfill_stage_spy.call_count == 1
    assert backfill_stage_spy.call_args.args[1].snapshot_to_intervals
    assert sorted(context.engine_adapter.fetchall("SELECT id FROM test.inc_unique")) == [
        ("2024-01-01",),
        ("2024-01-02",),
    ]


def test_audit_only_does_not_resolve_uncreated_sibling_table(tmp_path: Path, mocker: MockerFixture):
    """AuditOnlyRunStage before BackfillStage must not resolve a non-parent breaking sibling to its uncreated table."""
    _write_files(
        tmp_path,
        {
            "audits/matches_parent.sql": _MATCHES_PARENT_AUDIT,
            "models/other.sql": """
                MODEL (name test.other, kind FULL);
                SELECT 1 AS id
            """,
            "models/target.sql": """
                MODEL (name test.target, kind FULL);
                SELECT 1 AS id
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    context.plan("prod", no_prompts=True, auto_apply=True)

    # Single plan: breaking change to unrelated sibling test.other (needs backfill, physical table
    # not yet created during AuditOnlyRunStage) + metadata audit on test.target referencing test.other.
    _write_files(
        tmp_path,
        {
            "models/other.sql": """
                MODEL (name test.other, kind FULL);
                SELECT 2 AS id
            """,
            "models/target.sql": """
                MODEL (
                    name test.target,
                    kind FULL,
                    audits (matches_parent(parent_model := test.other))
                );
                SELECT 1 AS id
            """,
        },
    )
    context.load()

    audit_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_audit_only_run_stage")
    backfill_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_backfill_stage")
    context.plan("prod", no_prompts=True, auto_apply=True)

    assert audit_stage_spy.call_count == 1
    assert backfill_stage_spy.call_count == 1
    assert backfill_stage_spy.call_args.args[1].snapshot_to_intervals
    assert context.engine_adapter.fetchall("SELECT id FROM test.other") == [(2,)]


def test_audit_only_dev_non_representative_without_dev_intervals(
    tmp_path: Path, mocker: MockerFixture
):
    """Non-representative dev METADATA models without dev_intervals skip AuditOnlyRunStage and audit in BackfillStage."""
    _write_files(
        tmp_path,
        {
            "audits/expect_val.sql": _EXPECT_VAL_AUDIT,
            "models/p.sql": """
                MODEL (
                    name test.p,
                    kind INCREMENTAL_BY_TIME_RANGE (
                        time_column ds,
                        auto_restatement_cron '@daily'
                    ),
                    start '2024-01-01'
                );
                SELECT 1 AS val, '2024-01-01'::DATE AS ds
            """,
            "models/c_full.sql": """
                MODEL (name test.c_full, kind FULL, start '2024-01-01');
                SELECT val, ds FROM test.p
            """,
            "models/c_inc.sql": """
                MODEL (
                    name test.c_inc,
                    kind INCREMENTAL_BY_TIME_RANGE (time_column ds),
                    start '2024-01-01'
                );
                SELECT val, ds FROM test.p WHERE ds BETWEEN @start_ds AND @end_ds
            """,
        },
    )

    context = Context(paths=tmp_path, config=Config())
    with time_machine.travel("2024-01-02 00:00:00 UTC"):
        context.plan("prod", no_prompts=True, auto_apply=True)

        # Dev plan 1: add metadata-only audits to c_full and c_inc (both non-representative because
        # test.p has auto_restatement_cron, and neither has dev_intervals yet).
        _write_files(
            tmp_path,
            {
                "models/c_full.sql": """
                    MODEL (
                        name test.c_full,
                        kind FULL,
                        start '2024-01-01',
                        audits (expect_val(expected := 1, blocking := true))
                    );
                    SELECT val, ds FROM test.p
                """,
                "models/c_inc.sql": """
                    MODEL (
                        name test.c_inc,
                        kind INCREMENTAL_BY_TIME_RANGE (time_column ds),
                        start '2024-01-01',
                        audits (expect_val(expected := 1, blocking := true))
                    );
                    SELECT val, ds FROM test.p WHERE ds BETWEEN @start_ds AND @end_ds
                """,
            },
        )
        context.load()

        audit_stage_spy = mocker.spy(BuiltInPlanEvaluator, "visit_audit_only_run_stage")
        audit_spy = mocker.spy(SnapshotEvaluator, "_audit")
        context.plan("dev", no_prompts=True, auto_apply=True)

        assert audit_stage_spy.call_count == 0
        assert audit_spy.call_count == 2
        assert context.engine_adapter.fetchall("SELECT val FROM test__dev.c_full") == [(1,)]
        assert context.engine_adapter.fetchall("SELECT val FROM test__dev.c_inc") == [(1,)]

        # Dev plan 2: c_full now has populated dev_intervals, so a subsequent audit-only change
        # runs in AuditOnlyRunStage against its existing __dev table.
        _write_files(
            tmp_path,
            {
                "models/c_full.sql": """
                    MODEL (
                        name test.c_full,
                        kind FULL,
                        start '2024-01-01',
                        audits (
                            expect_val(expected := 1, blocking := true),
                            not_null(columns := [val])
                        )
                    );
                    SELECT val, ds FROM test.p
                """,
            },
        )
        context.load()

        audit_stage_spy.reset_mock()
        context.plan("dev", no_prompts=True, auto_apply=True)
        assert audit_stage_spy.call_count == 1
