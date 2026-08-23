from __future__ import annotations

import typing as t
import zoneinfo
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import Field
from sqlglot import exp

from sqlmesh.utils.cron import CroniterCache
from sqlmesh.utils.date import TimeLike, to_datetime, validate_date_range
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import (
    PydanticModel,
    SQLGlotCron,
    field_validator,
    model_validator,
    PRIVATE_FIELDS,
)

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import Self
    from sqlmesh.core.snapshot import Node


class IntervalUnit(str, Enum):
    """IntervalUnit is the inferred granularity of an incremental node.

    IntervalUnit can be one of 5 types, YEAR, MONTH, DAY, HOUR, MINUTE. The unit is inferred
    based on the cron schedule of a node. The minimum time delta between a sample set of dates
    is used to determine which unit a node's schedule is.

    It's designed to align with common partitioning schemes, hence why there is no WEEK unit
    because generally tables are not partitioned by week
    """

    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    HALF_HOUR = "half_hour"
    QUARTER_HOUR = "quarter_hour"
    FIVE_MINUTE = "five_minute"

    @classmethod
    def from_cron(klass, cron: str) -> IntervalUnit:
        croniter = CroniterCache(cron)
        interval_seconds = croniter.interval_seconds

        if not interval_seconds:
            samples = [croniter.get_next() for _ in range(5)]
            interval_seconds = int(min(b - a for a, b in zip(samples, samples[1:])).total_seconds())

        for unit, seconds in INTERVAL_SECONDS.items():
            if seconds <= interval_seconds:
                return unit
        raise ConfigError(f"Invalid cron '{cron}': must run at a frequency of 5 minutes or slower.")

    @property
    def is_date_granularity(self) -> bool:
        return self in (IntervalUnit.YEAR, IntervalUnit.MONTH, IntervalUnit.DAY)

    @property
    def is_year(self) -> bool:
        return self == IntervalUnit.YEAR

    @property
    def is_month(self) -> bool:
        return self == IntervalUnit.MONTH

    @property
    def is_day(self) -> bool:
        return self == IntervalUnit.DAY

    @property
    def is_hour(self) -> bool:
        return self == IntervalUnit.HOUR

    @property
    def is_minute(self) -> bool:
        return self in (IntervalUnit.FIVE_MINUTE, IntervalUnit.QUARTER_HOUR, IntervalUnit.HALF_HOUR)

    @property
    def cron_expr(self) -> str:
        if self == IntervalUnit.FIVE_MINUTE:
            return "*/5 * * * *"
        if self == IntervalUnit.QUARTER_HOUR:
            return "*/15 * * * *"
        if self == IntervalUnit.HALF_HOUR:
            return "*/30 * * * *"
        if self == IntervalUnit.HOUR:
            return "0 * * * *"
        if self == IntervalUnit.DAY:
            return "0 0 * * *"
        if self == IntervalUnit.MONTH:
            return "0 0 1 * *"
        if self == IntervalUnit.YEAR:
            return "0 0 1 1 *"
        return ""

    def croniter(self, value: TimeLike) -> CroniterCache:
        return CroniterCache(self.cron_expr, value)

    def cron_next(self, value: TimeLike, estimate: bool = False) -> datetime:
        """
        Get the next timestamp given a time-like value for this interval unit.

        Args:
            value: A variety of date formats.
            estimate: Whether or not to estimate, only use this if the value is floored.

        Returns:
            The timestamp for the next run.
        """
        return self.croniter(value).get_next(estimate=estimate)

    def cron_prev(self, value: TimeLike, estimate: bool = False) -> datetime:
        """
        Get the previous timestamp given a time-like value for this interval unit.

        Args:
            value: A variety of date formats.
            estimate: Whether or not to estimate, only use this if the value is floored.

        Returns:
            The timestamp for the previous run.
        """
        return self.croniter(value).get_prev(estimate=estimate)

    def cron_floor(self, value: TimeLike, estimate: bool = False) -> datetime:
        """
        Get the floor timestamp given a time-like value for this interval unit.

        Args:
            value: A variety of date formats.
            estimate: Whether or not to estimate, only use this if the value is floored.

        Returns:
            The timestamp floor.
        """
        croniter = self.croniter(value)
        croniter.get_next(estimate=estimate)
        return croniter.get_prev(estimate=True)

    @property
    def seconds(self) -> int:
        return INTERVAL_SECONDS[self]

    @property
    def milliseconds(self) -> int:
        return self.seconds * 1000


class DbtNodeInfo(PydanticModel):
    """
    Represents dbt-specific model information set by the dbt loader and intended to be made available at the Snapshot level
    (as opposed to hidden within the individual model jinja macro registries).

    This allows for things like injecting implementations of variables / functions into the Jinja context that are compatible with
    their dbt equivalents but are backed by the sqlmesh snapshots in any given plan / environment
    """

    unique_id: str
    """This is the node/resource name/unique_id that's used as the node key in the dbt manifest.
    It's prefixed by the resource type and is exposed in context variables like {{ selected_resources }}.

    Examples:
        - test.jaffle_shop.unique_stg_orders_order_id.e3b841c71a
        - seed.jaffle_shop.raw_payments
        - model.jaffle_shop.stg_orders
    """

    name: str
    """Name of this object in the dbt global namespace, used by things like {{ ref() }} calls.    
    
    Examples:
        - unique_stg_orders_order_id
        - raw_payments
        - stg_orders
    """

    fqn: str
    """Used for selectors in --select/--exclude.
    Takes the filesystem into account so may be structured differently to :unique_id.
    
    Examples:
        - jaffle_shop.staging.unique_stg_orders_order_id
        - jaffle_shop.raw_payments
        - jaffle_shop.staging.stg_orders
    """

    alias: t.Optional[str] = None
    """This is dbt's way of overriding the _physical table_ a model is written to.

    It's used in the following situation:
     - Say you have two models, "stg_customers" and "customers"
     - You want "stg_customers" to be written to the "staging" schema as eg "staging.customers" - NOT "staging.stg_customers"
     - But you cant rename the file to "customers" because it will conflict with your other model file "customers"
     - Even if you put it in a different folder, eg "staging/customers.sql" - dbt still has a global namespace so it will conflict
        when you try to do something like "{{ ref('customers') }}"
     - So dbt's solution to this problem is to keep calling it "stg_customers" at the dbt project/model level,
        but allow overriding the physical table to "customers" via something like "{{ config(alias='customers', schema='staging') }}"

    Note that if :alias is set, it does *not* replace :name at the model level and cannot be used interchangably with :name.
    It also does not affect the :fqn or :unique_id. It's just used to override :name when it comes time to generate the physical table name.
    """

    @model_validator(mode="after")
    def post_init(self) -> Self:
        # by default, dbt sets alias to the same as :name
        # however, we only want to include :alias if it is actually different / actually providing an override
        if self.alias == self.name:
            self.alias = None
        return self

    def to_expression(self) -> exp.Expr:
        """Produce a SQLGlot expression representing this object, for use in things like the model/audit definition renderers"""
        return exp.tuple_(
            *(
                exp.PropertyEQ(this=exp.var(k), expression=exp.Literal.string(v))
                for k, v in sorted(self.model_dump(exclude_none=True).items())
            )
        )


class DbtInfoMixin:
    """This mixin encapsulates properties that only exist for dbt compatibility and are otherwise not required
    for native projects"""

    @property
    def dbt_node_info(self) -> t.Optional[DbtNodeInfo]:
        raise NotImplementedError()

    @property
    def dbt_unique_id(self) -> t.Optional[str]:
        """Used for compatibility with jinja context variables such as {{ selected_resources }}"""
        if self.dbt_node_info:
            return self.dbt_node_info.unique_id
        return None

    @property
    def dbt_fqn(self) -> t.Optional[str]:
        """Used in the selector engine for compatibility with selectors that select models by dbt fqn"""
        if self.dbt_node_info:
            return self.dbt_node_info.fqn
        return None


# this must be sorted in descending order
INTERVAL_SECONDS = {
    IntervalUnit.YEAR: 60 * 60 * 24 * 365,
    IntervalUnit.MONTH: 60 * 60 * 24 * 28,
    IntervalUnit.DAY: 60 * 60 * 24,
    IntervalUnit.HOUR: 60 * 60,
    IntervalUnit.HALF_HOUR: 60 * 30,
    IntervalUnit.QUARTER_HOUR: 60 * 15,
    IntervalUnit.FIVE_MINUTE: 60 * 5,
}


def _cron_tz_validator(cls: t.Type, v: t.Any) -> t.Optional[zoneinfo.ZoneInfo]:
    if not v or v == "UTC":
        return None

    v = str_or_exp_to_str(v)

    try:
        return zoneinfo.ZoneInfo(v)
    except Exception as e:
        available_timezones = zoneinfo.available_timezones()

        if available_timezones:
            raise ConfigError(f"{e}. {v} must be in {available_timezones}.")
        else:
            raise ConfigError(
                f"{e}. IANA time zone data is not available on your system. `pip install tzdata` to leverage cron time zones or remove this field which will default to UTC."
            )

    return None


cron_tz_validator = field_validator("cron_tz", mode="before")(_cron_tz_validator)


class _Node(DbtInfoMixin, PydanticModel):
    """
    Node is the core abstraction for entity that can be executed within the scheduler.

    Args:
        name: The name of the node.
        project: The name of the project this node belongs to, used in multi-repo deployments.
        description: The optional node description.
        owner: The owner of the node.
        start: The earliest date that the node will be executed for. If this is None,
            then the date is inferred by taking the most recent start date of its ancestors.
            The start date can be a static datetime or a relative datetime like "1 year ago"
        end: The latest date that the model will be executed for. If this is None,
            the date from the scheduler will be used
        cron: A cron string specifying how often the node should be run, leveraging the
            [croniter](https://github.com/kiorky/croniter) library.
        cron_tz: Time zone for the cron, defaults to utc, [IANA time zones](https://docs.python.org/3/library/zoneinfo.html).
        interval_unit: The duration of an interval for the node. By default, it is computed from the cron expression.
        tags: A list of tags that can be used to filter nodes.
        stamp: An optional arbitrary string sequence used to create new node versions without making
            changes to any of the functional components of the definition.
    """

    name: str
    project: str = ""
    description: t.Optional[str] = None
    owner: t.Optional[str] = None
    start: t.Optional[TimeLike] = None
    end: t.Optional[TimeLike] = None
    cron: SQLGlotCron = "@daily"
    cron_tz: t.Optional[zoneinfo.ZoneInfo] = None
    interval_unit_: t.Optional[IntervalUnit] = Field(alias="interval_unit", default=None)
    tags: t.List[str] = []
    stamp: t.Optional[str] = None
    dbt_node_info_: t.Optional[DbtNodeInfo] = Field(alias="dbt_node_info", default=None)
    _path: t.Optional[Path] = None
    _data_hash: t.Optional[str] = None
    _metadata_hash: t.Optional[str] = None

    _croniter: t.Optional[CroniterCache] = None
    __inferred_interval_unit: t.Optional[IntervalUnit] = None

    _cron_tz_validator = cron_tz_validator

    def __str__(self) -> str:
        path = f": {self._path.name}" if self._path else ""
        return f"{self.__class__.__name__}<{self.name}{path}>"

    def __getstate__(self) -> t.Dict[t.Any, t.Any]:
        state = super().__getstate__()
        private = state[PRIVATE_FIELDS]
        private["_data_hash"] = None
        private["_metadata_hash"] = None
        return state

    def copy(self, **kwargs: t.Any) -> Self:
        node = super().copy(**kwargs)
        node._data_hash = None
        node._metadata_hash = None
        return node

    @field_validator("name", mode="before")
    @classmethod
    def _name_validator(cls, v: t.Any) -> t.Optional[str]:
        if v is None:
            return None
        if isinstance(v, exp.Expr):
            return v.meta["sql"]
        return str(v)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _date_validator(cls, v: t.Any) -> t.Optional[TimeLike]:
        if isinstance(v, exp.Expr):
            v = v.name
        if v and not to_datetime(v):
            raise ConfigError(f"'{v}' needs to be time-like: https://pypi.org/project/dateparser")
        return v

    @field_validator("owner", "description", "stamp", mode="before")
    @classmethod
    def _string_expr_validator(cls, v: t.Any) -> t.Optional[str]:
        return str_or_exp_to_str(v)

    @field_validator("interval_unit_", mode="before")
    @classmethod
    def _interval_unit_validator(cls, v: t.Any) -> t.Optional[t.Union[IntervalUnit, str]]:
        if isinstance(v, IntervalUnit):
            return v
        v = str_or_exp_to_str(v)
        if v:
            v = v.lower()
        return v

    @model_validator(mode="after")
    def _node_root_validator(self) -> Self:
        interval_unit = self.interval_unit_
        if interval_unit and not getattr(self, "allow_partials", None):
            cron = self.cron
            max_interval_unit = IntervalUnit.from_cron(cron)
            if interval_unit.seconds > max_interval_unit.seconds:
                raise ConfigError(
                    f"Cron '{cron}' cannot be more frequent than interval unit '{interval_unit.value}'. "
                    "If this is intentional, set allow_partials to True."
                )

        start = self.start
        end = self.end

        if end is not None and start is None:
            raise ConfigError("Must define a start date if an end date is defined.")
        validate_date_range(start, end)
        return self

    @property
    def batch_size(self) -> t.Optional[int]:
        """The maximal number of units in a single task for a backfill."""
        return None

    @property
    def batch_concurrency(self) -> t.Optional[int]:
        """The maximal number of batches that can run concurrently for a backfill."""
        return None

    @property
    def interval_unit(self) -> IntervalUnit:
        """Returns the interval unit using which data intervals are computed for this node."""
        if self.interval_unit_ is not None:
            return self.interval_unit_
        return self._inferred_interval_unit()

    @property
    def depends_on(self) -> t.Set[str]:
        return set()

    @property
    def fqn(self) -> str:
        return self.name

    @property
    def data_hash(self) -> str:
        """
        Computes the data hash for the node.

        Returns:
            The data hash for the node.
        """
        raise NotImplementedError

    @property
    def metadata_hash(self) -> str:
        """
        Computes the metadata hash for the node.

        Returns:
            The metadata hash for the node.
        """
        raise NotImplementedError

    def is_metadata_only_change(self, previous: _Node) -> bool:
        """Determines if this node is a metadata only change in relation to the `previous` node.

        Args:
            previous: The previous node to compare against.

        Returns:
            True if this node is a metadata only change, False otherwise.
        """
        return self.data_hash == previous.data_hash and self.metadata_hash != previous.metadata_hash

    def is_data_change(self, previous: _Node) -> bool:
        """Determines if this node is a data change in relation to the `previous` node.

        Args:
            previous: The previous node to compare against.

        Returns:
            True if this node is a data change, False otherwise.
        """
        return (
            self.data_hash != previous.data_hash or self.metadata_hash != previous.metadata_hash
        ) and not self.is_metadata_only_change(previous)

    def croniter(self, value: TimeLike) -> CroniterCache:
        if self._croniter is None:
            self._croniter = CroniterCache(self.cron, value, tz=self.cron_tz)
        else:
            self._croniter.curr = to_datetime(value, tz=self.cron_tz)
        return self._croniter

    def cron_next(self, value: TimeLike, estimate: bool = False) -> datetime:
        """
        Get the next timestamp given a time-like value and the node's cron.

        Args:
            value: A variety of date formats.
            estimate: Whether or not to estimate, only use this if the value is floored.

        Returns:
            The timestamp for the next run.
        """
        return self.croniter(value).get_next(estimate=estimate)

    def cron_prev(self, value: TimeLike, estimate: bool = False) -> datetime:
        """
        Get the previous timestamp given a time-like value and the node's cron.

        Args:
            value: A variety of date formats.
            estimate: Whether or not to estimate, only use this if the value is floored.

        Returns:
            The timestamp for the previous run.
        """
        return self.croniter(value).get_prev(estimate=estimate)

    def cron_floor(self, value: TimeLike, estimate: bool = False) -> datetime:
        """
        Get the floor timestamp given a time-like value and the node's cron.

        Args:
            value: A variety of date formats.
            estimate: Whether or not to estimate, only use this if the value is floored.

        Returns:
            The timestamp floor.
        """
        return self.croniter(self.cron_next(value, estimate=estimate)).get_prev(estimate=True)

    def text_diff(self, other: Node, rendered: bool = False) -> str:
        """Produce a text diff against another node.

        Args:
            other: The node to diff against. Must be of the same type.

        Returns:
            A unified text diff showing additions and deletions.
        """
        raise NotImplementedError

    def _inferred_interval_unit(self) -> IntervalUnit:
        """Infers the interval unit from the cron expression.

        The interval unit is used to determine the lag applied to start_date and end_date for node rendering and intervals.

        Returns:
            The IntervalUnit enum.
        """
        if not self.__inferred_interval_unit:
            self.__inferred_interval_unit = IntervalUnit.from_cron(self.cron)
        return self.__inferred_interval_unit

    @property
    def is_model(self) -> bool:
        """Return True if this is a model node"""
        return False

    @property
    def is_audit(self) -> bool:
        """Return True if this is an audit node"""
        return False

    @property
    def dbt_node_info(self) -> t.Optional[DbtNodeInfo]:
        return self.dbt_node_info_


class NodeType(str, Enum):
    MODEL = "model"
    AUDIT = "audit"

    def __str__(self) -> str:
        return self.name


def str_or_exp_to_str(v: t.Any) -> t.Optional[str]:
    if isinstance(v, exp.Expr):
        return v.name
    return str(v) if v is not None else None
