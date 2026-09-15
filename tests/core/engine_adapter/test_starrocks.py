"""Tests for StarRocks Engine Adapter

This test suite covers the StarRocks-specific functionality of the engine adapter,
including schema operations, table operations, and StarRocks-specific table properties.

Test classes are organized by functionality (following the standard order):
- TestSchemaOperations: Schema/Database operations
- TestTableOperations: Basic table operations
- TestKeyPropertyBuilding: Table key types (primary_key, duplicate_key, unique_key, aggregate_key)
- TestPartitionPropertyBuilding: Partition (partitioned_by, partitions)
- TestDistributionPropertyBuilding: Distribution (distributed_by)
- TestOrderByPropertyBuilding: Order By (order_by, clustered_by)
- TestCommentPropertyBuilding: Comments (table and column)
- TestGenericPropertyBuilding: Generic properties (replication_num, etc.)
- TestComprehensive: Comprehensive tests with all features combined

Unit tests use @pytest.mark.parametrize to systematically cover all value forms.
"""

import typing as t

import pytest
from sqlglot import expressions as exp
from sqlglot import parse_one
from pytest_mock.plugin import MockerFixture
from sqlmesh.core.engine_adapter.shared import DataObjectType
from sqlmesh.utils.errors import SQLMeshError

from tests.core.engine_adapter import to_sql_calls
from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.engine_adapter.starrocks import StarRocksEngineAdapter
from sqlmesh.core.engine_adapter.duckdb import DuckDBEngineAdapter
from sqlmesh.core.dialect import parse
from sqlmesh.core.model import load_sql_based_model, SqlModel
from sqlmesh.core.snapshot.definition import (
    DeployabilityIndex,
    Snapshot,
    SnapshotChangeCategory,
)
from sqlmesh.core.snapshot.evaluator import _adjust_physical_properties_for_engine

pytestmark = [pytest.mark.starrocks, pytest.mark.engine]


def _load_sql_model(model_sql: str) -> SqlModel:
    """Parse StarRocks MODEL SQL into a SqlModel instance."""
    expressions = parse(model_sql, default_dialect="starrocks")
    return t.cast(SqlModel, load_sql_based_model(expressions))


def _columns(model: SqlModel) -> t.Dict[str, exp.DataType]:
    assert model.columns_to_types is not None
    return model.columns_to_types


# =============================================================================
# Schema Operations
# =============================================================================
class TestSchemaOperations:
    """Tests for schema (database) operations."""

    def test_create_schema(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE DATABASE statement generation.

        StarRocks uses DATABASE keyword (MySQL-style) instead of SCHEMA.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_schema("test_schema")

        assert to_sql_calls(adapter) == [
            "CREATE SCHEMA IF NOT EXISTS `test_schema`",
        ]

    def test_create_schema_without_if_exists(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE DATABASE without IF NOT EXISTS clause."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_schema("test_schema", ignore_if_exists=False)

        assert to_sql_calls(adapter) == [
            "CREATE SCHEMA `test_schema`",
        ]

    def test_drop_schema(self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]):
        """Test DROP DATABASE statement generation."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.drop_schema("test_schema")
        adapter.drop_schema("test_schema", ignore_if_not_exists=False)

        assert to_sql_calls(adapter) == [
            "DROP SCHEMA IF EXISTS `test_schema`",
            "DROP SCHEMA `test_schema`",
        ]


# =============================================================================
# Data Object Query (MV vs VIEW)
# =============================================================================
class TestDataObjectQuery:
    def test_get_data_object_materialized_view_is_distinguished_from_view(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        mocker: MockerFixture,
    ) -> None:
        """
        StarRocks may report materialized views as TABLE_TYPE='VIEW' in information_schema.tables.
        Ensure StarRocksEngineAdapter upgrades MV objects using information_schema.materialized_views.
        """
        import pandas as pd

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter, patch_get_data_objects=False)

        # information_schema.tables output (MV appears as 'view')
        # fetchdf is called twice:
        # 1) information_schema.tables
        # 2) information_schema.materialized_views
        tables_df = pd.DataFrame(
            [
                {"schema_name": "test_db", "name": "mv1", "type": "view"},
                {"schema_name": "test_db", "name": "mv2", "type": "view"},
                {"schema_name": "test_db", "name": "v1", "type": "view"},
                {"schema_name": "test_db", "name": "t1", "type": "table"},
            ]
        )
        mv_df = pd.DataFrame(
            [
                {"schema_name": "test_db", "name": "mv1"},
                {"schema_name": "test_db", "name": "mv2"},
            ]
        )

        known_names = ["mv1", "mv2", "v1", "t1"]

        def fetchdf_side_effect(query: exp.Expression, *_: t.Any, **__: t.Any):
            query_sql = query.sql(dialect="starrocks").lower()
            requested = [
                name for name in known_names if f"'{name}'" in query_sql or f"`{name}`" in query_sql
            ]
            if "information_schema.materialized_views" in query_sql:
                df = mv_df
            else:
                df = tables_df
            if requested:
                mask = df["name"].str.lower().isin(requested)
                return df[mask].reset_index(drop=True)
            return df.reset_index(drop=True)

        adapter.fetchdf = mocker.Mock(side_effect=fetchdf_side_effect)  # type: ignore[assignment]

        mv1 = adapter.get_data_object("test_db.mv1")
        assert mv1 is not None
        assert mv1.type == DataObjectType.MATERIALIZED_VIEW

        v1 = adapter.get_data_object("test_db.v1")
        assert v1 is not None
        assert v1.type == DataObjectType.VIEW

        mv2_objects = adapter.get_data_objects(schema_name="test_db", object_names={"mv2"})
        assert len(mv2_objects) == 1
        assert mv2_objects[0].name.lower() == "mv2"
        assert mv2_objects[0].type == DataObjectType.MATERIALIZED_VIEW


# =============================================================================
# Basic Table Operations
# =============================================================================
class TestTableOperations:
    """Tests for basic table operations."""

    def test_create_table(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test basic CREATE TABLE statement generation."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            "test_table",
            target_columns_to_types={
                "a": exp.DataType.build("INT"),
                "b": exp.DataType.build("VARCHAR(100)"),
            },
        )

        sql = to_sql_calls(adapter)[0]
        assert "CREATE TABLE IF NOT EXISTS `test_table`" in sql
        assert "`a` INT" in sql
        assert "`b` VARCHAR(100)" in sql

    def test_create_table_like(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE TABLE LIKE statement."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table_like("target_table", "source_table")
        assert to_sql_calls(adapter) == [
            "CREATE TABLE IF NOT EXISTS `target_table` LIKE `source_table`",
        ]

    def test_create_table_like_exists_false(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE TABLE LIKE with exists=False (no IF NOT EXISTS)."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table_like("target_table", "source_table", exists=False)
        assert to_sql_calls(adapter) == [
            "CREATE TABLE `target_table` LIKE `source_table`",
        ]

    def test_create_table_like_qualified_names(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE TABLE LIKE with database-qualified names."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table_like("db.target_table", "db.source_table")
        assert to_sql_calls(adapter) == [
            "CREATE TABLE IF NOT EXISTS `db`.`target_table` LIKE `db`.`source_table`",
        ]

    def test_create_table_like_does_not_call_columns(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        mocker: MockerFixture,
    ):
        """
        StarRocks overrides _create_table_like to use native CREATE TABLE LIKE and should
        not fall back to the base implementation (which calls columns(source)).
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        columns_mock = mocker.patch.object(
            adapter, "columns", side_effect=AssertionError("columns() should not be called")
        )

        adapter.create_table_like("target_table", "source_table")
        assert columns_mock.call_count == 0

    def test_create_table_like_clears_cache(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        mocker: MockerFixture,
    ):
        """create_table_like should clear the data object cache for the target table."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        clear_cache = mocker.patch.object(adapter, "_clear_data_object_cache")

        adapter.create_table_like("target_table", "source_table")
        clear_cache.assert_called_once_with("target_table")

    def test_rename_table(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test RENAME TABLE statement."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        # Test 1: Simple table names (no database qualifier)
        adapter.rename_table("old_table", "new_table")
        adapter.cursor.execute.assert_called_with("ALTER TABLE `old_table` RENAME `new_table`")

        # Test 2: Database-qualified names - RENAME only uses table name
        adapter.cursor.execute.reset_mock()
        adapter.rename_table("db.old_table", "db.new_table")
        # StarRocks RENAME clause requires unqualified table name
        adapter.cursor.execute.assert_called_with("ALTER TABLE `db`.`old_table` RENAME `new_table`")

    def test_delete_from(self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]):
        """Test DELETE statement generation."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.delete_from(exp.to_table("test_table"), "id = 1")

        assert to_sql_calls(adapter) == [
            "DELETE FROM `test_table` WHERE `id` = 1",
        ]

    def test_create_index(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE INDEX statement - StarRocks doesn't support standalone indexes."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_index("test_table", "idx_name", ("cola",))

        # StarRocks skips index creation - verify no execute call was made
        adapter.cursor.execute.assert_not_called()

    def test_create_view(self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]):
        """Test CREATE VIEW statement generation."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view("test_view", parse_one("SELECT a FROM tbl"))
        adapter.create_view("test_view", parse_one("SELECT a FROM tbl"), replace=False)

        assert to_sql_calls(adapter) == [
            "CREATE OR REPLACE VIEW `test_view` AS SELECT `a` FROM `tbl`",
            "CREATE VIEW `test_view` AS SELECT `a` FROM `tbl`",
        ]

    def test_create_view_with_security(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE VIEW with StarRocks SECURITY property."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view(
            "test_view",
            parse_one("SELECT a FROM tbl"),
            replace=False,
            view_properties={"security": exp.Var(this="INVOKER")},
        )

        sql = to_sql_calls(adapter)[0]
        assert "SECURITY INVOKER" in sql

    def test_create_materialized_view_replace_with_refresh_and_comments(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE MATERIALIZED VIEW generation (drop+create, refresh, comments, schema)."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view(
            "test_mv",
            parse_one("SELECT a FROM tbl"),
            materialized=True,
            target_columns_to_types={"a": exp.DataType.build("INT")},
            table_description="Test MV description",
            column_descriptions={"a": "Column A description"},
            view_properties={
                "refresh_moment": exp.Var(this="IMMEDIATE"),
                "refresh_scheme": exp.Literal.string(
                    "ASYNC START ('2025-01-01 00:00:00') EVERY (INTERVAL 5 MINUTE)"
                ),
            },
        )

        calls = to_sql_calls(adapter)
        assert calls[0] == "DROP MATERIALIZED VIEW IF EXISTS `test_mv`"
        assert "CREATE MATERIALIZED VIEW" in calls[1]
        assert "COMMENT 'Test MV description'" in calls[1]
        assert "COMMENT 'Column A description'" in calls[1]
        assert "REFRESH IMMEDIATE ASYNC" in calls[1]
        assert "START ('2025-01-01 00:00:00')" in calls[1]
        assert "EVERY (INTERVAL 5 MINUTE)" in calls[1]

    def test_create_materialized_view_without_refresh_raises(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """StarRocks only supports ASYNC MVs, which require a REFRESH clause.

        Creating an MV without refresh_moment/refresh_scheme must raise rather than silently
        producing an undetectable synchronous MV.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        with pytest.raises(SQLMeshError, match="require a REFRESH clause"):
            adapter.create_view(
                "test_mv",
                parse_one("SELECT a FROM tbl"),
                materialized=True,
                target_columns_to_types={"a": exp.DataType.build("INT")},
                view_properties={"replication_num": exp.Literal.string("1")},
            )

    def test_create_materialized_view_with_audits_emits_sync_refresh(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """When an MV has audits, SQLMesh must synchronously refresh it right after creation.

        Audits require data to exist in the MV. With REFRESH DEFERRED, StarRocks does not populate
        the MV on creation, so SQLMesh issues an explicit `REFRESH MATERIALIZED VIEW ... WITH SYNC
        MODE` that blocks until the data is materialized.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view(
            "test_mv",
            parse_one("SELECT a FROM tbl"),
            materialized=True,
            target_columns_to_types={"a": exp.DataType.build("INT")},
            materialized_properties={"has_audits": True},
            view_properties={
                "refresh_moment": exp.Var(this="DEFERRED"),
                "refresh_scheme": exp.Var(this="ASYNC"),
            },
        )

        calls = to_sql_calls(adapter)
        assert calls[0] == "DROP MATERIALIZED VIEW IF EXISTS `test_mv`"
        assert "CREATE MATERIALIZED VIEW" in calls[1]
        assert "REFRESH DEFERRED" in calls[1]
        assert calls[2] == "REFRESH MATERIALIZED VIEW `test_mv` WITH SYNC MODE"

    def test_create_materialized_view_with_audits_emits_sync_refresh_on_first_create(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """The sync refresh must also fire on first-time creation (replace=False).

        ViewStrategy.create calls create_view with replace=False, so the DROP is skipped, but the
        synchronous refresh still needs to populate the MV before audits run.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view(
            "test_mv",
            parse_one("SELECT a FROM tbl"),
            replace=False,
            materialized=True,
            target_columns_to_types={"a": exp.DataType.build("INT")},
            materialized_properties={"has_audits": True},
            view_properties={
                "refresh_moment": exp.Var(this="DEFERRED"),
                "refresh_scheme": exp.Var(this="ASYNC"),
            },
        )

        calls = to_sql_calls(adapter)
        assert all("DROP MATERIALIZED VIEW" not in sql for sql in calls)
        assert "CREATE MATERIALIZED VIEW" in calls[0]
        assert "REFRESH DEFERRED" in calls[0]
        assert calls[1] == "REFRESH MATERIALIZED VIEW `test_mv` WITH SYNC MODE"

    def test_create_materialized_view_with_audits_immediate_refresh_raises(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """An MV with audits must use REFRESH DEFERRED; IMMEDIATE must raise and not create anything."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        with pytest.raises(SQLMeshError, match="DEFERRED"):
            adapter.create_view(
                "test_mv",
                parse_one("SELECT a FROM tbl"),
                materialized=True,
                target_columns_to_types={"a": exp.DataType.build("INT")},
                materialized_properties={"has_audits": True},
                view_properties={
                    "refresh_moment": exp.Var(this="IMMEDIATE"),
                    "refresh_scheme": exp.Var(this="ASYNC"),
                },
            )

        # Fail-fast: nothing should have been dropped or created.
        assert all("CREATE MATERIALIZED VIEW" not in sql for sql in to_sql_calls(adapter))
        assert all("DROP MATERIALIZED VIEW" not in sql for sql in to_sql_calls(adapter))

    def test_create_materialized_view_with_audits_missing_refresh_moment_raises(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """A missing refresh_moment defaults to IMMEDIATE in StarRocks, so audits must raise."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        with pytest.raises(SQLMeshError, match="DEFERRED"):
            adapter.create_view(
                "test_mv",
                parse_one("SELECT a FROM tbl"),
                materialized=True,
                target_columns_to_types={"a": exp.DataType.build("INT")},
                materialized_properties={"has_audits": True},
                view_properties={"refresh_scheme": exp.Var(this="ASYNC")},
            )

    def test_create_materialized_view_without_audits_does_not_sync_refresh(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Without audits, no synchronous refresh is issued and IMMEDIATE refresh is allowed."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view(
            "test_mv",
            parse_one("SELECT a FROM tbl"),
            materialized=True,
            target_columns_to_types={"a": exp.DataType.build("INT")},
            materialized_properties={"has_audits": False},
            view_properties={
                "refresh_moment": exp.Var(this="IMMEDIATE"),
                "refresh_scheme": exp.Var(this="ASYNC"),
            },
        )

        assert all("WITH SYNC MODE" not in sql for sql in to_sql_calls(adapter))

    def test_does_not_recreate_materialized_view_on_evaluation(self):
        """StarRocks async MVs maintain themselves, so SQLMesh must not recreate them on every run.

        The adapter opts out of per-evaluation recreation via
        RECREATE_MATERIALIZED_VIEW_ON_EVALUATION = False, which the evaluator's ViewStrategy honors
        for materialized views that already exist.
        """
        assert StarRocksEngineAdapter.RECREATE_MATERIALIZED_VIEW_ON_EVALUATION is False

    def test_delete_where_true_optimization(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """
        Test DELETE with WHERE TRUE optimization.

        WHERE TRUE is converted to TRUNCATE TABLE for better performance.
        This works for all StarRocks table types and is semantically equivalent.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        # Test WHERE TRUE
        adapter.delete_from(exp.to_table("test_table"), exp.true())
        assert to_sql_calls(adapter) == [
            "TRUNCATE TABLE `test_table`",
        ]

        adapter.cursor.reset_mock()

        # Test no WHERE clause (also uses TRUNCATE)
        adapter.delete_from(exp.to_table("test_table"), None)
        assert to_sql_calls(adapter) == [
            "TRUNCATE TABLE `test_table`",
        ]


# =============================================================================
# WHERE Clause Transformations
# =============================================================================
class TestWhereClauseTransformations:
    """
    Tests for WHERE clause transformations in DELETE statements.

    StarRocks has limitations on DELETE WHERE clauses for non-PRIMARY KEY tables:
    - BETWEEN is not supported → converted to >= AND <=
    - Boolean literals (TRUE/FALSE) are not supported → removed or converted to 1=1/1=0

    These transformations are applied conservatively to all DELETE statements since
    table type cannot be easily determined at DELETE time.
    """

    def test_delete_with_between_simple(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """
        Test BETWEEN is converted to >= AND <= in DELETE WHERE.

        StarRocks Limitation:
        BETWEEN is not supported in DELETE WHERE for DUPLICATE/UNIQUE/AGGREGATE KEY tables.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("dt BETWEEN '2024-01-01' AND '2024-12-31'"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "BETWEEN" not in sql
        assert "`dt` >= '2024-01-01'" in sql
        assert "`dt` <= '2024-12-31'" in sql
        assert "AND" in sql

    def test_delete_with_between_numeric(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test BETWEEN with numeric values."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("id BETWEEN 100 AND 200"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "BETWEEN" not in sql
        assert "`id` >= 100" in sql
        assert "`id` <= 200" in sql

    def test_delete_with_between_and_other_conditions(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test BETWEEN combined with other WHERE conditions."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        # Complex WHERE: id > 50 AND dt BETWEEN '2024-01-01' AND '2024-12-31'
        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("id > 50 AND dt BETWEEN '2024-01-01' AND '2024-12-31'"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "BETWEEN" not in sql
        assert "`id` > 50" in sql
        assert "`dt` >= '2024-01-01'" in sql
        assert "`dt` <= '2024-12-31'" in sql

    def test_delete_with_multiple_between(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test multiple BETWEEN expressions in one WHERE clause."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("dt BETWEEN '2024-01-01' AND '2024-12-31' AND id BETWEEN 1 AND 100"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "BETWEEN" not in sql
        assert "`dt` >= '2024-01-01'" in sql
        assert "`dt` <= '2024-12-31'" in sql
        assert "`id` >= 1" in sql
        assert "`id` <= 100" in sql

    def test_delete_with_and_true(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """
        Test AND TRUE is removed from WHERE clause.

        StarRocks Limitation:
        Boolean literals are not supported in WHERE clauses.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("id > 100 AND TRUE"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "TRUE" not in sql
        assert "`id` > 100" in sql
        # Should not have extra AND
        assert sql.count("AND") == 0

    def test_delete_with_true_and_condition(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test TRUE AND condition (reverse order)."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("TRUE AND id > 100"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "TRUE" not in sql
        assert "`id` > 100" in sql

    def test_delete_with_or_false(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test OR FALSE is removed from WHERE clause."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("id > 100 OR FALSE"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "FALSE" not in sql
        assert "`id` > 100" in sql
        assert sql.count("OR") == 0

    def test_delete_with_false_or_condition(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test FALSE OR condition (reverse order)."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("FALSE OR id > 100"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "FALSE" not in sql
        assert "`id` > 100" in sql

    def test_delete_with_standalone_false(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test standalone FALSE is converted to 1=0."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            exp.false(),
        )

        sql = to_sql_calls(adapter)[0]
        assert "FALSE" not in sql
        # Converted to 1=0 (always false condition)
        assert "1 = 0" in sql or "1=0" in sql

    def test_delete_with_combined_transformations(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """
        Test BETWEEN + boolean literals together.

        Verifies that multiple transformations work correctly when combined.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        # WHERE: dt BETWEEN '2024-01-01' AND '2024-12-31' AND TRUE
        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("dt BETWEEN '2024-01-01' AND '2024-12-31' AND TRUE"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "BETWEEN" not in sql
        assert "TRUE" not in sql
        assert "`dt` >= '2024-01-01'" in sql
        assert "`dt` <= '2024-12-31'" in sql

    def test_delete_with_nested_boolean_expressions(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test nested boolean expressions with multiple levels."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        # WHERE: (id > 100 AND TRUE) OR (name = 'test' AND FALSE)
        # After transformation: id > 100 OR (name = 'test' AND FALSE)
        # After transformation: id > 100 OR FALSE
        # After transformation: id > 100
        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one("(id > 100 AND TRUE) OR (name = 'test' AND FALSE)"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "TRUE" not in sql
        # Note: The AND FALSE cannot be fully simplified without more complex logic
        # Our transformation only handles direct AND TRUE / OR FALSE at the binary level

    def test_delete_with_between_in_complex_expression(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test BETWEEN within a complex nested expression."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.delete_from(
            exp.to_table("test_table"),
            parse_one(
                "(dt BETWEEN '2024-01-01' AND '2024-06-30') OR (dt BETWEEN '2024-07-01' AND '2024-12-31')"
            ),
        )

        sql = to_sql_calls(adapter)[0]
        assert "BETWEEN" not in sql
        # First BETWEEN converted
        assert "`dt` >= '2024-01-01'" in sql
        assert "`dt` <= '2024-06-30'" in sql
        # Second BETWEEN converted
        assert "`dt` >= '2024-07-01'" in sql
        assert "`dt` <= '2024-12-31'" in sql
        assert "OR" in sql


# =============================================================================
# Key Property Building
# =============================================================================
class TestKeyPropertyBuilding:
    """
    Tests for table key types: primary_key, duplicate_key, unique_key, aggregate_key.

    Key columns must be the first N columns in the table definition.
    Tests parse actual Model SQL to ensure real-world compatibility.
    """

    @pytest.mark.parametrize(
        "key_type,key_value,expected_clause",
        [
            # primary_key - single column
            ("primary_key", "id", "PRIMARY KEY (`id`)"),
            # primary_key - tuple form (multi-column)
            ("primary_key", "(id, dt)", "PRIMARY KEY (`id`, `dt`)"),
            # duplicate_key - tuple form
            ("duplicate_key", "(id, name)", "DUPLICATE KEY (`id`, `name`)"),
            # unique_key - tuple form
            ("unique_key", "(id, dt)", "UNIQUE KEY (`id`, `dt`)"),
            # aggregate_key - multi-column. not supported (requires aggregation function specification)
            # ("aggregate_key", ("id", "dt"), "AGGREGATE KEY (`id`, `dt`)"),
        ],
    )
    def test_key_types_with_tuple_form(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        key_type: str,
        key_value: str,
        expected_clause: str,
    ):
        """Test key types with tuple form: (id, dt) parsed from physical_properties."""
        model_sql = f"""
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, dt DATE, name STRING, value DECIMAL(10,2)),
            physical_properties (
                {key_type} = {key_value}
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_clause in sql

    @pytest.mark.parametrize(
        "key_string,expected_clause",
        [
            # String with parentheses
            ('"(id, dt)"', "PRIMARY KEY (`id`, `dt`)"),
            # String without parentheses (auto-wrapped)
            ('"id, dt"', "PRIMARY KEY (`id`, `dt`)"),
            # Single column string
            ('"id"', "PRIMARY KEY (`id`)"),
        ],
    )
    def test_primary_key_string_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        key_string: str,
        expected_clause: str,
    ):
        """Test primary_key with string forms (with/without parentheses) parsed from physical_properties."""
        model_sql = f"""
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, dt DATE, value DECIMAL(10,2)),
            physical_properties (
                primary_key = {key_string}
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_clause in sql

    def test_primary_key_single_identifier(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test primary_key = id (single identifier without quotes)."""
        model_sql = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, dt DATE),
            physical_properties (
                primary_key = id
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert "PRIMARY KEY (`id`)" in sql

    def test_primary_key_via_table_properties_tuple(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test primary_key passed via physical_properties with tuple form - duplicate of test_key_types_with_tuple_form."""
        model_sql = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, dt DATE, value DECIMAL(10,2)),
            physical_properties (
                primary_key = (id, dt)
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert "PRIMARY KEY (`id`, `dt`)" in sql

    def test_column_reordering_for_key(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test column reordering for key tables.

        StarRocks Requirement:
        Key columns MUST be the first N columns in CREATE TABLE statement.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        columns_to_types = {
            "customer_id": exp.DataType.build("INT"),
            "region": exp.DataType.build("VARCHAR(50)"),
            "order_id": exp.DataType.build("BIGINT"),
            "event_date": exp.DataType.build("DATE"),
            "amount": exp.DataType.build("DECIMAL(18,2)"),
        }

        adapter.create_table(
            "test_table",
            target_columns_to_types=columns_to_types,
            primary_key=("order_id", "event_date"),
        )

        sql = to_sql_calls(adapter)[0]
        assert "PRIMARY KEY (`order_id`, `event_date`)" in sql

        import re

        col_match = re.search(r"CREATE TABLE.*?\((.*)\)\s*PRIMARY KEY", sql, re.DOTALL)
        assert col_match, "Could not extract column definitions"
        col_defs = col_match.group(1)

        order_id_pos = col_defs.find("`order_id`")
        event_date_pos = col_defs.find("`event_date`")
        customer_id_pos = col_defs.find("`customer_id`")

        assert order_id_pos < event_date_pos, "order_id must appear before event_date"
        assert event_date_pos < customer_id_pos, "event_date must appear before customer_id"


# =============================================================================
# Partition Property Building
# =============================================================================
class TestPartitionPropertyBuilding:
    """Tests for partitioned_by/partition_by and partitions properties."""

    @pytest.mark.parametrize(
        "partition_expr,expected_clause,expected_clause2",
        [
            # Expression partitioning - single column
            ("'dt'", "PARTITION BY `dt`", "PARTITION BY (`dt`)"),
            # Expression partitioning - multi-column
            ("(year, month)", "PARTITION BY `year`, `month`", "PARTITION BY (`year`, `month`)"),
            # Expression partitioning - multi-column with func
            (
                "(date_trunc('day', dt), region)",
                "PARTITION BY DATE_TRUNC('DAY', `dt`), `region`",
                None,
            ),
            # RANGE partitioning
            ("RANGE (dt)", "PARTITION BY RANGE (`dt`) ()", None),
            # LIST partitioning
            ("LIST (region)", "PARTITION BY LIST (`region`) ()", None),
        ],
    )
    def test_partitioned_by_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        partition_expr: str,
        expected_clause: str,
        expected_clause2: t.Optional[str],
    ):
        """Test partition_by with various forms parsed from physical_properties."""
        model_sql = f"""
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, dt DATE, year INT, month INT, region STRING),
            physical_properties (
                partition_by = {partition_expr}
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            partitioned_by=model.partitioned_by,
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_clause in sql or expected_clause2 and expected_clause2 in sql

    @pytest.mark.parametrize(
        "partition_expr,expected_clause",
        [
            ("(year, month)", "PARTITION BY (`year`, `month`)"),
            (
                "(date_trunc('day', dt), region)",
                "PARTITION BY (DATE_TRUNC('DAY', `dt`), `region`)",
            ),
            (
                "(from_unixtime(dt))",
                "PARTITION BY (FROM_UNIXTIME(`dt`))",
            ),
        ],
    )
    def test_partitioned_by_forms_for_mv(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        partition_expr: str,
        expected_clause: str,
    ):
        """MV partition_by should keep outer parentheses when rendering partition tuples."""
        model_sql = f"""
        MODEL (
            name test_schema.test_mv_partition,
            kind VIEW (
                materialized true,
            ),
            dialect starrocks,
            columns (dt DATE, region STRING, year INT, month INT),
            physical_properties (
                partition_by = {partition_expr},
                refresh_scheme = ASYNC
            )
        );
        SELECT dt, region, year, month FROM src;
        """

        model = _load_sql_model(model_sql)
        materialized_properties = (
            {"partitioned_by": model.partitioned_by} if model.partitioned_by else None
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view(
            model.name,
            model.render_query(),
            materialized=True,
            replace=False,
            target_columns_to_types=_columns(model),
            materialized_properties=materialized_properties,
            view_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_clause in sql

    def test_partition_by_alias(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test partition_by as alias for partitioned_by in physical_properties."""
        model_sql = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, year INT, month INT),
            physical_properties (
                partition_by = (year, month)
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            partitioned_by=model.partitioned_by,
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert "PARTITION BY (`year`, `month`)" in sql or "PARTITION BY `year`, `month`" in sql

    def test_partitioned_by_as_model_parameter(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test partitioned_by as model-level parameter (not in physical_properties)."""
        model_sql = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, year INT, month INT, value DECIMAL(10,2)),
            partitioned_by (year, month)
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            partitioned_by=model.partitioned_by,
        )

        sql = to_sql_calls(adapter)[0]
        assert (
            "PARTITION BY (year, month)" in sql
            or "PARTITION BY `year`, `month`" in sql
            or "PARTITION BY (`year`, `month`)" in sql
        )

    def test_partitions_value_forms(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test partitions property with single and multiple partition definitions."""
        # Single partition string (paren)
        model_sql_single = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, dt DATE),
            physical_properties (
                partition_by = RANGE(dt),
                partitions = 'PARTITION p1 VALUES LESS THAN ("2024-01-01")'
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql_single, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            partitioned_by=model.partitioned_by,
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert "PARTITION p1" in sql
        assert "VALUES LESS THAN" in sql

        # Multiple partitions (tuple of strings)
        model_sql_multiple = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, dt DATE),
            physical_properties (
                partition_by = RANGE(dt),
                partitions = (
                    'PARTITION p1 VALUES LESS THAN ("2024-01-01")',
                    'PARTITION p2 VALUES LESS THAN ("2024-02-01")'
                )
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql_multiple, default_dialect="starrocks")
        model = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            partitioned_by=model.partitioned_by,
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert "PARTITION p1" in sql
        assert "PARTITION p2" in sql


# =============================================================================
# Distribution Property Building
# =============================================================================
class TestDistributionPropertyBuilding:
    """Tests for distributed_by property."""

    @pytest.mark.parametrize(
        "dist_input,expected_clause",
        [
            # String form: HASH single column
            ('"HASH(id) BUCKETS 10"', "DISTRIBUTED BY HASH (`id`) BUCKETS 10"),
            # String form: HASH multi-column
            (
                '"HASH(id, region) BUCKETS 16"',
                "DISTRIBUTED BY HASH (`id`, `region`) BUCKETS 16",
            ),
            # String form: RANDOM
            ('"RANDOM"', "DISTRIBUTED BY RANDOM"),
            # String form: RANDOM with BUCKETS
            ('"RANDOM BUCKETS 10"', "DISTRIBUTED BY RANDOM BUCKETS 10"),
        ],
    )
    def test_distributed_by_string_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        dist_input: str,
        expected_clause: str,
    ):
        """Test distributed_by with string forms parsed from physical_properties."""
        model_sql = f"""
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, region STRING),
            physical_properties (
                distributed_by = {dist_input}
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_clause in sql

    @pytest.mark.parametrize(
        "dist_struct,expected_clause",
        [
            # Structured: HASH with quoted kind
            ("(kind='HASH', expressions=id, buckets=32)", "DISTRIBUTED BY HASH (`id`) BUCKETS 32"),
            # Structured: HASH with unquoted kind (Column)
            ("(kind=HASH, expressions=id, buckets=10)", "DISTRIBUTED BY HASH (`id`) BUCKETS 10"),
            # Structured: HASH multi-column
            (
                "(kind='HASH', expressions=(a, b), buckets=16)",
                "DISTRIBUTED BY HASH (`a`, `b`) BUCKETS 16",
            ),
            # Structured: RANDOM
            ("(kind='RANDOM')", "DISTRIBUTED BY RANDOM"),
            # Structured: RANDOM with buckets
            ("(kind=RANDOM, buckets=10)", "DISTRIBUTED BY RANDOM BUCKETS 10"),
        ],
    )
    def test_distributed_by_structured_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        dist_struct: str,
        expected_clause: str,
    ):
        """Test distributed_by with structured tuple forms parsed from physical_properties."""
        model_sql = f"""
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, a INT, b STRING, region STRING),
            physical_properties (
                distributed_by = {dist_struct}
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_clause in sql


# =============================================================================
# Order By Property Building
# =============================================================================
class TestOrderByPropertyBuilding:
    """Tests for order_by and clustered_by properties."""

    @pytest.mark.parametrize(
        "order_value,expected_clause,description",
        [
            # String form (double-quoted string)
            ('"id"', "ORDER BY (`id`)", "Bare string: single column"),
            (
                '"id, timestamp"',
                "ORDER BY (`id`, `timestamp`)",
                "Bare string: multi-column without parens",
            ),
            ('"(id, timestamp)"', "ORDER BY (`id`, `timestamp`)", "String with parens"),
            # Literal form (single-quoted string)
            ("'id'", "ORDER BY (`id`)", "Bare string: single column"),
            (
                "'id, timestamp'",
                "ORDER BY (`id`, `timestamp`)",
                "Bare string: multi-column without parens",
            ),
            ("'(id, timestamp)'", "ORDER BY (`id`, `timestamp`)", "String with parens"),
            # Tuple form (direct expression construction in MODEL)
            ("(id, timestamp)", "ORDER BY (`id`, `timestamp`)", "Tuple: multi-column"),
            # Single identifier (unquoted)
            ("id", "ORDER BY (`id`)", "Identifier: single column"),
        ],
    )
    def test_order_by_value_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        order_value: str,
        expected_clause: str,
        description: str,
    ):
        """Test ORDER BY with various input forms parsed from physical_properties."""
        model_sql = f"""
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, timestamp DATETIME, value DECIMAL(10,2)),
            physical_properties (
                order_by = {order_value}
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_clause in sql, (
            f"\nTest case: {description}\n"
            f"Input: {order_value}\n"
            f"Expected: {expected_clause}\n"
            f"Actual SQL: {sql}"
        )

    def test_clustered_by_generates_order_by(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test that clustered_by parameter generates ORDER BY clause."""
        model_sql = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, timestamp DATETIME, value DECIMAL(10,2)),
            physical_properties (
                clustered_by = (id, timestamp)
            )
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            table_properties=model.physical_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert "ORDER BY (`id`, `timestamp`)" in sql
        assert "CLUSTER BY" not in sql

    def test_clustered_by_as_model_parameter(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test clustered_by as model-level parameter (not in physical_properties)."""
        model_sql = """
        MODEL (
            name t,
            kind FULL,
            dialect starrocks,
            columns (id INT, timestamp DATETIME, value DECIMAL(10,2)),
            clustered_by id
        );
        SELECT 1;
        """

        parsed = parse(model_sql, default_dialect="starrocks")
        model: SqlModel = t.cast(SqlModel, load_sql_based_model(parsed))

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            model.name,
            _columns(model),
            clustered_by=model.clustered_by,
        )

        sql = to_sql_calls(adapter)[0]
        assert "ORDER BY (`id`)" in sql
        # Verify that StarRocks uses ORDER BY, not CLUSTER BY
        assert "CLUSTER BY" not in sql


# =============================================================================
# Generic Property Building
# =============================================================================
class TestGenericPropertyBuilding:
    """Tests for generic table properties (replication_num, etc.)."""

    @pytest.mark.parametrize(
        "prop_name,prop_value,expected_in_sql",
        [
            # Integer value
            ("replication_num", "1", "'replication_num'='1'"),
            ("replication_num", "3", "'replication_num'='3'"),
            # Boolean TRUE
            ("enable_persistent_index", "TRUE", "'enable_persistent_index'='TRUE'"),
            # Boolean FALSE
            ("in_memory", "FALSE", "'in_memory'='FALSE'"),
            # String value
            ("compression", "LZ4", "'compression'='LZ4'"),
        ],
    )
    def test_generic_property_value_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        prop_name: str,
        prop_value: str,
        expected_in_sql: str,
    ):
        """Test generic properties with various value types."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        adapter.create_table(
            "test_table",
            target_columns_to_types={
                "id": exp.DataType.build("INT"),
                "name": exp.DataType.build("VARCHAR(100)"),
            },
            primary_key=("id",),
            table_properties={
                prop_name: prop_value,
            },
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_in_sql in sql


# =============================================================================
# View Property Building
# =============================================================================
class TestViewPropertyBuilding:
    """Tests for StarRocks-specific view properties (SECURITY)."""

    @pytest.mark.parametrize(
        "property_sql,expected_fragment",
        [
            ("INVOKER", "SECURITY INVOKER"),
            ("'INVOKER'", "SECURITY INVOKER"),
            ("invoker", "SECURITY INVOKER"),
            ("NONE", "SECURITY NONE"),
        ],
    )
    def test_security_value_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        property_sql: str,
        expected_fragment: str,
    ):
        """Ensure different input forms render SECURITY <value>."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        model_sql = f"""
        MODEL (
            name test_schema.test_view_security,
            kind VIEW,
            dialect starrocks,
            columns (c INT),
            virtual_properties (
                security = {property_sql}
            )
        );
        SELECT 1 AS c;
        """
        model = _load_sql_model(model_sql)

        query = model.render_query()
        adapter.create_view(
            model.name,
            query,
            replace=False,
            target_columns_to_types=_columns(model),
            view_properties=model.virtual_properties,
        )

        sql = to_sql_calls(adapter)[0]
        assert expected_fragment in sql

    def test_security_invalid_value(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Invalid SECURITY enum should raise SQLMeshError."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model_sql = """
        MODEL (
            name test_schema.test_view_security_invalid,
            kind VIEW,
            dialect starrocks,
            columns (c INT),
            virtual_properties (
                security = foo
            )
        );
        SELECT 1 AS c;
        """
        model = _load_sql_model(model_sql)

        query = model.render_query()
        with pytest.raises(SQLMeshError, match="security"):
            adapter.create_view(
                model.name,
                query,
                replace=False,
                target_columns_to_types=_columns(model),
                view_properties=model.virtual_properties,
            )


# =============================================================================
# Materialized View Refresh Property Building
# =============================================================================
class TestMVRefreshPropertyBuilding:
    """Tests for refresh_moment / refresh_scheme parsing and rendering."""

    def _build_mv_model(self, property_sql: str) -> SqlModel:
        model_sql = f"""
        MODEL (
            name test_schema.test_mv_refresh_model,
            kind VIEW (materialized true),
            dialect starrocks,
            columns (a INT),
            physical_properties (
                {property_sql}
            )
        );
        SELECT 1 AS a;
        """
        return _load_sql_model(model_sql)

    def _create_simple_mv(
        self,
        adapter: StarRocksEngineAdapter,
        model: SqlModel,
    ) -> str:
        query = model.render_query()
        adapter.create_view(
            "test_mv_refresh",
            query,
            replace=False,
            materialized=True,
            target_columns_to_types=_columns(model),
            view_properties=model.physical_properties,
        )
        # replace=False → only CREATE statement is emitted
        return to_sql_calls(adapter)[-1]

    @pytest.mark.parametrize(
        "property_sql,expected_fragment",
        [
            ("refresh_moment = IMMEDIATE", "REFRESH IMMEDIATE"),
            ("refresh_moment = deferred", "REFRESH DEFERRED"),
        ],
    )
    def test_refresh_moment_value_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        property_sql: str,
        expected_fragment: str,
    ):
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = self._build_mv_model(property_sql)
        sql = self._create_simple_mv(adapter, model)
        assert expected_fragment in sql

    @pytest.mark.parametrize(
        "property_sql,expected_fragments",
        [
            ("refresh_scheme = ASYNC", ["REFRESH", "ASYNC"]),
            # single quote value with single quote start
            (
                "refresh_scheme = 'ASYNC START (''2025-01-01 00:00:00'') EVERY (INTERVAL 5 MINUTE)'",
                [
                    "REFRESH",
                    "ASYNC",
                    "START ('2025-01-01 00:00:00')",
                    "EVERY (INTERVAL 5 MINUTE)",
                ],
            ),
            # single quote value with double quote start
            (
                "refresh_scheme = 'ASYNC START (\"2025-02-01 00:00:00\") EVERY (INTERVAL 5 MINUTE)'",
                [
                    "REFRESH",
                    "ASYNC",
                    "START ('2025-02-01 00:00:00')",
                    "EVERY (INTERVAL 5 MINUTE)",
                ],
            ),
            # double quote value with single quote start
            (
                "refresh_scheme = \"async start ('2025-03-01') every (interval 10 minute)\"",
                [
                    "REFRESH",
                    "ASYNC",
                    "START ('2025-03-01')",
                    "EVERY (INTERVAL 10 MINUTE)",
                ],
            ),
            ("refresh_scheme = MANUAL", ["REFRESH", "MANUAL"]),
        ],
    )
    def test_refresh_scheme_value_forms(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        property_sql: str,
        expected_fragments: t.List[str],
    ):
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = self._build_mv_model(property_sql)
        sql = self._create_simple_mv(adapter, model)
        for fragment in expected_fragments:
            assert fragment in sql

    def test_refresh_moment_invalid_value(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = self._build_mv_model("refresh_moment = AUTO")
        with pytest.raises(SQLMeshError):
            self._create_simple_mv(adapter, model)

    def test_refresh_scheme_invalid_prefix(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = self._build_mv_model("refresh_scheme = 'SCHEDULE EVERY (INTERVAL 5 MINUTE)'")
        with pytest.raises(SQLMeshError, match="refresh_scheme"):
            self._create_simple_mv(adapter, model)


# =============================================================================
# Comment Property Building
# =============================================================================
class TestCommentPropertyBuilding:
    """Tests for table and column comments."""

    def test_table_and_column_comments(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE TABLE with table and column comments."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            "test_table",
            target_columns_to_types={
                "a": exp.DataType.build("INT"),
                "b": exp.DataType.build("VARCHAR(100)"),
            },
            table_description="Test table description",
            column_descriptions={
                "a": "Column A description",
                "b": "Column B description",
            },
        )

        sql = to_sql_calls(adapter)[0]
        assert "COMMENT 'Test table description'" in sql
        assert "COMMENT 'Column A description'" in sql
        assert "COMMENT 'Column B description'" in sql

    def test_view_with_comments(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """Test CREATE VIEW with comments."""
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_view(
            "test_view",
            parse_one("SELECT a FROM tbl"),
            replace=False,
            target_columns_to_types={"a": exp.DataType.build("INT")},
            table_description="Test view description",
            column_descriptions={"a": "Column A description"},
        )

        sql = to_sql_calls(adapter)[0]
        assert "COMMENT 'Test view description'" in sql
        assert "COMMENT 'Column A description'" in sql

    @pytest.mark.parametrize(
        "table_name,comment,expected_sql",
        [
            (
                "test_table",
                "Test table comment",
                "ALTER TABLE `test_table` COMMENT = 'Test table comment'",
            ),
            (
                "db.test_table",
                "Database qualified table comment",
                "ALTER TABLE `db`.`test_table` COMMENT = 'Database qualified table comment'",
            ),
            (
                "test_table",
                "It's a test",
                None,  # Will check for escaped quote
            ),
        ],
        ids=["simple_table", "qualified_table", "special_chars"],
    )
    def test_build_create_comment_table_exp(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        table_name: str,
        comment: str,
        expected_sql: t.Optional[str],
    ):
        """
        Test _build_create_comment_table_exp generates correct ALTER TABLE COMMENT SQL.

        Verifies:
        1. SQL format: ALTER TABLE {table} COMMENT = '{comment}'
        2. No MODIFY keyword (StarRocks uses direct COMMENT =)
        3. Comment is properly quoted
        4. Table name is properly quoted
        5. Special characters are escaped
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        table = exp.to_table(table_name)
        sql = adapter._build_create_comment_table_exp(table, comment, "TABLE")

        if expected_sql:
            assert sql == expected_sql
        else:
            # Special chars case - check for escaped quote
            assert "It's a test" in sql or "It''s a test" in sql

        # Common assertions for all cases
        assert "ALTER TABLE" in sql
        assert "COMMENT =" in sql
        assert "MODIFY" not in sql  # StarRocks doesn't use MODIFY for table comments

    def test_build_create_comment_table_exp_truncation(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """
        Test _build_create_comment_table_exp truncates long comments.

        Verifies comments longer than MAX_TABLE_COMMENT_LENGTH (2048) are truncated.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        table = exp.to_table("test_table")
        long_comment = "x" * 3000  # Longer than MAX_TABLE_COMMENT_LENGTH (2048)
        sql = adapter._build_create_comment_table_exp(table, long_comment, "TABLE")

        # The comment should be truncated to 2048 characters
        expected_truncated = "x" * 2048
        assert expected_truncated in sql
        assert "xxx" * 1000 not in sql  # Verify it's actually truncated

    @pytest.mark.parametrize(
        "table_name,column_name,comment,expected_sql",
        [
            (
                "test_table",
                "test_column",
                "Test column comment",
                "ALTER TABLE `test_table` MODIFY COLUMN `test_column` COMMENT 'Test column comment'",
            ),
            (
                "db.test_table",
                "id",
                "ID column",
                "ALTER TABLE `db`.`test_table` MODIFY COLUMN `id` COMMENT 'ID column'",
            ),
        ],
        ids=["simple_table", "qualified_table"],
    )
    def test_build_create_comment_column_exp(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
        table_name: str,
        column_name: str,
        comment: str,
        expected_sql: str,
    ):
        """
        Test _build_create_comment_column_exp generates correct ALTER TABLE MODIFY COLUMN SQL.

        Verifies:
        1. SQL format: ALTER TABLE {table} MODIFY COLUMN {column} COMMENT '{comment}'
        2. No column type required (StarRocks supports this)
        3. Comment is properly quoted
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        table = exp.to_table(table_name)
        sql = adapter._build_create_comment_column_exp(table, column_name, comment, "TABLE")

        assert sql == expected_sql
        # Should NOT contain column type
        assert "VARCHAR" not in sql
        assert "INT" not in sql
        assert "BIGINT" not in sql

    def test_build_create_comment_column_exp_truncation(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """
        Test _build_create_comment_column_exp truncates long comments.

        Verifies comments longer than MAX_COLUMN_COMMENT_LENGTH (255) are truncated.
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)

        table = exp.to_table("test_table")
        long_comment = "y" * 500  # Longer than MAX_COLUMN_COMMENT_LENGTH (255)
        sql = adapter._build_create_comment_column_exp(table, "test_col", long_comment, "TABLE")

        # The comment should be truncated to 255 characters
        expected_truncated = "y" * 255
        assert expected_truncated in sql
        assert "yyy" * 200 not in sql  # Verify it's actually truncated


# =============================================================================
# Invalid Property Scenarios
# =============================================================================
class TestInvalidPropertyScenarios:
    """Unit tests for property validation errors (mutual exclusivity, aliases, names)."""

    def test_key_type_mutually_exclusive(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model_sql = """
        MODEL (
            name test_schema.test_conflicting_keys,
            kind FULL,
            dialect starrocks,
            columns (
                id INT,
                dt DATE,
                value INT
            ),
            physical_properties (
                primary_key = (id),
                unique_key = (id)
            )
        );
        SELECT id, dt, value FROM source_table;
        """
        model = _load_sql_model(model_sql)
        columns = _columns(model)

        with pytest.raises(SQLMeshError, match="Multiple table key type"):
            adapter.create_table(
                "test_conflicting_keys",
                target_columns_to_types=columns,
                table_properties=model.physical_properties,
            )

    def test_partition_alias_conflict_with_parameter(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model_sql = """
        MODEL (
            name test_schema.test_partition_conflict,
            kind FULL,
            dialect starrocks,
            columns (
                id INT,
                dt DATE,
                value INT
            ),
            partitioned_by (dt),
            physical_properties (
                partition_by = (dt)
            )
        );
        SELECT id, dt, value FROM source_table;
        """
        model = _load_sql_model(model_sql)

        with pytest.raises(SQLMeshError, match="partition definition"):
            adapter.create_table(
                model.name,
                target_columns_to_types=_columns(model),
                partitioned_by=model.partitioned_by,
                table_properties=model.physical_properties,
            )

    def test_invalid_property_name_detection(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model_sql = """
        MODEL (
            name test_schema.test_invalid_property,
            kind FULL,
            dialect starrocks,
            columns (
                id INT,
                dt DATE,
                value INT
            ),
            physical_properties (
                partition = dt
            )
        );
        SELECT id, dt, value FROM source_table;
        """
        model = _load_sql_model(model_sql)

        with pytest.raises(SQLMeshError, match="Invalid property 'partition'"):
            adapter.create_table(
                model.name,
                target_columns_to_types=_columns(model),
                table_properties=model.physical_properties,
            )


# =============================================================================
# Comprehensive Tests
# =============================================================================
class TestComprehensive:
    """Comprehensive tests combining multiple features."""

    def test_create_table_comprehensive(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ):
        """
        Test CREATE TABLE with all features combined:
        - PRIMARY KEY
        - Table and column comments
        - DISTRIBUTED BY
        - ORDER BY
        - Custom properties
        """
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        adapter.create_table(
            "test_table",
            target_columns_to_types={
                "customer_id": exp.DataType.build("INT"),
                "order_id": exp.DataType.build("BIGINT"),
                "event_date": exp.DataType.build("DATE"),
                "amount": exp.DataType.build("DECIMAL(10,2)"),
            },
            primary_key=("order_id", "event_date"),
            table_description="Sales transaction table",
            column_descriptions={
                "customer_id": "Customer identifier",
                "order_id": "Order identifier",
            },
            table_properties={
                "distributed_by": exp.Tuple(
                    expressions=[
                        exp.EQ(
                            this=exp.Column(this="kind"),
                            expression=exp.Literal.string("HASH"),
                        ),
                        exp.EQ(
                            this=exp.Column(this="expressions"),
                            expression=exp.Tuple(expressions=[exp.to_column("customer_id")]),
                        ),
                        exp.EQ(
                            this=exp.Column(this="buckets"),
                            expression=exp.Literal.number(10),
                        ),
                    ]
                ),
                "replication_num": "3",
            },
            clustered_by=[exp.to_column("customer_id"), exp.to_column("order_id")],
        )

        sql = to_sql_calls(adapter)[0]
        assert "CREATE TABLE IF NOT EXISTS `test_table`" in sql
        assert "PRIMARY KEY (`order_id`, `event_date`)" in sql
        assert "COMMENT 'Sales transaction table'" in sql
        assert "COMMENT 'Customer identifier'" in sql
        assert "COMMENT 'Order identifier'" in sql
        assert "DISTRIBUTED BY HASH (`customer_id`) BUCKETS 10" in sql
        assert "ORDER BY (`customer_id`, `order_id`)" in sql
        assert "PROPERTIES ('replication_num'='3')" in sql


# =============================================================================
# Incremental models require a PRIMARY KEY table on StarRocks
# =============================================================================
class TestIncrementalRequiresPrimaryKey:
    """StarRocks incremental kinds rely on DELETE/MERGE, which only work on PRIMARY KEY tables.

    Declaring such a model without a PRIMARY KEY must therefore fail at creation time, while
    append-only kinds and non-StarRocks engines are unaffected. The rule is enforced by
    ``StarRocksEngineAdapter.adjust_physical_properties_for_incremental`` (reached from the
    evaluator's ``_adjust_physical_properties_for_engine``); these tests drive that path with
    declared models and assert the observable outcome.
    """

    def _adjust(self, adapter: EngineAdapter, model: SqlModel) -> t.Dict[str, t.Any]:
        return _adjust_physical_properties_for_engine(adapter, model, model.physical_properties)

    def test_incremental_model_without_primary_key_raises(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ) -> None:
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = _load_sql_model(
            """
            MODEL (
                name test_schema.inc_no_pk,
                kind INCREMENTAL_BY_TIME_RANGE (time_column event_date),
                dialect starrocks,
                columns (id INT, event_date DATE)
            );
            SELECT id, event_date FROM src WHERE event_date BETWEEN @start_ds AND @end_ds;
            """
        )
        with pytest.raises(SQLMeshError, match="requires a PRIMARY KEY"):
            self._adjust(adapter, model)

    def test_incremental_model_with_primary_key_is_allowed(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ) -> None:
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = _load_sql_model(
            """
            MODEL (
                name test_schema.inc_pk,
                kind INCREMENTAL_BY_TIME_RANGE (time_column event_date),
                dialect starrocks,
                columns (id INT, event_date DATE),
                physical_properties (primary_key = (id, event_date))
            );
            SELECT id, event_date FROM src WHERE event_date BETWEEN @start_ds AND @end_ds;
            """
        )
        assert "primary_key" in self._adjust(adapter, model)

    def test_incremental_by_unique_key_is_promoted_to_primary_key(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ) -> None:
        # INCREMENTAL_BY_UNIQUE_KEY auto-promotes the unique_key to a PRIMARY KEY (a multi-column
        # key becomes a tuple) rather than requiring one to be declared explicitly.
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = _load_sql_model(
            """
            MODEL (
                name test_schema.inc_by_uk,
                kind INCREMENTAL_BY_UNIQUE_KEY (unique_key (id, event_date)),
                dialect starrocks,
                columns (id INT, event_date DATE)
            );
            SELECT id, event_date FROM src;
            """
        )
        primary_key = self._adjust(adapter, model)["primary_key"]
        assert isinstance(primary_key, exp.Tuple)
        assert [c.name for c in primary_key.expressions] == ["id", "event_date"]

    def test_append_only_incremental_does_not_require_primary_key(
        self, make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter]
    ) -> None:
        # Append-only INCREMENTAL_UNMANAGED (insert_overwrite=False) only does INSERT, so it does
        # not need a PRIMARY KEY table.
        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        model = _load_sql_model(
            """
            MODEL (
                name test_schema.inc_append,
                kind INCREMENTAL_UNMANAGED,
                dialect starrocks,
                columns (id INT, event_date DATE)
            );
            SELECT id, event_date FROM src;
            """
        )
        assert "primary_key" not in self._adjust(adapter, model)

    def test_non_starrocks_incremental_is_unaffected(
        self, make_mocked_engine_adapter: t.Callable[..., DuckDBEngineAdapter]
    ) -> None:
        # Engines without the PRIMARY KEY requirement inherit the base no-op and never raise.
        adapter = make_mocked_engine_adapter(DuckDBEngineAdapter)
        model = _load_sql_model(
            """
            MODEL (
                name test_schema.inc_duckdb,
                kind INCREMENTAL_BY_TIME_RANGE (time_column event_date),
                dialect duckdb,
                columns (id INT, event_date DATE)
            );
            SELECT id, event_date FROM src WHERE event_date BETWEEN @start_ds AND @end_ds;
            """
        )
        assert "primary_key" not in self._adjust(adapter, model)


# =============================================================================
# excluded_trigger_tables / excluded_refresh_tables physical table ref resolution
# =============================================================================
class TestExcludedTablesResolution:
    """Tests for automatic resolution of logical model names to physical table names
    in excluded_trigger_tables and excluded_refresh_tables physical_properties.

    StarRocks async materialized views accept these properties to skip certain tables
    from triggering or participating in refreshes. When a value is a managed SQLMesh
    model, StarRocks needs the physical name (db.table), not the logical view name.
    """

    @staticmethod
    def _make_snapshot(model: SqlModel) -> Snapshot:
        snapshot = Snapshot.from_node(model, nodes={}, ttl="in 1 week")
        snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
        return snapshot

    def _build_mv_with_excluded_tables(
        self,
        adapter: StarRocksEngineAdapter,
        model: SqlModel,
        snapshots: t.Dict[str, Snapshot],
    ) -> str:
        """Render physical properties with snapshot resolution and create the MV, returning DDL."""
        rendered_props = model.render_physical_properties(
            snapshots=snapshots,
            engine_adapter=adapter,
        )
        query = model.render_query()
        adapter.create_view(
            model.name,
            query,
            replace=False,
            materialized=True,
            target_columns_to_types={"a": exp.DataType.build("INT")},
            view_properties=rendered_props,
        )
        calls = to_sql_calls(adapter)
        return calls[-1]

    def test_single_managed_model_ref_is_resolved_to_physical_name(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
    ) -> None:
        """excluded_trigger_tables referencing a managed model is resolved to physical db.table."""
        base_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_1_model,
                kind FULL,
                dialect starrocks,
                columns (a INT)
            );
            SELECT 1 AS a;
            """
        )
        base_snapshot = self._make_snapshot(base_model)
        physical_name = exp.to_table(base_snapshot.table_name())
        expected_physical = f"{physical_name.db}.{physical_name.name}"

        mv_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_mv,
                kind VIEW (materialized true),
                dialect starrocks,
                columns (a INT),
                physical_properties (
                    refresh_scheme = ASYNC,
                    excluded_trigger_tables = starrocks.test_1_model
                )
            );
            SELECT a FROM starrocks.test_1_model;
            """
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        snapshots = {base_snapshot.name: base_snapshot}
        ddl = self._build_mv_with_excluded_tables(adapter, mv_model, snapshots)

        assert expected_physical in ddl
        # Logical name must NOT appear as the property value
        assert f"'excluded_trigger_tables'='starrocks.test_1_model'" not in ddl

    def test_single_managed_model_ref_in_excluded_refresh_tables(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
    ) -> None:
        """excluded_refresh_tables referencing a managed model is also resolved."""
        base_model = _load_sql_model(
            """
            MODEL (
                name starrocks.source_model,
                kind FULL,
                dialect starrocks,
                columns (a INT)
            );
            SELECT 1 AS a;
            """
        )
        base_snapshot = self._make_snapshot(base_model)
        physical_name = exp.to_table(base_snapshot.table_name())
        expected_physical = f"{physical_name.db}.{physical_name.name}"

        mv_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_mv2,
                kind VIEW (materialized true),
                dialect starrocks,
                columns (a INT),
                physical_properties (
                    refresh_scheme = ASYNC,
                    excluded_refresh_tables = starrocks.source_model
                )
            );
            SELECT a FROM starrocks.source_model;
            """
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        snapshots = {base_snapshot.name: base_snapshot}
        ddl = self._build_mv_with_excluded_tables(adapter, mv_model, snapshots)

        assert expected_physical in ddl

    def test_unmanaged_source_is_left_as_is(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
    ) -> None:
        """A raw source that is not a managed snapshot passes through unchanged."""
        mv_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_mv3,
                kind VIEW (materialized true),
                dialect starrocks,
                columns (a INT),
                physical_properties (
                    refresh_scheme = ASYNC,
                    excluded_trigger_tables = "external_db.raw_table"
                )
            );
            SELECT 1 AS a;
            """
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        ddl = self._build_mv_with_excluded_tables(adapter, mv_model, snapshots={})

        assert "external_db.raw_table" in ddl

    def test_unmanaged_external_catalog_ref_keeps_catalog(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
    ) -> None:
        """A three-part external-catalog reference is preserved in full (catalog not stripped)."""
        mv_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_mv_ext,
                kind VIEW (materialized true),
                dialect starrocks,
                columns (a INT),
                physical_properties (
                    refresh_scheme = ASYNC,
                    excluded_trigger_tables = "ext_catalog.ext_db.raw_table"
                )
            );
            SELECT 1 AS a;
            """
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        ddl = self._build_mv_with_excluded_tables(adapter, mv_model, snapshots={})

        assert "ext_catalog.ext_db.raw_table" in ddl

    def test_mixed_list_managed_and_unmanaged(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
    ) -> None:
        """A comma-separated list: managed model resolved, unmanaged source left as-is."""
        base_model = _load_sql_model(
            """
            MODEL (
                name starrocks.managed_model,
                kind FULL,
                dialect starrocks,
                columns (a INT)
            );
            SELECT 1 AS a;
            """
        )
        base_snapshot = self._make_snapshot(base_model)
        physical_name = exp.to_table(base_snapshot.table_name())
        expected_physical = f"{physical_name.db}.{physical_name.name}"

        mv_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_mv4,
                kind VIEW (materialized true),
                dialect starrocks,
                columns (a INT),
                physical_properties (
                    refresh_scheme = ASYNC,
                    excluded_trigger_tables = "starrocks.managed_model,external_db.raw_table"
                )
            );
            SELECT 1 AS a;
            """
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        snapshots = {base_snapshot.name: base_snapshot}
        ddl = self._build_mv_with_excluded_tables(adapter, mv_model, snapshots)

        assert expected_physical in ddl
        assert "external_db.raw_table" in ddl
        # The logical name must NOT appear as a property value; the physical name MUST be present.
        # StarRocks DDL format: PROPERTIES ('key'='value'), both key and value in single quotes.
        import re

        match = re.search(r"'excluded_trigger_tables'='([^']*)'", ddl)
        assert match is not None, "excluded_trigger_tables property not found in DDL"
        prop_value = match.group(1)
        assert "starrocks.managed_model" not in prop_value, (
            "Logical model name leaked into excluded_trigger_tables property value"
        )
        assert expected_physical in prop_value, (
            "Physical table name not found in excluded_trigger_tables property value"
        )

    def test_no_snapshots_passes_value_through(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
    ) -> None:
        """When no snapshots are provided, the value passes through unchanged."""
        mv_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_mv5,
                kind VIEW (materialized true),
                dialect starrocks,
                columns (a INT),
                physical_properties (
                    refresh_scheme = ASYNC,
                    excluded_trigger_tables = starrocks.some_table
                )
            );
            SELECT 1 AS a;
            """
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        ddl = self._build_mv_with_excluded_tables(adapter, mv_model, snapshots={})

        assert "starrocks.some_table" in ddl

    def test_dev_plan_non_deployable_snapshot_resolves_to_dev_table(
        self,
        make_mocked_engine_adapter: t.Callable[..., StarRocksEngineAdapter],
    ) -> None:
        """When a snapshot is non-deployable (dev plan), the dev physical table is used.

        A snapshot that is not representative in the deployability_index has its dev
        (``__dev``-suffixed) table selected by ``to_table_mapping``.  The property value
        must reference this dev physical name, NOT the production physical name.
        """
        base_model = _load_sql_model(
            """
            MODEL (
                name starrocks.upstream,
                kind FULL,
                dialect starrocks,
                columns (a INT)
            );
            SELECT 1 AS a;
            """
        )
        base_snapshot = self._make_snapshot(base_model)
        prod_physical_name = exp.to_table(base_snapshot.table_name(is_deployable=True))
        dev_physical_name = exp.to_table(base_snapshot.table_name(is_deployable=False))

        prod_expected = f"{prod_physical_name.db}.{prod_physical_name.name}"
        dev_expected = f"{dev_physical_name.db}.{dev_physical_name.name}"

        mv_model = _load_sql_model(
            """
            MODEL (
                name starrocks.test_mv_dev,
                kind VIEW (materialized true),
                dialect starrocks,
                columns (a INT),
                physical_properties (
                    refresh_scheme = ASYNC,
                    excluded_trigger_tables = starrocks.upstream
                )
            );
            SELECT a FROM starrocks.upstream;
            """
        )

        adapter = make_mocked_engine_adapter(StarRocksEngineAdapter)
        snapshots = {base_snapshot.name: base_snapshot}

        # With all_deployable (default/prod plan): resolves to prod physical name
        deployability_all = DeployabilityIndex.all_deployable()
        rendered_prod = mv_model.render_physical_properties(
            snapshots=snapshots,
            engine_adapter=adapter,
            deployability_index=deployability_all,
        )
        prod_value = rendered_prod["excluded_trigger_tables"]
        assert hasattr(prod_value, "this"), "expected exp.Literal"
        # prod value must equal the prod table exactly (no __dev suffix)
        assert prod_value.this == prod_expected
        assert "__dev" not in prod_value.this

        # With none_deployable (dev plan): resolves to dev (__dev-suffixed) physical name
        deployability_none = DeployabilityIndex.none_deployable()
        rendered_dev = mv_model.render_physical_properties(
            snapshots=snapshots,
            engine_adapter=adapter,
            deployability_index=deployability_none,
        )
        dev_value = rendered_dev["excluded_trigger_tables"]
        assert hasattr(dev_value, "this"), "expected exp.Literal"
        # dev value must equal the dev table exactly (has __dev suffix)
        assert dev_value.this == dev_expected
        assert "__dev" in dev_value.this

    def test_non_starrocks_engine_is_not_affected(self) -> None:
        """The RESOLVE_TABLE_REFS_IN_PHYSICAL_PROPERTIES set is empty on the base EngineAdapter."""
        from sqlmesh.core.engine_adapter.base import EngineAdapter

        assert len(EngineAdapter.RESOLVE_TABLE_REFS_IN_PHYSICAL_PROPERTIES) == 0
        assert (
            "excluded_trigger_tables"
            in StarRocksEngineAdapter.RESOLVE_TABLE_REFS_IN_PHYSICAL_PROPERTIES
        )
        assert (
            "excluded_refresh_tables"
            in StarRocksEngineAdapter.RESOLVE_TABLE_REFS_IN_PHYSICAL_PROPERTIES
        )
