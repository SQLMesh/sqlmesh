from __future__ import annotations

import os
import pathlib
import sys
import typing as t
import time
from contextlib import contextmanager

import pandas as pd  # noqa: TID253
import pytest
from sqlglot import exp, parse_one
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sqlmesh import Config, Context, EngineAdapter
from sqlmesh.core.config import load_config_from_paths
from sqlmesh.core.config.connection import AthenaConnectionConfig
from sqlmesh.core.dialect import normalize_model_name
import sqlmesh.core.dialect as d
from sqlmesh.core.engine_adapter import SparkEngineAdapter, TrinoEngineAdapter, AthenaEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObject
from sqlmesh.core.model.definition import SqlModel, load_sql_based_model
from sqlmesh.utils import random_id
from sqlmesh.utils.date import to_ds
from sqlmesh.utils.pydantic import PydanticModel
from tests.utils.pandas import compare_dataframes
from dataclasses import dataclass
from _pytest.mark import MarkDecorator
from _pytest.mark.structures import ParameterSet

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import TableName, SchemaName
    from sqlmesh.core.engine_adapter._typing import Query

TEST_SCHEMA = "test_schema"


@dataclass
class IntegrationTestEngine:
    engine: str
    catalog_types: t.Optional[t.List[str]] = None
    native_dataframe_type: t.Optional[str] = None
    cloud: bool = False

    @property
    def dialect(self) -> str:
        return self.engine.split("_", maxsplit=1)[0]

    @property
    def pytest_marks(self) -> t.List[MarkDecorator]:
        marks = [getattr(pytest.mark, self.engine), pytest.mark.engine]
        if self.cloud:
            marks.append(pytest.mark.remote)
        else:
            marks.append(pytest.mark.docker)
        if self.engine == "duckdb":
            marks.extend(
                [
                    # run the duckdb tests in `make cicd-test` as well
                    pytest.mark.slow,
                    # the duckdb tests cannot run concurrently because many of them point at the same files
                    # and duckdb does not support multi process read/write on the same files
                    # ref: https://duckdb.org/docs/connect/concurrency.html#writing-to-duckdb-from-multiple-processes
                    pytest.mark.xdist_group("engine_integration_duckdb"),
                ]
            )
        return marks


ENGINES = [
    # Docker engines that can be locally tested
    IntegrationTestEngine("duckdb"),
    IntegrationTestEngine("postgres"),
    IntegrationTestEngine("mysql"),
    IntegrationTestEngine("mssql"),
    IntegrationTestEngine("trino", catalog_types=["hive", "iceberg", "delta", "nessie"]),
    IntegrationTestEngine("spark", native_dataframe_type="pyspark"),
    IntegrationTestEngine("clickhouse", catalog_types=["standalone", "cluster"]),
    IntegrationTestEngine("risingwave"),
    # Cloud engines that need paid accounts / special credentials
    IntegrationTestEngine("clickhouse_cloud", cloud=True),
    IntegrationTestEngine("redshift", cloud=True),
    IntegrationTestEngine("athena", catalog_types=["hive", "iceberg"], cloud=True),
    IntegrationTestEngine("bigquery", native_dataframe_type="bigframe", cloud=True),
    IntegrationTestEngine("databricks", native_dataframe_type="pyspark", cloud=True),
    IntegrationTestEngine("snowflake", native_dataframe_type="snowpark", cloud=True),
    IntegrationTestEngine("fabric", cloud=True),
    IntegrationTestEngine("gcp_postgres", cloud=True),
]

ENGINES_BY_NAME = {e.engine: e for e in ENGINES}


def generate_pytest_params(
    engines: t.Union[IntegrationTestEngine, t.List[IntegrationTestEngine]],
    query: bool = True,
    df: bool = False,
    show_variant_in_test_id: bool = True,
) -> t.Iterable[ParameterSet]:
    """
    The engine adapter tests have a bunch of variants:
     - Per engine for engines that dont have pluggable catalogs
     - Per engine per catalog type for engines that have pluggable catalogs

    In addition, many engine adapter functions take either a SQL Query or a DataFrame so we need to test both combinations.
    For the methods that take a DataFrame:
     - Every engine takes a Pandas DataFrame
     - A small subset of engines also take their own engine-specific DataFrame (eg Bigframe, Snowpark, Pyspark)

    This function controls the parameter generation so that:
     - Tests that only need to test SQL queries only get called once per engine/catalog
     - Tests that only need to test DataFrame's get called once for Pandas Dataframe's and once for each engine-specific DataFrame
     - Tests that need to test both SQL Queries and DataFrame's get called once for every combination of (engine, catalog, *(query, pandas df, native df))

    The goal is to prevent needing to code this kind of logic into tests:

    > if test_type == "df":
    >    pytest.skip("Test only needs to run for query")

    As well as make it easier to generate the right combinations for new databases / catalogs / DataFrame implementations
    """
    if not isinstance(engines, list):
        engines = [engines]

    for engine in engines:
        catalogs = engine.catalog_types if engine.catalog_types else [""]
        for catalog in catalogs:
            gateway = (
                f"inttest_{engine.engine}_{catalog}" if catalog else f"inttest_{engine.engine}"
            )
            if engine.engine == "athena":
                # athena only has a single gateway defined, not a gateway per catalog
                gateway = f"inttest_athena"

            variants = []
            if query:
                variants.append("query")
            if df:
                variants.append("df-pandas")
                if engine.native_dataframe_type:
                    variants.append(f"df-{engine.native_dataframe_type}")

            test_id = f"{engine.engine}_{catalog}" if catalog else f"{engine.engine}"
            default_table_format = catalog

            for variant in variants:
                yield pytest.param(
                    (engine, gateway, variant, default_table_format),
                    marks=engine.pytest_marks,
                    id=f"[{variant}]{test_id}" if show_variant_in_test_id else test_id,
                )


class MetadataResults(PydanticModel):
    tables: t.List[str] = []
    views: t.List[str] = []
    materialized_views: t.List[str] = []
    managed_tables: t.List[str] = []

    @classmethod
    def from_data_objects(cls, data_objects: t.List[DataObject]) -> MetadataResults:
        tables = []
        views = []
        materialized_views = []
        managed_tables = []
        for obj in data_objects:
            if obj.type.is_table:
                tables.append(obj.name)
            elif obj.type.is_view:
                views.append(obj.name)
            elif obj.type.is_materialized_view:
                materialized_views.append(obj.name)
            elif obj.type.is_managed_table:
                managed_tables.append(obj.name)
            else:
                raise ValueError(f"Unexpected object type: {obj.type}")
        return MetadataResults(
            tables=tables,
            views=views,
            materialized_views=materialized_views,
            managed_tables=managed_tables,
        )

    @property
    def non_temp_tables(self) -> t.List[str]:
        return [x for x in self.tables if not x.startswith("__temp") and not x.startswith("temp")]


class TestContext:
    __test__ = False  # prevent pytest trying to collect this as a test class

    def __init__(
        self,
        test_type: str,
        engine_adapter: EngineAdapter,
        mark: str,
        gateway: str,
        tmp_path: pathlib.Path,
        is_remote: bool = False,
        columns_to_types: t.Optional[t.Dict[str, t.Union[str, exp.DataType]]] = None,
    ):
        self._test_type = test_type
        self.engine_adapter = engine_adapter
        self.mark = mark
        self.gateway = gateway
        self._columns_to_types = columns_to_types
        self.test_id = random_id(short=True)
        self._context: t.Optional[Context] = None
        self.is_remote = is_remote
        self._schemas: t.List[
            str
        ] = []  # keep track of any schemas returned from self.schema() / self.table() so we can drop them at the end
        self._catalogs: t.List[
            str
        ] = []  # keep track of any catalogs created via self.create_catalog() so we can drop them at the end
        self.tmp_path = tmp_path

    @property
    def test_type(self) -> str:
        return "df" if self._test_type.startswith("df") else "query"

    @property
    def df_type(self) -> t.Optional[str]:
        if self.test_type == "df":
            # the 'pandas' part of 'df-pandas'
            return self._test_type.split("-", maxsplit=1)[1]
        return None

    @property
    def engine_type(self) -> str:
        if self.mark.startswith("gcp_postgres"):
            return "gcp_postgres"

        return self.mark.split("_")[0]

    @property
    def columns_to_types(self):
        if self._columns_to_types is None:
            self._columns_to_types = {
                "id": exp.DataType.build("int"),
                "ds": exp.DataType.build("string"),
            }
        return self._columns_to_types

    @columns_to_types.setter
    def columns_to_types(self, value: t.Dict[str, t.Union[str, exp.DataType]]):
        self._columns_to_types = {
            k: exp.DataType.build(v, dialect=self.dialect) for k, v in value.items()
        }

    @property
    def time_columns(self) -> t.List[str]:
        return [
            k
            for k, v in self.columns_to_types.items()
            if v.sql().lower().startswith("timestamp")
            or v.sql().lower().startswith("date")
            or k.lower() == "ds"
        ]

    @property
    def timestamp_columns(self) -> t.List[str]:
        return [
            k
            for k, v in self.columns_to_types.items()
            if v.sql().lower().startswith("timestamp")
            or (v.sql().lower() == "datetime" and self.dialect == "bigquery")
        ]

    @property
    def time_column(self) -> str:
        return self.time_columns[0]

    @property
    def time_formatter(self) -> t.Callable:
        return lambda x, _: exp.Literal.string(to_ds(x))

    @property
    def partitioned_by(self) -> t.List[exp.Expression]:
        return [parse_one(self.time_column)]

    @property
    def dialect(self) -> str:
        return self.engine_adapter.dialect

    @property
    def current_catalog_type(self) -> str:
        return self.engine_adapter.current_catalog_type

    @property
    def supports_merge(self) -> bool:
        if self.dialect == "spark":
            assert isinstance(self.engine_adapter, SparkEngineAdapter)
            # Spark supports MERGE on the Iceberg catalog (which is configured under "testing" in these integration tests)
            return self.engine_adapter.default_catalog == "testing"

        if self.dialect == "trino":
            assert isinstance(self.engine_adapter, TrinoEngineAdapter)
            # Trino supports MERGE on Delta and Iceberg but not Hive
            return (
                self.engine_adapter.get_catalog_type(self.engine_adapter.default_catalog) != "hive"
            )

        if self.dialect == "athena":
            return "hive" not in self.mark

        if self.dialect == "risingwave":
            return False

        return True

    @property
    def default_table_format(self) -> t.Optional[str]:
        if self.dialect in {"athena", "trino"} and "_" in self.mark:
            return self.mark.split("_", 1)[-1]  # take eg 'athena_iceberg' and return 'iceberg'
        return None

    def add_test_suffix(self, value: str) -> str:
        return f"{value}_{self.test_id}"

    def get_metadata_results(self, schema: t.Optional[SchemaName] = None) -> MetadataResults:
        schema = schema if schema else self.schema(TEST_SCHEMA)
        return MetadataResults.from_data_objects(self.engine_adapter.get_data_objects(schema))

    def _init_engine_adapter(self) -> None:
        schema = self.schema(TEST_SCHEMA)
        self.engine_adapter.drop_schema(schema, ignore_if_not_exists=True, cascade=True)
        self.engine_adapter.create_schema(schema)

    def _format_df(self, data: pd.DataFrame, to_datetime: bool = True) -> pd.DataFrame:
        for timestamp_column in self.timestamp_columns:
            if timestamp_column in data.columns:
                value = data[timestamp_column]
                if to_datetime:
                    value = pd.to_datetime(value)
                data[timestamp_column] = value.astype("datetime64[ns]")
        return data

    def init(self):
        if self.df_type == "pyspark" and not hasattr(self.engine_adapter, "is_pyspark_df"):
            pytest.skip(f"Engine adapter {self.engine_adapter} doesn't support pyspark")
        self._init_engine_adapter()

    def input_data(
        self,
        data: pd.DataFrame,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> t.Union[Query, pd.DataFrame]:
        columns_to_types = columns_to_types or self.columns_to_types
        if self.test_type == "query":
            return self.engine_adapter._values_to_sql(
                list(data.itertuples(index=False, name=None)),
                batch_start=0,
                batch_end=sys.maxsize,
                target_columns_to_types=columns_to_types,
            )
        if self.test_type == "df":
            formatted_df = self._format_df(data, to_datetime=self.dialect != "trino")
            if self.df_type == "pandas":
                return formatted_df
            if self.df_type == "pyspark":
                return self.engine_adapter.spark.createDataFrame(formatted_df)  # type: ignore
            if self.df_type == "bigframe":
                return self.engine_adapter.bigframe.read_pandas(formatted_df)  # type: ignore
            if self.df_type == "snowpark":
                return self.engine_adapter.snowpark.create_dataframe(formatted_df)  # type: ignore

            raise ValueError(f"Unknown DF type: {self.df_type}")

        raise ValueError(f"Unknown test type: {self.test_type}")

    def output_data(self, data: pd.DataFrame) -> pd.DataFrame:
        return self._format_df(data)

    def table(self, table_name: TableName, schema: str = TEST_SCHEMA) -> exp.Table:
        schema = self.add_test_suffix(schema)
        self._schemas.append(schema)

        table = exp.to_table(table_name, dialect=self.dialect)
        table.set("db", exp.parse_identifier(schema, dialect=self.dialect))

        return exp.to_table(
            normalize_model_name(
                table,
                default_catalog=self.engine_adapter.default_catalog,
                dialect=self.dialect,
            )
        )

    def physical_properties(
        self, properties_for_dialect: t.Dict[str, t.Dict[str, str | exp.Expression]]
    ) -> t.Dict[str, exp.Expression]:
        if props := properties_for_dialect.get(self.dialect):
            return {k: exp.Literal.string(v) if isinstance(v, str) else v for k, v in props.items()}
        return {}

    def schema(self, schema_name: str = TEST_SCHEMA, catalog_name: t.Optional[str] = None) -> str:
        schema_name = exp.table_name(
            normalize_model_name(
                self.add_test_suffix(
                    ".".join(
                        p
                        for p in (catalog_name or self.engine_adapter.default_catalog, schema_name)
                        if p
                    )
                    if "." not in schema_name
                    else schema_name
                ),
                default_catalog=None,
                dialect=self.dialect,
            )
        )
        self._schemas.append(schema_name)
        return schema_name

    def get_current_data(self, table: exp.Table) -> pd.DataFrame:
        df = self.engine_adapter.fetchdf(exp.select("*").from_(table), quote_identifiers=True)
        if self.dialect == "snowflake" and "id" in df.columns:
            df["id"] = df["id"].apply(lambda x: x if pd.isna(x) else int(x))
        return self._format_df(df)

    def compare_with_current(self, table: exp.Table, expected: pd.DataFrame) -> None:
        compare_dataframes(
            self.get_current_data(table),
            self.output_data(expected),
            check_dtype=False,
            check_index_type=False,
        )

    def get_table_comment(
        self,
        schema_name: str,
        table_name: str,
        table_kind: str = "BASE TABLE",
        snowflake_capitalize_ids: bool = True,
    ) -> t.Optional[str]:
        if self.dialect in ["postgres", "redshift"]:
            query = f"""
                SELECT
                    pgc.relname,
                    pg_catalog.obj_description(pgc.oid, 'pg_class')
                FROM pg_catalog.pg_class pgc
                INNER JOIN pg_catalog.pg_namespace n
                ON pgc.relnamespace = n.oid
                WHERE
                    n.nspname = '{schema_name}'
                    AND pgc.relname = '{table_name}'
                    AND pgc.relkind = '{"v" if table_kind == "VIEW" else "r"}'
                ;
            """
        elif self.dialect in ["mysql", "snowflake"]:
            # Snowflake treats all identifiers as uppercase unless they are lowercase and quoted.
            # They are lowercase and quoted in sushi but not in the inline tests.
            if self.dialect == "snowflake" and snowflake_capitalize_ids:
                schema_name = schema_name.upper()
                table_name = table_name.upper()

            comment_field_name = {
                "mysql": "table_comment",
                "snowflake": "comment",
            }

            query = f"""
                SELECT
                    table_name,
                    {comment_field_name[self.dialect]}
                FROM INFORMATION_SCHEMA.TABLES
                WHERE
                    table_schema='{schema_name}'
                    AND table_name='{table_name}'
                    AND table_type='{table_kind}'
            """
        elif self.dialect == "bigquery":
            query = f"""
                SELECT
                    table_name,
                    option_value
                FROM `region-us.INFORMATION_SCHEMA.TABLE_OPTIONS`
                WHERE
                    table_schema='{schema_name}'
                    AND table_name='{table_name}'
                    AND option_name = 'description'
            """
        elif self.dialect in ["spark", "databricks"]:
            query = f"DESCRIBE TABLE EXTENDED {schema_name}.{table_name}"
        elif self.dialect == "trino":
            query = f"""
                SELECT
                    table_name,
                    comment
                FROM system.metadata.table_comments
                WHERE
                    schema_name = '{schema_name}'
                    AND table_name = '{table_name}'
            """
        elif self.dialect == "duckdb":
            kind = "table" if table_kind == "BASE TABLE" else "view"
            query = f"""
                SELECT
                    {kind}_name,
                    comment
                FROM duckdb_{kind}s()
                WHERE
                    schema_name = '{schema_name}'
                    AND {kind}_name = '{table_name}'
            """
        elif self.dialect == "clickhouse":
            query = f"SELECT name, comment FROM system.tables WHERE database = '{schema_name}' AND name = '{table_name}'"
        elif self.dialect == "risingwave":
            query = f"""
                SELECT
                    c.relname,
                    d.description
                FROM pg_class c
                INNER JOIN pg_description d ON c.oid = d.objoid AND d.objsubid = 0
                INNER JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE
                    c.relname = '{table_name}'
                    AND n.nspname= '{schema_name}'
                    AND c.relkind = '{"v" if table_kind == "VIEW" else "r"}'
                ;
            """

        result = self.engine_adapter.fetchall(query)

        if result:
            if self.dialect == "bigquery":
                comment = result[0][1].replace('"', "").replace("\\n", "\n")
            elif self.dialect in ["spark", "databricks"]:
                comment = [x for x in result if x[0] == "Comment"]
                comment = comment[0][1] if comment else None
            else:
                comment = result[0][1]

            return comment

        return None

    def get_column_comments(
        self,
        schema_name: str,
        table_name: str,
        table_kind: str = "BASE TABLE",
        snowflake_capitalize_ids: bool = True,
    ) -> t.Dict[str, str]:
        comment_index = 1
        if self.dialect in ["postgres", "redshift"]:
            query = f"""
                SELECT
                    cols.column_name,
                    pg_catalog.col_description(pgc.oid, cols.ordinal_position::int) AS column_comment
                FROM pg_catalog.pg_class pgc
                INNER JOIN pg_catalog.pg_namespace n
                ON
                    pgc.relnamespace = n.oid
                INNER JOIN information_schema.columns cols
                ON
                    pgc.relname = cols.table_name
                    AND n.nspname = cols.table_schema
                WHERE
                    n.nspname = '{schema_name}'
                    AND pgc.relname = '{table_name}'
                    AND pgc.relkind = '{"v" if table_kind == "VIEW" else "r"}'
                ;
            """
        elif self.dialect in ["mysql", "snowflake", "trino"]:
            # Snowflake treats all identifiers as uppercase unless they are lowercase and quoted.
            # They are lowercase and quoted in sushi but not in the inline tests.
            if self.dialect == "snowflake" and snowflake_capitalize_ids:
                schema_name = schema_name.upper()
                table_name = table_name.upper()

            comment_field_name = {
                "mysql": "column_comment",
                "snowflake": "comment",
                "trino": "comment",
            }

            query = f"""
                SELECT
                    column_name,
                    {comment_field_name[self.dialect]}
                FROM
                    information_schema.columns
                WHERE
                    table_schema = '{schema_name}'
                    AND table_name = '{table_name}'
            """
        elif self.dialect == "bigquery":
            query = f"""
                SELECT
                    column_name,
                    description
                FROM
                    `region-us.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
                WHERE
                    table_schema = '{schema_name}'
                    AND table_name = '{table_name}'
                ;
            """
        elif self.dialect in ["spark", "databricks", "clickhouse"]:
            query = f"DESCRIBE TABLE {schema_name}.{table_name}"
            comment_index = 2 if self.dialect in ["spark", "databricks"] else 4
        elif self.dialect == "duckdb":
            query = f"""
                SELECT
                    column_name,
                    comment
                FROM duckdb_columns()
                WHERE
                    schema_name = '{schema_name}'
                    AND table_name = '{table_name}'
            """
        elif self.dialect == "risingwave":
            query = f"""
                SELECT
                    a.attname AS column_name, d.description
                FROM
                    pg_class c
                INNER JOIN pg_namespace n ON c.relnamespace = n.oid
                INNER JOIN pg_attribute a ON c.oid = a.attrelid
                INNER JOIN pg_description d
                ON
                    a.attnum = d.objsubid
                    AND d.objoid = c.oid
                WHERE
                    n.nspname = '{schema_name}'
                    AND c.relname = '{table_name}'
                    AND c.relkind = '{"v" if table_kind == "VIEW" else "r"}'
                ;
            """

        result = self.engine_adapter.fetchall(query)

        comments = {}
        if result:
            if self.dialect in ["spark", "databricks"]:
                result = list(set([x for x in result if not x[0].startswith("#")]))

            comments = {
                x[0]: x[comment_index]
                for x in result
                if x[comment_index] is not None and x[comment_index].strip() != ""
            }

        return comments

    def create_context(
        self,
        config_mutator: t.Optional[t.Callable[[str, Config], None]] = None,
        path: t.Optional[pathlib.Path] = None,
        ephemeral_state_connection: bool = True,
    ) -> Context:
        private_sqlmesh_dir = pathlib.Path(pathlib.Path().home(), ".sqlmesh")
        config = load_config_from_paths(
            Config,
            project_paths=[
                pathlib.Path(os.path.join(os.path.dirname(__file__), "config.yaml")),
                private_sqlmesh_dir / "config.yml",
                private_sqlmesh_dir / "config.yaml",
            ],
            variables={"tmp_path": str(path or self.tmp_path)},
        )
        if config_mutator:
            config_mutator(self.gateway, config)
        config.gateways = {self.gateway: config.gateways[self.gateway]}

        gateway_config = config.gateways[self.gateway]
        if ephemeral_state_connection:
            # Override whatever state connection has been configured on the integration test config to use in-memory DuckDB instead
            # This is so tests that initialize a SQLMesh context can run concurrently without clobbering each others state
            from sqlmesh.core.config.connection import DuckDBConnectionConfig

            gateway_config.state_connection = DuckDBConnectionConfig()

        if "athena" in self.gateway:
            conn = gateway_config.connection
            assert isinstance(conn, AthenaConnectionConfig)
            assert isinstance(self.engine_adapter, AthenaEngineAdapter)
            # Ensure that s3_warehouse_location is propagated
            conn.s3_warehouse_location = self.engine_adapter.s3_warehouse_location

        self._context = Context(paths=path or ".", config=config, gateway=self.gateway)
        return self._context

    def create_catalog(self, catalog_name: str):
        if self.dialect == "databricks":
            self.engine_adapter.execute(f"CREATE CATALOG IF NOT EXISTS {catalog_name}")
        elif self.dialect == "tsql":
            self.engine_adapter.cursor.connection.autocommit(True)
            try:
                self.engine_adapter.cursor.execute(f"CREATE DATABASE {catalog_name}")
            except Exception:
                pass
            self.engine_adapter.cursor.connection.autocommit(False)
        elif self.dialect == "fabric":
            # Use the engine adapter's built-in catalog creation functionality
            self.engine_adapter.create_catalog(catalog_name)
        elif self.dialect == "snowflake":
            self.engine_adapter.execute(f'CREATE DATABASE IF NOT EXISTS "{catalog_name}"')
        elif self.dialect == "duckdb":
            try:
                # Only applies to MotherDuck
                self.engine_adapter.execute(f'CREATE DATABASE IF NOT EXISTS "{catalog_name}"')
            except Exception:
                pass

        self._catalogs.append(catalog_name)

    def drop_catalog(self, catalog_name: str):
        if self.dialect == "bigquery":
            return  # bigquery cannot create/drop catalogs
        if self.dialect == "databricks":
            self.engine_adapter.execute(f"DROP CATALOG IF EXISTS {catalog_name} CASCADE")
        elif self.dialect == "fabric":
            # Use the engine adapter's built-in catalog dropping functionality
            self.engine_adapter.drop_catalog(catalog_name)
        else:
            self.engine_adapter.execute(f'DROP DATABASE IF EXISTS "{catalog_name}"')

    def cleanup(self, ctx: t.Optional[Context] = None):
        self._schemas.append(self.schema(TEST_SCHEMA))

        ctx = ctx or self._context
        if ctx and ctx.models:
            for _, model in ctx.models.items():
                self._schemas.append(model.schema_name)
                self._schemas.append(model.physical_schema)

        for schema_name in set(self._schemas):
            self.engine_adapter.drop_schema(
                schema_name=schema_name, ignore_if_not_exists=True, cascade=True
            )

        for catalog_name in set(self._catalogs):
            self.drop_catalog(catalog_name)

        self.engine_adapter.close()

    def upsert_sql_model(self, model_definition: str) -> t.Tuple[Context, SqlModel]:
        if not self._context:
            self._context = self.create_context()

        model = load_sql_based_model(expressions=d.parse(model_definition))
        assert isinstance(model, SqlModel)
        self._context.upsert_model(model)
        return self._context, model

    def _get_create_user_or_role(
        self, username: str, password: t.Optional[str] = None
    ) -> t.Tuple[str, t.Optional[str]]:
        password = password or random_id()
        if self.dialect in ["postgres", "redshift"]:
            return username, f"CREATE USER \"{username}\" WITH PASSWORD '{password}'"
        if self.dialect == "snowflake":
            return username, f"CREATE ROLE {username}"
        if self.dialect == "databricks":
            # Creating an account-level group in Databricks requires making REST API calls so we are going to
            # use a pre-created group instead. We assume the suffix on the name is the unique id.
            # In the Databricks UI, Workspace Settings -> Identity and Access, create the following groups:
            #  - test_user, test_analyst, test_etl_user, test_reader, test_writer, test_admin
            # (there do not need to be any users assigned to these groups)
            return "_".join(username.split("_")[:-1]), None
        if self.dialect == "bigquery":
            # BigQuery uses IAM service accounts that need to be pre-created
            # Pre-created GCP service accounts:
            # - sqlmesh-test-admin@{project-id}.iam.gserviceaccount.com
            # - sqlmesh-test-analyst@{project-id}.iam.gserviceaccount.com
            # - sqlmesh-test-etl-user@{project-id}.iam.gserviceaccount.com
            # - sqlmesh-test-reader@{project-id}.iam.gserviceaccount.com
            # - sqlmesh-test-user@{project-id}.iam.gserviceaccount.com
            # - sqlmesh-test-writer@{project-id}.iam.gserviceaccount.com
            role_name = (
                username.replace(f"_{self.test_id}", "").replace("test_", "").replace("_", "-")
            )
            project_id = self.engine_adapter.get_current_catalog()
            service_account = f"sqlmesh-test-{role_name}@{project_id}.iam.gserviceaccount.com"
            return f"serviceAccount:{service_account}", None
        raise ValueError(f"User creation not supported for dialect: {self.dialect}")

    def _create_user_or_role(self, username: str, password: t.Optional[str] = None) -> str:
        username, create_user_sql = self._get_create_user_or_role(username, password)
        if create_user_sql:
            self.engine_adapter.execute(create_user_sql)
        return username

    @contextmanager
    def create_users_or_roles(self, *role_names: str) -> t.Iterator[t.Dict[str, str]]:
        created_users = []
        roles = {}

        try:
            for role_name in role_names:
                user_name = normalize_identifiers(
                    self.add_test_suffix(f"test_{role_name}"), dialect=self.dialect
                ).sql(dialect=self.dialect)
                password = random_id()
                if self.dialect == "redshift":
                    password += (
                        "A"  # redshift requires passwords to have at least one uppercase letter
                    )
                user_name = self._create_user_or_role(user_name, password)
                created_users.append(user_name)
                roles[role_name] = user_name

            yield roles

        finally:
            for user_name in created_users:
                self._cleanup_user_or_role(user_name)

    def get_select_privilege(self) -> str:
        if self.dialect == "bigquery":
            return "roles/bigquery.dataViewer"
        return "SELECT"

    def get_insert_privilege(self) -> str:
        if self.dialect == "databricks":
            # This would really be "MODIFY" but for the purposes of having this be unique from UPDATE
            # we return "MANAGE" instead
            return "MANAGE"
        if self.dialect == "bigquery":
            return "roles/bigquery.dataEditor"
        return "INSERT"

    def get_update_privilege(self) -> str:
        if self.dialect == "databricks":
            return "MODIFY"
        if self.dialect == "bigquery":
            return "roles/bigquery.dataOwner"
        return "UPDATE"

    def _cleanup_user_or_role(self, user_name: str) -> None:
        """Helper function to clean up a user/role and all their dependencies."""
        try:
            if self.dialect in ["postgres", "redshift"]:
                self.engine_adapter.execute(f"""
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE usename = '{user_name}' AND pid <> pg_backend_pid()
                """)
                self.engine_adapter.execute(f'DROP OWNED BY "{user_name}"')
                self.engine_adapter.execute(f'DROP USER IF EXISTS "{user_name}"')
            elif self.dialect == "snowflake":
                self.engine_adapter.execute(f"DROP ROLE IF EXISTS {user_name}")
            elif self.dialect in ["databricks", "bigquery"]:
                # For Databricks and BigQuery, we use pre-created accounts that should not be deleted
                pass
        except Exception:
            pass


def wait_until(fn: t.Callable[..., bool], attempts=3, wait=5) -> None:
    current_attempt = 0
    while current_attempt < attempts:
        current_attempt += 1
        result = fn()
        if result:
            return
        time.sleep(wait)

    raise Exception(f"Wait function did not return True after {attempts} attempts")
