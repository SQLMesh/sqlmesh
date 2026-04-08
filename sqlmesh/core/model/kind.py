from __future__ import annotations

import typing as t
from enum import Enum
from typing_extensions import Self

from pydantic import Field
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_columns import quote_identifiers
from sqlglot.optimizer.simplify import gen
from sqlglot.time import format_time

from sqlmesh.core import dialect as d
from sqlmesh.core.model.common import (
    parse_properties,
    properties_validator,
    validate_extra_and_required_fields,
)
from sqlmesh.core.model.seed import CsvSettings
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import (
    PydanticModel,
    SQLGlotBool,
    SQLGlotColumn,
    SQLGlotListOfFieldsOrStar,
    SQLGlotListOfFields,
    SQLGlotPositiveInt,
    SQLGlotString,
    SQLGlotCron,
    ValidationInfo,
    column_validator,
    field_validator,
    get_dialect,
    validate_string,
    validate_expression,
)


if t.TYPE_CHECKING:
    from sqlmesh.core._typing import CustomMaterializationProperties

    MODEL_KIND = t.TypeVar("MODEL_KIND", bound="_ModelKind")


class ModelKindMixin:
    @property
    def model_kind_name(self) -> t.Optional[ModelKindName]:
        """Returns the model kind name."""
        raise NotImplementedError

    @property
    def is_incremental_by_time_range(self) -> bool:
        return self.model_kind_name == ModelKindName.INCREMENTAL_BY_TIME_RANGE

    @property
    def is_incremental_by_unique_key(self) -> bool:
        return self.model_kind_name == ModelKindName.INCREMENTAL_BY_UNIQUE_KEY

    @property
    def is_incremental_by_partition(self) -> bool:
        return self.model_kind_name == ModelKindName.INCREMENTAL_BY_PARTITION

    @property
    def is_incremental_unmanaged(self) -> bool:
        return self.model_kind_name == ModelKindName.INCREMENTAL_UNMANAGED

    @property
    def is_incremental(self) -> bool:
        return (
            self.is_incremental_by_time_range
            or self.is_incremental_by_unique_key
            or self.is_incremental_by_partition
            or self.is_incremental_unmanaged
            or self.is_scd_type_2
        )

    @property
    def is_full(self) -> bool:
        return self.model_kind_name == ModelKindName.FULL

    @property
    def is_view(self) -> bool:
        return self.model_kind_name == ModelKindName.VIEW

    @property
    def is_embedded(self) -> bool:
        return self.model_kind_name == ModelKindName.EMBEDDED

    @property
    def is_seed(self) -> bool:
        return self.model_kind_name == ModelKindName.SEED

    @property
    def is_external(self) -> bool:
        return self.model_kind_name == ModelKindName.EXTERNAL

    @property
    def is_scd_type_2(self) -> bool:
        return self.model_kind_name in {
            ModelKindName.SCD_TYPE_2,
            ModelKindName.SCD_TYPE_2_BY_TIME,
            ModelKindName.SCD_TYPE_2_BY_COLUMN,
        }

    @property
    def is_scd_type_2_by_time(self) -> bool:
        return self.model_kind_name in {ModelKindName.SCD_TYPE_2, ModelKindName.SCD_TYPE_2_BY_TIME}

    @property
    def is_scd_type_2_by_column(self) -> bool:
        return self.model_kind_name == ModelKindName.SCD_TYPE_2_BY_COLUMN

    @property
    def is_custom(self) -> bool:
        return self.model_kind_name == ModelKindName.CUSTOM

    @property
    def is_managed(self) -> bool:
        return self.model_kind_name == ModelKindName.MANAGED

    @property
    def is_dbt_custom(self) -> bool:
        return self.model_kind_name == ModelKindName.DBT_CUSTOM

    @property
    def is_symbolic(self) -> bool:
        """A symbolic model is one that doesn't execute at all."""
        return self.model_kind_name in (ModelKindName.EMBEDDED, ModelKindName.EXTERNAL)

    @property
    def is_materialized(self) -> bool:
        return self.model_kind_name is not None and not (self.is_symbolic or self.is_view)

    @property
    def only_execution_time(self) -> bool:
        """Whether or not this model only cares about execution time to render."""
        return self.is_view or self.is_full

    @property
    def full_history_restatement_only(self) -> bool:
        """Whether or not this model only supports restatement of full history."""
        return (
            self.is_incremental_unmanaged
            or self.is_incremental_by_unique_key
            or self.is_incremental_by_partition
            or self.is_scd_type_2
            or self.is_managed
            or self.is_full
            or self.is_view
        )

    @property
    def supports_python_models(self) -> bool:
        return True

    @property
    def supports_grants(self) -> bool:
        """Whether this model kind supports grants configuration."""
        return self.is_materialized or self.is_view


class ModelKindName(str, ModelKindMixin, Enum):
    """The kind of model, determining how this data is computed and stored in the warehouse."""

    INCREMENTAL_BY_TIME_RANGE = "INCREMENTAL_BY_TIME_RANGE"
    INCREMENTAL_BY_UNIQUE_KEY = "INCREMENTAL_BY_UNIQUE_KEY"
    INCREMENTAL_BY_PARTITION = "INCREMENTAL_BY_PARTITION"
    INCREMENTAL_UNMANAGED = "INCREMENTAL_UNMANAGED"
    FULL = "FULL"
    # Legacy alias to SCD Type 2 By Time
    # Only used for Parsing and mapping name to SCD Type 2 By Time
    SCD_TYPE_2 = "SCD_TYPE_2"
    SCD_TYPE_2_BY_TIME = "SCD_TYPE_2_BY_TIME"
    SCD_TYPE_2_BY_COLUMN = "SCD_TYPE_2_BY_COLUMN"
    VIEW = "VIEW"
    EMBEDDED = "EMBEDDED"
    SEED = "SEED"
    EXTERNAL = "EXTERNAL"
    CUSTOM = "CUSTOM"
    MANAGED = "MANAGED"
    DBT_CUSTOM = "DBT_CUSTOM"

    @property
    def model_kind_name(self) -> t.Optional[ModelKindName]:
        return self

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return str(self)


class OnDestructiveChange(str, Enum):
    """What should happen when a forward-only model change requires a destructive schema change."""

    ERROR = "ERROR"
    WARN = "WARN"
    ALLOW = "ALLOW"
    IGNORE = "IGNORE"

    @property
    def is_error(self) -> bool:
        return self == OnDestructiveChange.ERROR

    @property
    def is_warn(self) -> bool:
        return self == OnDestructiveChange.WARN

    @property
    def is_allow(self) -> bool:
        return self == OnDestructiveChange.ALLOW

    @property
    def is_ignore(self) -> bool:
        return self == OnDestructiveChange.IGNORE


class OnAdditiveChange(str, Enum):
    """What should happen when a forward-only model change requires an additive schema change."""

    ERROR = "ERROR"
    WARN = "WARN"
    ALLOW = "ALLOW"
    IGNORE = "IGNORE"

    @property
    def is_error(self) -> bool:
        return self == OnAdditiveChange.ERROR

    @property
    def is_warn(self) -> bool:
        return self == OnAdditiveChange.WARN

    @property
    def is_allow(self) -> bool:
        return self == OnAdditiveChange.ALLOW

    @property
    def is_ignore(self) -> bool:
        return self == OnAdditiveChange.IGNORE


def _on_destructive_change_validator(
    cls: t.Type, v: t.Union[OnDestructiveChange, str, exp.Identifier]
) -> t.Any:
    if v and not isinstance(v, OnDestructiveChange):
        return OnDestructiveChange(
            v.this.upper() if isinstance(v, (exp.Identifier, exp.Literal)) else v.upper()
        )
    return v


def _on_additive_change_validator(
    cls: t.Type, v: t.Union[OnAdditiveChange, str, exp.Identifier]
) -> t.Any:
    if v and not isinstance(v, OnAdditiveChange):
        return OnAdditiveChange(
            v.this.upper() if isinstance(v, (exp.Identifier, exp.Literal)) else v.upper()
        )
    return v


on_additive_change_validator = field_validator("on_additive_change", mode="before")(
    _on_additive_change_validator
)

on_destructive_change_validator = field_validator("on_destructive_change", mode="before")(
    _on_destructive_change_validator
)


class _ModelKind(PydanticModel, ModelKindMixin):
    name: ModelKindName

    @property
    def model_kind_name(self) -> t.Optional[ModelKindName]:
        return self.name

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        kwargs["expressions"] = expressions
        return d.ModelKind(this=self.name.value.upper(), **kwargs)

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [self.name.value]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return []


class TimeColumn(PydanticModel):
    column: exp.Expr
    format: t.Optional[str] = None

    @classmethod
    def validator(cls) -> classmethod:
        def _time_column_validator(v: t.Any, info: ValidationInfo) -> TimeColumn:
            return TimeColumn.create(v, get_dialect(info.data))

        return field_validator("time_column", mode="before")(_time_column_validator)

    @field_validator("column", mode="before")
    @classmethod
    def _column_validator(cls, v: t.Union[str, exp.Expr]) -> exp.Expr:
        if not v:
            raise ConfigError("Time Column cannot be empty.")
        if isinstance(v, str):
            return exp.to_column(v)
        return v

    @property
    def expression(self) -> exp.Expr:
        """Convert this pydantic model into a time_column SQLGlot expression."""
        if not self.format:
            return self.column

        return exp.Tuple(expressions=[self.column, exp.Literal.string(self.format)])

    def to_expression(self, dialect: str) -> exp.Expr:
        """Convert this pydantic model into a time_column SQLGlot expression."""
        if not self.format:
            return self.column

        return exp.Tuple(
            expressions=[
                self.column,
                exp.Literal.string(
                    format_time(self.format, d.Dialect.get_or_raise(dialect).INVERSE_TIME_MAPPING)
                ),
            ]
        )

    def to_property(self, dialect: str = "") -> exp.Property:
        return exp.Property(this="time_column", value=self.to_expression(dialect))

    @classmethod
    def create(cls, v: t.Any, dialect: str) -> Self:
        if isinstance(v, exp.Tuple):
            column_expr = v.expressions[0]
            column = (
                exp.column(column_expr) if isinstance(column_expr, exp.Identifier) else column_expr
            )
            format = v.expressions[1].name if len(v.expressions) > 1 else None
        elif isinstance(v, exp.Expr):
            column = exp.column(v) if isinstance(v, exp.Identifier) else v
            format = None
        elif isinstance(v, str):
            column = d.parse_one(v, dialect=dialect)
            column.meta.pop("sql")
            format = None
        elif isinstance(v, dict):
            column_raw = v["column"]
            column = (
                d.parse_one(column_raw, dialect=dialect)
                if isinstance(column_raw, str)
                else column_raw
            )
            format = v.get("format")
        elif isinstance(v, TimeColumn):
            column = v.column
            format = v.format
        else:
            raise ConfigError(f"Invalid time_column: '{v}'.")

        column = quote_identifiers(normalize_identifiers(column, dialect=dialect), dialect=dialect)
        column.meta["dialect"] = dialect

        return cls(column=column, format=format)


def _kind_dialect_validator(cls: t.Type, v: t.Optional[str]) -> str:
    if v is None:
        return get_dialect({})
    return v


kind_dialect_validator = field_validator("dialect", mode="before")(_kind_dialect_validator)


class _Incremental(_ModelKind):
    on_destructive_change: OnDestructiveChange = OnDestructiveChange.ERROR
    on_additive_change: OnAdditiveChange = OnAdditiveChange.ALLOW
    auto_restatement_cron: t.Optional[SQLGlotCron] = None

    _on_destructive_change_validator = on_destructive_change_validator
    _on_additive_change_validator = on_additive_change_validator

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.on_destructive_change),
            str(self.on_additive_change),
            self.auto_restatement_cron,
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "on_destructive_change": self.on_destructive_change.value,
                        "on_additive_change": self.on_additive_change.value,
                        "auto_restatement_cron": self.auto_restatement_cron,
                    }
                ),
            ],
        )


class _IncrementalBy(_Incremental):
    dialect: t.Optional[str] = Field(None, validate_default=True)
    batch_size: t.Optional[SQLGlotPositiveInt] = None
    batch_concurrency: t.Optional[SQLGlotPositiveInt] = None
    lookback: t.Optional[SQLGlotPositiveInt] = None
    forward_only: SQLGlotBool = False
    disable_restatement: SQLGlotBool = False

    _dialect_validator = kind_dialect_validator

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            self.dialect,
            str(self.lookback) if self.lookback is not None else None,
        ]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.batch_size) if self.batch_size is not None else None,
            str(self.forward_only),
            str(self.disable_restatement),
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "batch_size": self.batch_size,
                        "batch_concurrency": self.batch_concurrency,
                        "lookback": self.lookback,
                        "forward_only": self.forward_only,
                        "disable_restatement": self.disable_restatement,
                    }
                ),
            ],
        )


class IncrementalByTimeRangeKind(_IncrementalBy):
    name: t.Literal[ModelKindName.INCREMENTAL_BY_TIME_RANGE] = (
        ModelKindName.INCREMENTAL_BY_TIME_RANGE
    )
    time_column: TimeColumn
    auto_restatement_intervals: t.Optional[SQLGlotPositiveInt] = None
    partition_by_time_column: SQLGlotBool = True

    _time_column_validator = TimeColumn.validator()

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                self.time_column.to_property(kwargs.get("dialect") or ""),
                *_properties(
                    {
                        "partition_by_time_column": self.partition_by_time_column,
                    }
                ),
                *(
                    [_property("auto_restatement_intervals", self.auto_restatement_intervals)]
                    if self.auto_restatement_intervals is not None
                    else []
                ),
            ]
        )

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().data_hash_values, gen(self.time_column.column), self.time_column.format]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.partition_by_time_column),
            str(self.auto_restatement_intervals)
            if self.auto_restatement_intervals is not None
            else None,
        ]


class IncrementalByUniqueKeyKind(_IncrementalBy):
    name: t.Literal[ModelKindName.INCREMENTAL_BY_UNIQUE_KEY] = (
        ModelKindName.INCREMENTAL_BY_UNIQUE_KEY
    )
    unique_key: SQLGlotListOfFields
    when_matched: t.Optional[exp.Whens] = None
    merge_filter: t.Optional[exp.Expr] = None
    batch_concurrency: t.Literal[1] = 1

    @field_validator("when_matched", mode="before")
    def _when_matched_validator(
        cls,
        v: t.Optional[t.Union[str, list, exp.Whens]],
        info: ValidationInfo,
    ) -> t.Optional[exp.Whens]:
        if v is None:
            return v
        if isinstance(v, list):
            v = " ".join(v)

        dialect = get_dialect(info.data)

        if isinstance(v, str):
            # Whens wrap the WHEN clauses, but the parentheses aren't parsed by sqlglot
            v = v.strip()
            if v.startswith("("):
                v = v[1:-1]

            v = t.cast(exp.Whens, d.parse_one(v, into=exp.Whens, dialect=dialect))

        v = validate_expression(v, dialect=dialect)
        return t.cast(exp.Whens, v.transform(d.replace_merge_table_aliases, dialect=dialect))

    @field_validator("merge_filter", mode="before")
    def _merge_filter_validator(
        cls,
        v: t.Optional[exp.Expr],
        info: ValidationInfo,
    ) -> t.Optional[exp.Expr]:
        if v is None:
            return v

        dialect = get_dialect(info.data)

        if isinstance(v, str):
            v = v.strip()
            v = d.parse_one(v, dialect=dialect)

        v = validate_expression(v, dialect=dialect)
        return v.transform(d.replace_merge_table_aliases, dialect=dialect)

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            *(gen(k) for k in self.unique_key),
            gen(self.when_matched) if self.when_matched is not None else None,
            gen(self.merge_filter) if self.merge_filter is not None else None,
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "unique_key": exp.Tuple(expressions=self.unique_key),
                        "when_matched": self.when_matched,
                        "merge_filter": self.merge_filter,
                    }
                ),
            ],
        )


class IncrementalByPartitionKind(_Incremental):
    name: t.Literal[ModelKindName.INCREMENTAL_BY_PARTITION] = ModelKindName.INCREMENTAL_BY_PARTITION
    forward_only: t.Literal[True] = True
    disable_restatement: SQLGlotBool = False

    @field_validator("forward_only", mode="before")
    def _forward_only_validator(cls, v: t.Union[bool, exp.Expr]) -> t.Literal[True]:
        if v is not True:
            raise ConfigError(
                "Do not specify the `forward_only` configuration key - INCREMENTAL_BY_PARTITION models are always forward_only."
            )
        return v

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.forward_only),
            str(self.disable_restatement),
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "forward_only": self.forward_only,
                        "disable_restatement": self.disable_restatement,
                    }
                ),
            ],
        )


class IncrementalUnmanagedKind(_Incremental):
    name: t.Literal[ModelKindName.INCREMENTAL_UNMANAGED] = ModelKindName.INCREMENTAL_UNMANAGED
    insert_overwrite: SQLGlotBool = False
    forward_only: SQLGlotBool = True
    disable_restatement: SQLGlotBool = True

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().data_hash_values, str(self.insert_overwrite)]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.forward_only),
            str(self.disable_restatement),
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "insert_overwrite": self.insert_overwrite,
                        "forward_only": self.forward_only,
                        "disable_restatement": self.disable_restatement,
                    }
                ),
            ],
        )


class ViewKind(_ModelKind):
    name: t.Literal[ModelKindName.VIEW] = ModelKindName.VIEW
    materialized: SQLGlotBool = False

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().data_hash_values, str(self.materialized)]

    @property
    def supports_python_models(self) -> bool:
        return False

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                _property("materialized", self.materialized),
            ],
        )


class SeedKind(_ModelKind):
    name: t.Literal[ModelKindName.SEED] = ModelKindName.SEED
    path: SQLGlotString
    batch_size: SQLGlotPositiveInt = 1000
    csv_settings: t.Optional[CsvSettings] = None

    @field_validator("csv_settings", mode="before")
    @classmethod
    def _parse_csv_settings(cls, v: t.Any) -> t.Optional[CsvSettings]:
        if v is None or isinstance(v, CsvSettings):
            return v
        if isinstance(v, exp.Expr):
            tuple_exp = parse_properties(cls, v, None)
            if not tuple_exp:
                return None
            return CsvSettings(**{e.left.name: e.right for e in tuple_exp.expressions})
        if isinstance(v, dict):
            return CsvSettings(**v)
        return v

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        """Convert the seed kind into a SQLGlot expression."""
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "path": exp.Literal.string(self.path),
                        "batch_size": self.batch_size,
                    }
                ),
            ],
        )

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        csv_setting_values = (self.csv_settings or CsvSettings()).dict().values()
        return [
            *super().data_hash_values,
            *(v if isinstance(v, (str, type(None))) else str(v) for v in csv_setting_values),
        ]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [*super().metadata_hash_values, str(self.batch_size)]

    @property
    def supports_python_models(self) -> bool:
        return False


class FullKind(_ModelKind):
    name: t.Literal[ModelKindName.FULL] = ModelKindName.FULL


class _SCDType2Kind(_Incremental):
    dialect: t.Optional[str] = Field(None, validate_default=True)
    unique_key: SQLGlotListOfFields
    valid_from_name: SQLGlotColumn = Field(exp.column("valid_from"), validate_default=True)
    valid_to_name: SQLGlotColumn = Field(exp.column("valid_to"), validate_default=True)
    invalidate_hard_deletes: SQLGlotBool = False
    time_data_type: exp.DataType = Field(exp.DataType.build("TIMESTAMP"), validate_default=True)
    batch_size: t.Optional[SQLGlotPositiveInt] = None

    forward_only: SQLGlotBool = True
    disable_restatement: SQLGlotBool = True

    _dialect_validator = kind_dialect_validator

    _always_validate_column = field_validator("valid_from_name", "valid_to_name", mode="before")(
        column_validator
    )

    @field_validator("time_data_type", mode="before")
    @classmethod
    def _time_data_type_validator(cls, v: t.Union[str, exp.Expr], values: t.Any) -> exp.Expr:
        if isinstance(v, exp.Expr) and not isinstance(v, exp.DataType):
            v = v.name
        dialect = get_dialect(values)
        data_type = exp.DataType.build(v, dialect=dialect)
        # Clear meta["sql"] (set by our parser extension) so the pydantic encoder
        # uses dialect-aware rendering: e.sql(dialect=meta["dialect"]). Without this,
        # the raw SQL text takes priority, which can be wrong for dialect-normalized
        # types (e.g., default "TIMESTAMP" should render as "DATETIME" in BigQuery).
        data_type.meta.pop("sql", None)
        data_type.meta["dialect"] = dialect
        return data_type

    @property
    def managed_columns(self) -> t.Dict[str, exp.DataType]:
        return {
            self.valid_from_name.name: self.time_data_type,
            self.valid_to_name.name: self.time_data_type,
        }

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            self.dialect,
            *(gen(k) for k in self.unique_key),
            gen(self.valid_from_name),
            gen(self.valid_to_name),
            str(self.invalidate_hard_deletes),
            self.time_data_type.sql(self.dialect),
            str(self.batch_size) if self.batch_size is not None else None,
        ]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.forward_only),
            str(self.disable_restatement),
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "unique_key": exp.Tuple(expressions=self.unique_key),
                        "valid_from_name": self.valid_from_name,
                        "valid_to_name": self.valid_to_name,
                        "invalidate_hard_deletes": self.invalidate_hard_deletes,
                        "time_data_type": self.time_data_type,
                        "forward_only": self.forward_only,
                        "disable_restatement": self.disable_restatement,
                    }
                ),
            ],
        )


class SCDType2ByTimeKind(_SCDType2Kind):
    name: t.Literal[ModelKindName.SCD_TYPE_2, ModelKindName.SCD_TYPE_2_BY_TIME] = (
        ModelKindName.SCD_TYPE_2_BY_TIME
    )
    updated_at_name: SQLGlotColumn = Field(exp.column("updated_at"), validate_default=True)
    updated_at_as_valid_from: SQLGlotBool = False

    _always_validate_updated_at = field_validator("updated_at_name", mode="before")(
        column_validator
    )

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            gen(self.updated_at_name),
            str(self.updated_at_as_valid_from),
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "updated_at_name": self.updated_at_name,
                        "updated_at_as_valid_from": self.updated_at_as_valid_from,
                    }
                ),
            ],
        )


class SCDType2ByColumnKind(_SCDType2Kind):
    name: t.Literal[ModelKindName.SCD_TYPE_2_BY_COLUMN] = ModelKindName.SCD_TYPE_2_BY_COLUMN
    columns: SQLGlotListOfFieldsOrStar
    execution_time_as_valid_from: SQLGlotBool = False
    updated_at_name: t.Optional[SQLGlotColumn] = None

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        columns_sql = (
            [gen(c) for c in self.columns]
            if isinstance(self.columns, list)
            else [gen(self.columns)]
        )
        return [
            *super().data_hash_values,
            *columns_sql,
            str(self.execution_time_as_valid_from),
            gen(self.updated_at_name) if self.updated_at_name is not None else None,
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "columns": exp.Tuple(expressions=self.columns)
                        if isinstance(self.columns, list)
                        else self.columns,
                        "execution_time_as_valid_from": self.execution_time_as_valid_from,
                    }
                ),
            ],
        )


class ManagedKind(_ModelKind):
    name: t.Literal[ModelKindName.MANAGED] = ModelKindName.MANAGED
    disable_restatement: t.Literal[True] = True

    @property
    def supports_python_models(self) -> bool:
        return False


class DbtCustomKind(_ModelKind):
    name: t.Literal[ModelKindName.DBT_CUSTOM] = ModelKindName.DBT_CUSTOM
    materialization: str
    adapter: str = "default"
    definition: str
    dialect: t.Optional[str] = Field(None, validate_default=True)

    _dialect_validator = kind_dialect_validator

    @field_validator("materialization", "adapter", "definition", mode="before")
    @classmethod
    def _validate_fields(cls, v: t.Any) -> str:
        return validate_string(v)

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            self.materialization,
            self.definition,
            self.adapter,
            self.dialect,
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "materialization": exp.Literal.string(self.materialization),
                        "adapter": exp.Literal.string(self.adapter),
                    }
                ),
            ],
        )


class EmbeddedKind(_ModelKind):
    name: t.Literal[ModelKindName.EMBEDDED] = ModelKindName.EMBEDDED

    @property
    def supports_python_models(self) -> bool:
        return False


class ExternalKind(_ModelKind):
    name: t.Literal[ModelKindName.EXTERNAL] = ModelKindName.EXTERNAL


class CustomKind(_ModelKind):
    name: t.Literal[ModelKindName.CUSTOM] = ModelKindName.CUSTOM
    materialization: str
    materialization_properties_: t.Optional[exp.Tuple] = Field(
        default=None, alias="materialization_properties"
    )
    forward_only: SQLGlotBool = False
    disable_restatement: SQLGlotBool = False
    batch_size: t.Optional[SQLGlotPositiveInt] = None
    batch_concurrency: t.Optional[SQLGlotPositiveInt] = None
    lookback: t.Optional[SQLGlotPositiveInt] = None
    auto_restatement_cron: t.Optional[SQLGlotCron] = None
    auto_restatement_intervals: t.Optional[SQLGlotPositiveInt] = None

    # so that CustomKind subclasses know the dialect when validating / normalizing / interpreting values in `materialization_properties`
    dialect: str = Field(exclude=True)

    _properties_validator = properties_validator

    @field_validator("materialization", mode="before")
    @classmethod
    def _validate_materialization(cls, v: t.Any) -> str:
        # note: create_model_kind() validates the custom materialization class
        return validate_string(v)

    @property
    def materialization_properties(self) -> CustomMaterializationProperties:
        """A dictionary of materialization properties."""
        if not self.materialization_properties_:
            return {}
        return d.interpret_key_value_pairs(self.materialization_properties_)

    @property
    def data_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().data_hash_values,
            self.materialization,
            gen(self.materialization_properties_) if self.materialization_properties_ else None,
            str(self.lookback) if self.lookback is not None else None,
        ]

    @property
    def metadata_hash_values(self) -> t.List[t.Optional[str]]:
        return [
            *super().metadata_hash_values,
            str(self.batch_size) if self.batch_size is not None else None,
            str(self.batch_concurrency) if self.batch_concurrency is not None else None,
            str(self.forward_only),
            str(self.disable_restatement),
            self.auto_restatement_cron,
            str(self.auto_restatement_intervals)
            if self.auto_restatement_intervals is not None
            else None,
        ]

    def to_expression(
        self, expressions: t.Optional[t.List[exp.Expr]] = None, **kwargs: t.Any
    ) -> d.ModelKind:
        return super().to_expression(
            expressions=[
                *(expressions or []),
                *_properties(
                    {
                        "materialization": exp.Literal.string(self.materialization),
                        "materialization_properties": self.materialization_properties_,
                        "forward_only": self.forward_only,
                        "disable_restatement": self.disable_restatement,
                        "batch_size": self.batch_size,
                        "batch_concurrency": self.batch_concurrency,
                        "lookback": self.lookback,
                        "auto_restatement_cron": self.auto_restatement_cron,
                        "auto_restatement_intervals": self.auto_restatement_intervals,
                    }
                ),
            ],
        )


ModelKind = t.Annotated[
    t.Union[
        EmbeddedKind,
        ExternalKind,
        FullKind,
        IncrementalByTimeRangeKind,
        IncrementalByUniqueKeyKind,
        IncrementalByPartitionKind,
        IncrementalUnmanagedKind,
        SeedKind,
        ViewKind,
        SCDType2ByTimeKind,
        SCDType2ByColumnKind,
        CustomKind,
        ManagedKind,
        DbtCustomKind,
    ],
    Field(discriminator="name"),
]

MODEL_KIND_NAME_TO_TYPE: t.Dict[str, t.Type[ModelKind]] = {
    ModelKindName.EMBEDDED: EmbeddedKind,
    ModelKindName.EXTERNAL: ExternalKind,
    ModelKindName.FULL: FullKind,
    ModelKindName.INCREMENTAL_BY_TIME_RANGE: IncrementalByTimeRangeKind,
    ModelKindName.INCREMENTAL_BY_UNIQUE_KEY: IncrementalByUniqueKeyKind,
    ModelKindName.INCREMENTAL_BY_PARTITION: IncrementalByPartitionKind,
    ModelKindName.INCREMENTAL_UNMANAGED: IncrementalUnmanagedKind,
    ModelKindName.SEED: SeedKind,
    ModelKindName.VIEW: ViewKind,
    ModelKindName.SCD_TYPE_2: SCDType2ByTimeKind,
    ModelKindName.SCD_TYPE_2_BY_TIME: SCDType2ByTimeKind,
    ModelKindName.SCD_TYPE_2_BY_COLUMN: SCDType2ByColumnKind,
    ModelKindName.CUSTOM: CustomKind,
    ModelKindName.MANAGED: ManagedKind,
    ModelKindName.DBT_CUSTOM: DbtCustomKind,
}


def model_kind_type_from_name(name: t.Optional[str]) -> t.Type[ModelKind]:
    klass = MODEL_KIND_NAME_TO_TYPE.get(name) if name else None
    if not klass:
        raise ConfigError(f"Invalid model kind '{name}'")
    return t.cast(t.Type[ModelKind], klass)


def create_model_kind(v: t.Any, dialect: str, defaults: t.Dict[str, t.Any]) -> ModelKind:
    if isinstance(v, _ModelKind):
        return t.cast(ModelKind, v)

    if isinstance(v, (d.ModelKind, dict)):
        props = (
            {prop.name: prop.args.get("value") for prop in v.expressions}
            if isinstance(v, d.ModelKind)
            else v
        )
        name = v.this if isinstance(v, d.ModelKind) else props.get("name")

        # We want to ensure whatever name is provided to construct the class is the same name that will be
        # found inside the class itself in order to avoid a change during plan/apply for legacy aliases.
        # Ex: Pass in `SCD_TYPE_2` then we want to ensure we get `SCD_TYPE_2` as the kind name
        # instead of `SCD_TYPE_2_BY_TIME`.
        props["name"] = name
        kind_type = model_kind_type_from_name(name)

        if "dialect" in kind_type.all_fields() and props.get("dialect") is None:
            props["dialect"] = dialect

        # only pass the on_destructive_change or on_additive_change user default to models inheriting from _Incremental
        # that don't explicitly set it in the model definition
        if issubclass(kind_type, _Incremental):
            for on_change_property in ("on_additive_change", "on_destructive_change"):
                if (
                    props.get(on_change_property) is None
                    and defaults.get(on_change_property) is not None
                ):
                    props[on_change_property] = defaults.get(on_change_property)

        # only pass the batch_concurrency user default to models inheriting from _IncrementalBy
        # that don't explicitly set it in the model definition, but ignore subclasses of _IncrementalBy
        # that hardcode a specific batch_concurrency
        if issubclass(kind_type, _IncrementalBy):
            BATCH_CONCURRENCY: t.Final = "batch_concurrency"
            if (
                props.get(BATCH_CONCURRENCY) is None
                and defaults.get(BATCH_CONCURRENCY) is not None
                and kind_type.all_field_infos()[BATCH_CONCURRENCY].default is None
            ):
                props[BATCH_CONCURRENCY] = defaults.get(BATCH_CONCURRENCY)

        if kind_type == CustomKind:
            # load the custom materialization class and check if it uses a custom kind type
            from sqlmesh.core.snapshot.evaluator import get_custom_materialization_type

            if "materialization" not in props:
                raise ConfigError(
                    "The 'materialization' property is required for models of the CUSTOM kind"
                )

            # The below call will print a warning if a materialization with the given name doesn't exist
            # we dont want to throw an error here because we still want Models with a CustomKind to be able
            # to be serialized / deserialized in contexts where the custom materialization class may not be available,
            # such as in HTTP request handlers
            custom_materialization = get_custom_materialization_type(
                validate_string(props.get("materialization")), raise_errors=False
            )
            if custom_materialization is not None:
                actual_kind_type, _ = custom_materialization
                return actual_kind_type(**props)

        validate_extra_and_required_fields(
            kind_type, set(props), f"MODEL block 'kind {name}' field"
        )
        return kind_type(**props)

    name = (v.name if isinstance(v, exp.Expr) else str(v)).upper()
    return model_kind_type_from_name(name)(name=name)  # type: ignore


def _model_kind_validator(cls: t.Type, v: t.Any, info: t.Optional[ValidationInfo]) -> ModelKind:
    dialect = get_dialect(info.data) if info else ""
    return create_model_kind(v, dialect, {})


model_kind_validator: t.Callable = field_validator("kind", mode="before")(_model_kind_validator)


def _property(name: str, value: t.Any) -> exp.Property:
    return exp.Property(this=exp.var(name), value=exp.convert(value))


def _properties(name_value_pairs: t.Dict[str, t.Any]) -> t.List[exp.Property]:
    return [_property(k, v) for k, v in name_value_pairs.items() if v is not None]
