from __future__ import annotations

import datetime
import typing as t
import logging

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.helper import ensure_list

from sqlmesh.core import dialect as d
from sqlmesh.core.config.base import UpdateStrategy
from sqlmesh.core.config.common import VirtualEnvironmentMode
from sqlmesh.core.console import get_console
from sqlmesh.core.model import (
    EmbeddedKind,
    FullKind,
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    IncrementalUnmanagedKind,
    Model,
    ModelKind,
    SCDType2ByColumnKind,
    ViewKind,
    ManagedKind,
    create_sql_model,
)
from sqlmesh.core.model.kind import (
    SCDType2ByTimeKind,
    OnDestructiveChange,
    OnAdditiveChange,
    on_destructive_change_validator,
    on_additive_change_validator,
    DbtCustomKind,
)
from sqlmesh.dbt.basemodel import BaseModelConfig, Materialization, SnapshotStrategy
from sqlmesh.dbt.common import SqlStr, sql_str_validator
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import field_validator

if t.TYPE_CHECKING:
    from sqlmesh.core.audit.definition import ModelAudit
    from sqlmesh.dbt.context import DbtContext
    from sqlmesh.dbt.package import MaterializationConfig

logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)


INCREMENTAL_BY_TIME_RANGE_STRATEGIES = set(
    ["delete+insert", "insert_overwrite", "microbatch", "incremental_by_time_range"]
)
INCREMENTAL_BY_UNIQUE_KEY_STRATEGIES = set(["merge"])


def collection_to_str(collection: t.Iterable) -> str:
    return ", ".join(f"'{item}'" for item in sorted(collection))


class ModelConfig(BaseModelConfig):
    """
    ModelConfig contains all config parameters available to DBT models

    See https://docs.getdbt.com/reference/configs-and-properties for
    a more detailed description of each config parameter under the
    General propreties, General configs, and For models sections.

    Args:
        sql: The model sql
        time_column: The name of the time column
        cron: A cron string specifying how often the model should be refreshed, leveraging the
            [croniter](https://github.com/kiorky/croniter) library.
        interval_unit: The duration of an interval for the model. By default, it is computed from the cron expression.
        batch_size: The maximum number of incremental intervals that can be run per backfill job. If this is None,
            then backfilling this model will do all of history in one job. If this is set, a model's backfill
            will be chunked such that each individual job will only contain jobs with max `batch_size` intervals.
        lookback: The number of previous incremental intervals in the lookback window.
        cluster_by: Field(s) to use for clustering in data warehouses that support clustering
        incremental_strategy: Strategy used to build the incremental model
        materialized: How the model will be materialized in the database
        sql_header: SQL statement to run before table/view creation. Currently implemented as a pre-hook.
        unique_key: List of columns that define row uniqueness for the model
        partition_by: List of partition columns or dictionary of bigquery partition by parameters ([dbt bigquery config](https://docs.getdbt.com/reference/resource-configs/bigquery-configs)).
    """

    # sqlmesh fields
    sql: SqlStr = SqlStr("")
    time_column: t.Optional[t.Union[str, t.Dict[str, str]]] = None
    cron: t.Optional[str] = None
    interval_unit: t.Optional[str] = None
    batch_concurrency: t.Optional[int] = None
    forward_only: bool = True
    disable_restatement: t.Optional[bool] = None
    allow_partials: bool = True
    physical_version: t.Optional[str] = None
    auto_restatement_cron: t.Optional[str] = None
    auto_restatement_intervals: t.Optional[int] = None
    partition_by_time_column: t.Optional[bool] = None
    on_destructive_change: t.Optional[OnDestructiveChange] = None
    on_additive_change: t.Optional[OnAdditiveChange] = None

    # DBT configuration fields
    cluster_by: t.Optional[t.List[str]] = None
    incremental_strategy: t.Optional[str] = None
    materialized: str = Materialization.VIEW.value
    sql_header: t.Optional[str] = None
    unique_key: t.Optional[t.List[str]] = None
    partition_by: t.Optional[t.Union[t.List[str], t.Dict[str, t.Any]]] = None
    full_refresh: t.Optional[bool] = None
    on_schema_change: str = "ignore"

    # Snapshot (SCD Type 2) Fields
    updated_at: t.Optional[str] = None
    strategy: t.Optional[str] = None
    invalidate_hard_deletes: bool = False
    target_schema: t.Optional[str] = None
    check_cols: t.Optional[t.Union[t.List[str], str]] = None

    # Microbatch Fields
    event_time: t.Optional[str] = None
    begin: t.Optional[datetime.datetime] = None
    concurrent_batches: t.Optional[bool] = None

    # Shared SQLMesh and DBT configuration fields
    batch_size: t.Optional[t.Union[int, str]] = None
    lookback: t.Optional[int] = None

    # redshift
    bind: t.Optional[bool] = None

    # bigquery
    require_partition_filter: t.Optional[bool] = None
    partition_expiration_days: t.Optional[int] = None

    # snowflake
    snowflake_warehouse: t.Optional[str] = None
    # note: for Snowflake dynamic tables, in the DBT adapter we only support properties that DBT supports
    # which are defined here: https://docs.getdbt.com/reference/resource-configs/snowflake-configs#dynamic-tables
    target_lag: t.Optional[str] = None

    # clickhouse
    engine: t.Optional[str] = None
    order_by: t.Optional[t.Union[t.List[str], str]] = None
    primary_key: t.Optional[t.Union[t.List[str], str]] = None
    sharding_key: t.Optional[t.Union[t.List[str], str]] = None
    ttl: t.Optional[t.Union[t.List[str], str]] = None
    settings: t.Optional[t.Dict[str, t.Any]] = None
    query_settings: t.Optional[t.Dict[str, t.Any]] = None
    inserts_only: t.Optional[bool] = None
    incremental_predicates: t.Optional[t.List[str]] = None

    _sql_validator = sql_str_validator
    _on_destructive_change_validator = on_destructive_change_validator
    _on_additive_change_validator = on_additive_change_validator

    @field_validator(
        "unique_key",
        "cluster_by",
        "tags",
        mode="before",
    )
    @classmethod
    def _validate_list(cls, v: t.Union[str, t.List[str]]) -> t.List[str]:
        return ensure_list(v)

    @field_validator("check_cols", mode="before")
    @classmethod
    def _validate_check_cols(cls, v: t.Union[str, t.List[str]]) -> t.Union[str, t.List[str]]:
        if isinstance(v, str) and v.lower() == "all":
            return "*"
        return ensure_list(v)

    @field_validator("updated_at", mode="before")
    @classmethod
    def _validate_updated_at(cls, v: t.Optional[str]) -> t.Optional[str]:
        """
        Extract column name if updated_at contains a cast.

        SCDType2ByTimeKind and SCDType2ByColumnKind expect a column, and the casting is done later.
        """
        if v is None:
            return None
        parsed = d.parse_one(v)
        if isinstance(parsed, exp.Cast) and isinstance(parsed.this, exp.Column):
            return parsed.this.name

        return v

    @field_validator("sql", mode="before")
    @classmethod
    def _validate_sql(cls, v: t.Union[str, SqlStr]) -> SqlStr:
        return SqlStr(v)

    @field_validator("partition_by", mode="before")
    @classmethod
    def _validate_partition_by(
        cls, v: t.Any
    ) -> t.Optional[t.Union[t.List[str], t.Dict[str, t.Any]]]:
        if v is None:
            return None
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            if not v.get("field"):
                raise ConfigError("'field' key required for partition_by.")
            if "granularity" in v and v["granularity"].lower() not in (
                "day",
                "month",
                "year",
                "hour",
            ):
                granularity = v["granularity"]
                raise ConfigError(f"Unexpected granularity '{granularity}' in partition_by '{v}'.")
            if "data_type" in v and v["data_type"].lower() not in (
                "timestamp",
                "date",
                "datetime",
                "int64",
            ):
                data_type = v["data_type"]
                raise ConfigError(f"Unexpected data_type '{data_type}' in partition_by '{v}'.")
            return {"data_type": "date", "granularity": "day", **v}
        raise ConfigError(f"Invalid format for partition_by '{v}'")

    @field_validator("materialized", mode="before")
    @classmethod
    def _validate_materialized(cls, v: str) -> str:
        unsupported_materializations = [
            "materialized_view",  # multiple engines
            "dictionary",  # clickhouse only
            "distributed_table",  # clickhouse only
            "distributed_incremental",  # clickhouse only
        ]
        if v in unsupported_materializations:
            fallback = v.split("_")
            msg = f"SQLMesh does not support the '{v}' model materialization."
            if len(fallback) == 1:
                # dictionary materialization
                raise ConfigError(msg)
            else:
                get_console().log_warning(
                    f"{msg} Falling back to the '{fallback[1]}' materialization."
                )

            return fallback[1]
        return v

    _FIELD_UPDATE_STRATEGY: t.ClassVar[t.Dict[str, UpdateStrategy]] = {
        **BaseModelConfig._FIELD_UPDATE_STRATEGY,
        **{
            "sql": UpdateStrategy.IMMUTABLE,
            "time_column": UpdateStrategy.IMMUTABLE,
        },
    }

    @property
    def model_materialization(self) -> Materialization:
        return Materialization(self.materialized.lower())

    @property
    def snapshot_strategy(self) -> t.Optional[SnapshotStrategy]:
        return SnapshotStrategy(self.strategy.lower()) if self.strategy else None

    @property
    def table_schema(self) -> str:
        return self.target_schema or super().table_schema

    def model_kind(self, context: DbtContext) -> ModelKind:
        """
        Get the sqlmesh ModelKind
        Returns:
            The sqlmesh ModelKind
        """
        target = context.target
        materialization = self.model_materialization

        # args common to all sqlmesh incremental kinds, regardless of materialization
        incremental_kind_kwargs: t.Dict[str, t.Any] = {}
        on_schema_change = self.on_schema_change.lower()
        if materialization == Materialization.SNAPSHOT:
            # dbt snapshots default to `append_new_columns` behavior and can't be changed
            on_schema_change = "append_new_columns"

        if on_schema_change == "ignore":
            on_destructive_change = OnDestructiveChange.IGNORE
            on_additive_change = OnAdditiveChange.IGNORE
        elif on_schema_change == "fail":
            on_destructive_change = OnDestructiveChange.ERROR
            on_additive_change = OnAdditiveChange.ERROR
        elif on_schema_change == "append_new_columns":
            on_destructive_change = OnDestructiveChange.IGNORE
            on_additive_change = OnAdditiveChange.ALLOW
        elif on_schema_change == "sync_all_columns":
            on_destructive_change = OnDestructiveChange.ALLOW
            on_additive_change = OnAdditiveChange.ALLOW
        else:
            raise ConfigError(
                f"{self.canonical_name(context)}: Invalid on_schema_change value '{on_schema_change}'. "
                "Valid values are 'ignore', 'fail', 'append_new_columns', 'sync_all_columns'."
            )

        incremental_kind_kwargs["on_destructive_change"] = (
            self._get_field_value("on_destructive_change") or on_destructive_change
        )
        incremental_kind_kwargs["on_additive_change"] = (
            self._get_field_value("on_additive_change") or on_additive_change
        )
        auto_restatement_cron_value = self._get_field_value("auto_restatement_cron")
        if auto_restatement_cron_value is not None:
            incremental_kind_kwargs["auto_restatement_cron"] = auto_restatement_cron_value

        if materialization == Materialization.TABLE:
            return FullKind()
        if materialization == Materialization.VIEW:
            return ViewKind()
        if materialization == Materialization.INCREMENTAL:
            incremental_by_kind_kwargs: t.Dict[str, t.Any] = {"dialect": self.dialect(context)}
            forward_only_value = self._get_field_value("forward_only")
            if forward_only_value is not None:
                incremental_kind_kwargs["forward_only"] = forward_only_value

            is_incremental_by_time_range = self.time_column or (
                self.incremental_strategy
                and self.incremental_strategy in {"microbatch", "incremental_by_time_range"}
            )
            # Get shared incremental by kwargs
            for field in ("batch_size", "batch_concurrency", "lookback"):
                field_val = self._get_field_value(field)
                if field_val is not None:
                    # Check if `batch_size` is representing an interval unit and if so that will be handled at the model level
                    if field == "batch_size" and isinstance(field_val, str):
                        continue
                    incremental_by_kind_kwargs[field] = field_val

            disable_restatement = self.disable_restatement
            if disable_restatement is None:
                if is_incremental_by_time_range:
                    disable_restatement = False
                else:
                    disable_restatement = (
                        not self.full_refresh if self.full_refresh is not None else False
                    )
            incremental_by_kind_kwargs["disable_restatement"] = disable_restatement

            if is_incremental_by_time_range:
                strategy = self.incremental_strategy or target.default_incremental_strategy(
                    IncrementalByTimeRangeKind
                )

                if strategy not in INCREMENTAL_BY_TIME_RANGE_STRATEGIES:
                    get_console().log_warning(
                        f"SQLMesh incremental by time strategy is not compatible with '{strategy}' incremental strategy in model '{self.canonical_name(context)}'. "
                        f"Supported strategies include {collection_to_str(INCREMENTAL_BY_TIME_RANGE_STRATEGIES)}."
                    )

                if self.time_column and strategy != "incremental_by_time_range":
                    get_console().log_warning(
                        f"Using `time_column` on a model with incremental_strategy '{strategy}' has been deprecated. "
                        f"Please use `incremental_by_time_range` instead in model '{self.canonical_name(context)}'."
                    )

                if strategy == "microbatch":
                    if self.time_column:
                        raise ConfigError(
                            f"{self.canonical_name(context)}: 'time_column' cannot be used with 'microbatch' incremental strategy. Use 'event_time' instead."
                        )
                    time_column = self._get_field_value("event_time")
                    if not time_column:
                        raise ConfigError(
                            f"{self.canonical_name(context)}: 'event_time' is required for microbatch incremental strategy."
                        )
                    # dbt microbatch always processes batches in a size of 1
                    incremental_by_kind_kwargs["batch_size"] = 1
                else:
                    if not self.time_column:
                        raise ConfigError(
                            f"{self.canonical_name(context)}: 'time_column' is required for incremental by time range models not defined using microbatch."
                        )
                    time_column = self.time_column

                incremental_by_time_range_kwargs = {
                    "time_column": time_column,
                }
                if self.auto_restatement_intervals:
                    incremental_by_time_range_kwargs["auto_restatement_intervals"] = (
                        self.auto_restatement_intervals
                    )
                if self.partition_by_time_column is not None:
                    incremental_by_time_range_kwargs["partition_by_time_column"] = (
                        self.partition_by_time_column
                    )

                return IncrementalByTimeRangeKind(
                    **incremental_kind_kwargs,
                    **incremental_by_kind_kwargs,
                    **incremental_by_time_range_kwargs,
                )

            if self.unique_key:
                strategy = self.incremental_strategy or target.default_incremental_strategy(
                    IncrementalByUniqueKeyKind
                )
                if (
                    self.incremental_strategy
                    and strategy not in INCREMENTAL_BY_UNIQUE_KEY_STRATEGIES
                ):
                    get_console().log_warning(
                        f"Unique key is not compatible with '{strategy}' incremental strategy in model '{self.canonical_name(context)}'. "
                        f"Supported strategies include {collection_to_str(INCREMENTAL_BY_UNIQUE_KEY_STRATEGIES)}. Falling back to 'merge' strategy."
                    )

                merge_filter = None
                if self.incremental_predicates:
                    dialect = self.dialect(context)
                    merge_filter = exp.and_(
                        *[
                            d.parse_one(predicate, dialect=dialect)
                            for predicate in self.incremental_predicates
                        ],
                        dialect=dialect,
                    ).transform(d.replace_merge_table_aliases)

                return IncrementalByUniqueKeyKind(
                    unique_key=self.unique_key,
                    merge_filter=merge_filter,
                    **incremental_kind_kwargs,
                    **incremental_by_kind_kwargs,
                )

            strategy = self.incremental_strategy or target.default_incremental_strategy(
                IncrementalUnmanagedKind
            )
            return IncrementalUnmanagedKind(
                insert_overwrite=strategy in INCREMENTAL_BY_TIME_RANGE_STRATEGIES,
                disable_restatement=incremental_by_kind_kwargs["disable_restatement"],
                **incremental_kind_kwargs,
            )
        if materialization == Materialization.EPHEMERAL:
            return EmbeddedKind()
        if materialization == Materialization.SNAPSHOT:
            if not self.snapshot_strategy:
                raise ConfigError(
                    f"{self.canonical_name(context)}: SQLMesh snapshot strategy is required for snapshot materialization."
                )
            shared_kwargs = {
                "dialect": self.dialect(context),
                "unique_key": self.unique_key,
                "invalidate_hard_deletes": self.invalidate_hard_deletes,
                "valid_from_name": "dbt_valid_from",
                "valid_to_name": "dbt_valid_to",
                "time_data_type": (
                    exp.DataType.build("TIMESTAMPTZ")
                    if target.dialect == "bigquery"
                    else exp.DataType.build("TIMESTAMP")
                ),
                **incremental_kind_kwargs,
            }
            if self.snapshot_strategy.is_check:
                return SCDType2ByColumnKind(
                    columns=self.check_cols, execution_time_as_valid_from=True, **shared_kwargs
                )
            return SCDType2ByTimeKind(
                updated_at_name=self.updated_at, updated_at_as_valid_from=True, **shared_kwargs
            )

        if materialization == Materialization.DYNAMIC_TABLE:
            return ManagedKind()

        if materialization == Materialization.CUSTOM:
            if custom_materialization := self._get_custom_materialization(context):
                return DbtCustomKind(
                    materialization=self.materialized,
                    adapter=custom_materialization.adapter,
                    dialect=self.dialect(context),
                    definition=custom_materialization.definition,
                )

            raise ConfigError(
                f"Unknown materialization '{self.materialized}'. Custom materializations must be defined in your dbt project."
            )

        raise ConfigError(f"{materialization.value} materialization not supported.")

    def _big_query_partition_by_expr(self, context: DbtContext) -> exp.Expr:
        assert isinstance(self.partition_by, dict)
        data_type = self.partition_by["data_type"].lower()
        raw_field = self.partition_by["field"]
        try:
            field = d.parse_one(raw_field, dialect="bigquery")
        except SqlglotError as e:
            raise ConfigError(
                f"Failed to parse model '{self.canonical_name(context)}' partition_by field '{raw_field}' in '{self.path}': {e}"
            ) from e

        if data_type == "date" and self.partition_by["granularity"].lower() == "day":
            return field

        if data_type == "int64":
            if "range" not in self.partition_by:
                raise ConfigError(
                    f"Range is required for int64 partitioning in model '{self.canonical_name(context)}'."
                )

            range_ = self.partition_by["range"]
            start = range_["start"]
            end = range_["end"]
            interval = range_["interval"]

            return exp.func(
                "RANGE_BUCKET",
                field,
                exp.func("GENERATE_ARRAY", start, end, interval, dialect="bigquery"),
                dialect="bigquery",
            )

        return d.parse_one(
            f"{data_type}_trunc({self.partition_by['field']}, {self.partition_by['granularity'].upper()})",
            dialect="bigquery",
        )

    def _get_custom_materialization(self, context: DbtContext) -> t.Optional[MaterializationConfig]:
        materializations = context.manifest.materializations()
        name, target_adapter = self.materialized, context.target.dialect

        adapter_specific_key = f"{name}_{target_adapter}"
        default_key = f"{name}_default"
        if adapter_specific_key in materializations:
            return materializations[adapter_specific_key]
        if default_key in materializations:
            return materializations[default_key]
        return None

    @property
    def sqlmesh_config_fields(self) -> t.Set[str]:
        return super().sqlmesh_config_fields | {
            "cron",
            "interval_unit",
            "allow_partials",
            "physical_version",
            "start",
            # In microbatch models `begin` is the same as `start`
            "begin",
        }

    def to_sqlmesh(
        self,
        context: DbtContext,
        audit_definitions: t.Optional[t.Dict[str, ModelAudit]] = None,
        virtual_environment_mode: VirtualEnvironmentMode = VirtualEnvironmentMode.default,
    ) -> Model:
        """Converts the dbt model into a SQLMesh model."""
        model_dialect = self.dialect(context)
        query = d.jinja_query(self.sql)
        kind = self.model_kind(context)

        optional_kwargs: t.Dict[str, t.Any] = {}
        physical_properties: t.Dict[str, t.Any] = {}

        if self.partition_by:
            if isinstance(kind, (ViewKind, EmbeddedKind)):
                logger.warning(
                    "Ignoring partition_by config for model '%s'; partition_by is not supported for %s.",
                    self.name,
                    "views" if isinstance(kind, ViewKind) else "ephemeral models",
                )
            elif context.target.dialect == "snowflake":
                logger.warning(
                    "Ignoring partition_by config for model '%s' targeting %s. The partition_by config is not supported for Snowflake.",
                    self.name,
                    context.target.dialect,
                )
            else:
                partitioned_by = []
                if isinstance(self.partition_by, list):
                    for p in self.partition_by:
                        try:
                            partitioned_by.append(d.parse_one(p, dialect=model_dialect))
                        except SqlglotError as e:
                            raise ConfigError(
                                f"Failed to parse model '{self.canonical_name(context)}' partition_by field '{p}' in '{self.path}': {e}"
                            ) from e
                elif isinstance(self.partition_by, dict):
                    if context.target.dialect == "bigquery":
                        partitioned_by.append(self._big_query_partition_by_expr(context))
                    else:
                        logger.warning(
                            "Ignoring partition_by config for model '%s' targeting %s. The format of the config field is only supported for BigQuery.",
                            self.name,
                            context.target.dialect,
                        )

                if partitioned_by:
                    optional_kwargs["partitioned_by"] = partitioned_by

        if self.cluster_by:
            if isinstance(kind, (ViewKind, EmbeddedKind)):
                logger.warning(
                    "Ignoring cluster_by config for model '%s'; cluster_by is not supported for %s.",
                    self.name,
                    "views" if isinstance(kind, ViewKind) else "ephemeral models",
                )
            else:
                clustered_by = []
                for c in self.cluster_by:
                    try:
                        cluster_expr = exp.maybe_parse(
                            c, into=exp.Cluster, prefix="CLUSTER BY", dialect=model_dialect
                        )
                        for expr in cluster_expr.expressions:
                            clustered_by.append(
                                expr.this if isinstance(expr, exp.Ordered) else expr
                            )
                    except SqlglotError as e:
                        raise ConfigError(
                            f"Failed to parse model '{self.canonical_name(context)}' cluster_by field '{c}' in '{self.path}': {e}"
                        ) from e
                optional_kwargs["clustered_by"] = clustered_by

        model_kwargs = self.sqlmesh_model_kwargs(context)
        if self.sql_header:
            model_kwargs["pre_statements"].insert(0, d.jinja_statement(self.sql_header))

        if context.target.dialect == "bigquery":
            dbt_max_partition_blob = self._dbt_max_partition_blob()
            if dbt_max_partition_blob:
                model_kwargs["pre_statements"].append(d.jinja_statement(dbt_max_partition_blob))

            if self.partition_expiration_days is not None:
                physical_properties["partition_expiration_days"] = self.partition_expiration_days
            if self.require_partition_filter is not None:
                physical_properties["require_partition_filter"] = self.require_partition_filter

            if physical_properties:
                model_kwargs["physical_properties"] = physical_properties

        if context.target.dialect == "snowflake":
            if self.snowflake_warehouse is not None:
                model_kwargs["session_properties"] = {"warehouse": self.snowflake_warehouse}

            if self.model_materialization == Materialization.DYNAMIC_TABLE:
                if not self.snowflake_warehouse:
                    raise ConfigError("`snowflake_warehouse` must be set for dynamic tables")
                if not self.target_lag:
                    raise ConfigError("`target_lag` must be set for dynamic tables")

                model_kwargs["physical_properties"] = {
                    "warehouse": self.snowflake_warehouse,
                    "target_lag": self.target_lag,
                }

        if context.target.dialect == "clickhouse":
            if self.model_materialization == Materialization.INCREMENTAL:
                # `inserts_only` overrides incremental_strategy setting (if present)
                # https://github.com/ClickHouse/dbt-clickhouse/blob/065f3a724fa09205446ecadac7a00d92b2d8c646/README.md?plain=1#L108
                if self.inserts_only:
                    self.incremental_strategy = "append"

                if self.incremental_strategy == "delete+insert":
                    get_console().log_warning(
                        f"The '{self.incremental_strategy}' incremental strategy is not supported - SQLMesh will use the temp table/partition swap strategy."
                    )

                if self.incremental_predicates:
                    get_console().log_warning(
                        "SQLMesh does not support 'incremental_predicates' - they will not be applied."
                    )

            if self.query_settings:
                get_console().log_warning(
                    "SQLMesh does not support the 'query_settings' model configuration parameter. Specify the query settings directly in the model query."
                )

            if self.engine:
                optional_kwargs["storage_format"] = self.engine

            if self.order_by:
                order_by = []
                for o in self.order_by if isinstance(self.order_by, list) else [self.order_by]:
                    try:
                        order_by.append(d.parse_one(o, dialect=model_dialect))
                    except SqlglotError as e:
                        raise ConfigError(
                            f"Failed to parse model '{self.canonical_name(context)}' 'order_by' field '{o}' in '{self.path}': {e}"
                        ) from e
                physical_properties["order_by"] = order_by

            if self.primary_key:
                primary_key = []
                for p in self.primary_key:
                    try:
                        primary_key.append(d.parse_one(p, dialect=model_dialect))
                    except SqlglotError as e:
                        raise ConfigError(
                            f"Failed to parse model '{self.canonical_name(context)}' 'primary_key' field '{p}' in '{self.path}': {e}"
                        ) from e
                physical_properties["primary_key"] = primary_key

            if self.sharding_key:
                get_console().log_warning(
                    "SQLMesh does not support the 'sharding_key' model configuration parameter or distributed materializations."
                )

            if self.ttl:
                physical_properties["ttl"] = exp.var(
                    self.ttl[0] if isinstance(self.ttl, list) else self.ttl
                )

            if self.settings:
                physical_properties.update({k: exp.var(v) for k, v in self.settings.items()})

            if physical_properties:
                model_kwargs["physical_properties"] = physical_properties

        kind = self.model_kind(context)

        # A falsy grants config (None or {}) is considered as unmanaged per dbt semantics
        if self.grants and kind.supports_grants:
            model_kwargs["grants"] = self.grants

        allow_partials = model_kwargs.pop("allow_partials", None)
        if allow_partials is None:
            # Set allow_partials to True for dbt models to preserve the original semantics.
            allow_partials = True

        # pop begin for all models so we don't pass it through for non-incremental materializations
        # (happens if model config is microbatch but project config overrides)
        begin = model_kwargs.pop("begin", None)
        if kind.is_incremental:
            if self.batch_size and isinstance(self.batch_size, str):
                if "interval_unit" in model_kwargs:
                    get_console().log_warning(
                        f"Both 'interval_unit' and 'batch_size' are set for model '{self.canonical_name(context)}'. 'interval_unit' will be used."
                    )
                else:
                    model_kwargs["interval_unit"] = self.batch_size
                    self.batch_size = None
            if begin:
                if "start" in model_kwargs:
                    get_console().log_warning(
                        f"Both 'begin' and 'start' are set for model '{self.canonical_name(context)}'. 'start' will be used."
                    )
                else:
                    model_kwargs["start"] = begin
            # If user explicitly disables concurrent batches then we want to set depends on past to true which we
            # will do by including the model in the depends_on
            if self.concurrent_batches is not None and self.concurrent_batches is False:
                depends_on = model_kwargs.get("depends_on", set())
                depends_on.add(self.canonical_name(context))

        model_kwargs["start"] = model_kwargs.get(
            "start", context.sqlmesh_config.model_defaults.start
        )

        model = create_sql_model(
            self.canonical_name(context),
            query,
            dialect=model_dialect,
            kind=kind,
            audit_definitions=audit_definitions,
            # This ensures that we bypass query rendering that would otherwise be required to extract additional
            # dependencies from the model's SQL.
            # Note: any table dependencies that are not referenced using the `ref` macro will not be included.
            extract_dependencies_from_query=False,
            allow_partials=allow_partials,
            virtual_environment_mode=virtual_environment_mode,
            dbt_node_info=self.node_info,
            **optional_kwargs,
            **model_kwargs,
        )
        return model

    def _dbt_max_partition_blob(self) -> t.Optional[str]:
        """Returns a SQL blob which declares the _dbt_max_partition variable. Only applicable to BigQuery."""
        if (
            not isinstance(self.partition_by, dict)
            or self.model_materialization != Materialization.INCREMENTAL
        ):
            return None

        from sqlmesh.core.engine_adapter.bigquery import select_partitions_expr

        data_type = self.partition_by["data_type"]
        select_max_partition_expr = select_partitions_expr(
            "{{ adapter.resolve_schema(this) }}",
            "{{ adapter.resolve_identifier(this) }}",
            data_type,
            granularity=self.partition_by.get("granularity"),
            catalog="{{ target.database }}",
        )

        data_type = data_type.upper()
        default_value = "NULL"
        if data_type in ("DATE", "DATETIME", "TIMESTAMP"):
            default_value = f"CAST('1970-01-01' AS {data_type})"

        return f"""
{{% if is_incremental() %}}
  DECLARE _dbt_max_partition {data_type} DEFAULT (
    COALESCE(({select_max_partition_expr}), {default_value})
  );
{{% endif %}}
"""
