from __future__ import annotations

import typing as t
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import cached_property
from pydantic import Field

from sqlmesh.core.context_diff import ContextDiff
from sqlmesh.core.environment import Environment, EnvironmentNamingInfo, EnvironmentStatements
from sqlmesh.utils.metaprogramming import Executable  # noqa
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Intervals,
    Snapshot,
    earliest_start_date,
    merge_intervals,
    missing_intervals,
)
from sqlmesh.core.snapshot.definition import (
    Interval,
    SnapshotId,
    SnapshotTableInfo,
    format_intervals,
)
from sqlmesh.utils.date import TimeLike, now, to_datetime, to_timestamp
from sqlmesh.utils.pydantic import PydanticModel

SnapshotMapping = t.Dict[SnapshotId, t.Set[SnapshotId]]
UserProvidedFlags = t.Union[TimeLike, str, bool, t.List[str]]


class Plan(PydanticModel, frozen=True):
    context_diff: ContextDiff
    plan_id: str
    provided_start: t.Optional[TimeLike] = None
    provided_end: t.Optional[TimeLike] = None

    is_dev: bool
    skip_backfill: bool
    empty_backfill: bool
    skip_audits: bool = False
    no_gaps: bool
    forward_only: bool
    allow_destructive_models: t.Set[str]
    allow_additive_models: t.Set[str]
    include_unmodified: bool
    end_bounded: bool
    ensure_finalized_snapshots: bool
    explain: bool
    ignore_cron: bool = False

    environment_ttl: t.Optional[str] = None
    environment_naming_info: EnvironmentNamingInfo

    directly_modified: t.Set[SnapshotId]
    indirectly_modified: t.Dict[SnapshotId, t.Set[SnapshotId]]

    deployability_index: DeployabilityIndex
    selected_models_to_restate: t.Optional[t.Set[str]] = None
    """Models that have been explicitly selected for restatement by a user"""
    restatements: t.Dict[SnapshotId, Interval]
    """
    All models being restated, which are typically the explicitly selected ones + their downstream dependencies.

    Note that dev previews are also considered restatements, so :selected_models_to_restate can be empty
    while :restatements is still populated with dev previews
    """
    restate_all_snapshots: bool
    """Whether or not to clear intervals from state for other versions of the models listed in :restatements"""

    start_override_per_model: t.Optional[t.Dict[str, datetime]]
    end_override_per_model: t.Optional[t.Dict[str, datetime]]

    selected_models_to_backfill: t.Optional[t.Set[str]] = None
    """Models that have been explicitly selected for backfill by a user."""
    models_to_backfill: t.Optional[t.Set[str]] = None
    """All models that should be backfilled as part of this plan."""
    effective_from: t.Optional[TimeLike] = None
    execution_time_: t.Optional[TimeLike] = Field(default=None, alias="execution_time")

    user_provided_flags: t.Optional[t.Dict[str, UserProvidedFlags]] = None
    selected_models: t.Optional[t.Set[str]] = None
    """Models that have been selected for this plan (used for dbt selected_resources)"""

    @cached_property
    def start(self) -> TimeLike:
        if self.provided_start is not None:
            return self.provided_start

        missing_intervals = self.missing_intervals
        if missing_intervals:
            return min(si.intervals[0][0] for si in missing_intervals)

        return self._earliest_interval_start

    @cached_property
    def end(self) -> TimeLike:
        return self.provided_end or self.execution_time

    @cached_property
    def execution_time(self) -> TimeLike:
        # note: property is cached so that it returns a consistent timestamp for now()
        return self.execution_time_ or now()

    @property
    def previous_plan_id(self) -> t.Optional[str]:
        return self.context_diff.previous_plan_id

    @property
    def requires_backfill(self) -> bool:
        return (
            not self.skip_backfill
            and not self.empty_backfill
            and (bool(self.restatements) or bool(self.missing_intervals))
        )

    @property
    def has_changes(self) -> bool:
        return self.context_diff.has_changes

    @property
    def has_unmodified_unpromoted(self) -> bool:
        """Is the plan for an existing dev environment, has the include unmodified flag, and contains unmodified nodes that have not been promoted."""
        return (
            self.is_dev
            and not self.context_diff.is_new_environment
            and self.include_unmodified
            and bool(self.context_diff.unpromoted_models)
        )

    @property
    def categorized(self) -> t.List[Snapshot]:
        """Returns the already categorized snapshots."""
        return [
            self.context_diff.snapshots[s_id]
            for s_id in sorted({*self.directly_modified, *self.metadata_updated})
            if self.context_diff.snapshots[s_id].version
        ]

    @property
    def uncategorized(self) -> t.List[Snapshot]:
        """Returns the uncategorized snapshots."""
        return [
            self.context_diff.snapshots[s_id]
            for s_id in sorted(self.directly_modified)
            if not self.context_diff.snapshots[s_id].version
        ]

    @property
    def snapshots(self) -> t.Dict[SnapshotId, Snapshot]:
        return self.context_diff.snapshots

    @cached_property
    def modified_snapshots(self) -> t.Dict[SnapshotId, t.Union[Snapshot, SnapshotTableInfo]]:
        """Returns the modified (either directly or indirectly) snapshots."""
        return {
            **{s_id: self.context_diff.snapshots[s_id] for s_id in sorted(self.directly_modified)},
            **{
                s_id: self.context_diff.snapshots[s_id]
                for downstream_s_ids in self.indirectly_modified.values()
                for s_id in sorted(downstream_s_ids)
            },
            **self.context_diff.removed_snapshots,
            **{s_id: self.context_diff.snapshots[s_id] for s_id in sorted(self.metadata_updated)},
        }

    @cached_property
    def metadata_updated(self) -> t.Set[SnapshotId]:
        return {
            snapshot.snapshot_id
            for snapshot, _ in self.context_diff.modified_snapshots.values()
            if self.context_diff.metadata_updated(snapshot.name)
        }

    @property
    def new_snapshots(self) -> t.List[Snapshot]:
        """Gets only new snapshots in the plan/environment."""
        return list(self.context_diff.new_snapshots.values())

    @property
    def missing_intervals(self) -> t.List[SnapshotIntervals]:
        """Returns the missing intervals for this plan."""
        # NOTE: Even though the plan is immutable, snapshots that are part of it are not. Since snapshot intervals
        # may change over time, we should avoid caching missing intervals within the plan instance.
        intervals = [
            SnapshotIntervals(snapshot_id=snapshot.snapshot_id, intervals=missing)
            for snapshot, missing in missing_intervals(
                [s for s in self.snapshots.values() if self.is_selected_for_backfill(s.name)],
                start=self.provided_start or self._earliest_interval_start,
                end=self.provided_end,
                execution_time=self.execution_time,
                restatements=self.restatements,
                deployability_index=self.deployability_index,
                start_override_per_model=self.start_override_per_model,
                end_override_per_model=self.end_override_per_model,
                end_bounded=self.end_bounded,
                ignore_cron=self.ignore_cron,
            ).items()
            if snapshot.is_model and missing
        ]
        return sorted(intervals, key=lambda i: i.snapshot_id)

    @cached_property
    def environment(self) -> Environment:
        """The environment of this plan."""
        expiration_ts = (
            to_timestamp(self.environment_ttl, relative_base=now())
            if self.is_dev and self.environment_ttl is not None
            else None
        )

        snapshots_by_name = self.context_diff.snapshots_by_name
        snapshots = [s.table_info for s in self.snapshots.values()]
        promotable_snapshot_ids = None
        if self.is_dev:
            if self.selected_models_to_backfill is not None:
                # Only promote models that have been explicitly selected for backfill.
                promotable_snapshot_ids = {
                    *self.context_diff.previously_promoted_snapshot_ids,
                    *[
                        snapshots_by_name[m].snapshot_id
                        for m in self.selected_models_to_backfill
                        if m in snapshots_by_name
                    ],
                }
            elif not self.include_unmodified:
                promotable_snapshot_ids = self.context_diff.promotable_snapshot_ids.copy()

        promoted_snapshot_ids = (
            [s.snapshot_id for s in snapshots if s.snapshot_id in promotable_snapshot_ids]
            if promotable_snapshot_ids is not None
            else None
        )

        previous_finalized_snapshots = (
            self.context_diff.environment_snapshots
            if not self.context_diff.is_unfinalized_environment
            else self.context_diff.previous_finalized_snapshots
        )

        return Environment(
            snapshots=snapshots,
            start_at=self.provided_start or self._earliest_interval_start,
            end_at=self.provided_end,
            plan_id=self.plan_id,
            previous_plan_id=self.previous_plan_id,
            expiration_ts=expiration_ts,
            promoted_snapshot_ids=promoted_snapshot_ids,
            previous_finalized_snapshots=previous_finalized_snapshots,
            requirements=self.context_diff.requirements,
            **self.environment_naming_info.dict(),
        )

    def is_new_snapshot(self, snapshot: Snapshot) -> bool:
        """Returns True if the given snapshot is a new snapshot in this plan."""
        snapshot_id = snapshot.snapshot_id
        return snapshot_id in self.context_diff.new_snapshots

    def is_selected_for_backfill(self, model_fqn: str) -> bool:
        """Returns True if a model with the given FQN should be backfilled as part of this plan."""
        return self.models_to_backfill is None or model_fqn in self.models_to_backfill

    def to_evaluatable(self) -> EvaluatablePlan:
        return EvaluatablePlan(
            start=self.start,
            end=self.end,
            new_snapshots=self.new_snapshots,
            environment=self.environment,
            no_gaps=self.no_gaps,
            skip_backfill=self.skip_backfill,
            empty_backfill=self.empty_backfill,
            skip_audits=self.skip_audits,
            restatements={s.name: i for s, i in self.restatements.items()},
            restate_all_snapshots=self.restate_all_snapshots,
            is_dev=self.is_dev,
            allow_destructive_models=self.allow_destructive_models,
            allow_additive_models=self.allow_additive_models,
            forward_only=self.forward_only,
            end_bounded=self.end_bounded,
            ensure_finalized_snapshots=self.ensure_finalized_snapshots,
            ignore_cron=self.ignore_cron,
            directly_modified_snapshots=sorted(self.directly_modified),
            indirectly_modified_snapshots={
                s.name: sorted(snapshot_ids) for s, snapshot_ids in self.indirectly_modified.items()
            },
            metadata_updated_snapshots=sorted(self.metadata_updated),
            removed_snapshots=sorted(self.context_diff.removed_snapshots),
            requires_backfill=self.requires_backfill,
            models_to_backfill=self.models_to_backfill,
            start_override_per_model=self.start_override_per_model,
            end_override_per_model=self.end_override_per_model,
            execution_time=self.execution_time,
            disabled_restatement_models={
                s.name
                for s in self.snapshots.values()
                if s.is_model and s.model.disable_restatement
            },
            environment_statements=self.context_diff.environment_statements,
            user_provided_flags=self.user_provided_flags,
            selected_models=self.selected_models,
        )

    @cached_property
    def _earliest_interval_start(self) -> datetime:
        return earliest_interval_start(self.snapshots.values(), self.execution_time)


class EvaluatablePlan(PydanticModel):
    """A serializable version of a plan that can be evaluated."""

    start: TimeLike
    end: TimeLike
    new_snapshots: t.List[Snapshot]
    environment: Environment
    no_gaps: bool
    skip_backfill: bool
    empty_backfill: bool
    skip_audits: bool = False
    restatements: t.Dict[str, Interval]
    restate_all_snapshots: bool
    is_dev: bool
    allow_destructive_models: t.Set[str]
    allow_additive_models: t.Set[str]
    forward_only: bool
    end_bounded: bool
    ensure_finalized_snapshots: bool
    ignore_cron: bool = False
    directly_modified_snapshots: t.List[SnapshotId]
    indirectly_modified_snapshots: t.Dict[str, t.List[SnapshotId]]
    metadata_updated_snapshots: t.List[SnapshotId]
    removed_snapshots: t.List[SnapshotId]
    requires_backfill: bool
    models_to_backfill: t.Optional[t.Set[str]] = None
    start_override_per_model: t.Optional[t.Dict[str, datetime]] = None
    end_override_per_model: t.Optional[t.Dict[str, datetime]] = None
    execution_time: t.Optional[TimeLike] = None
    disabled_restatement_models: t.Set[str]
    environment_statements: t.Optional[t.List[EnvironmentStatements]] = None
    user_provided_flags: t.Optional[t.Dict[str, UserProvidedFlags]] = None
    selected_models: t.Optional[t.Set[str]] = None

    def is_selected_for_backfill(self, model_fqn: str) -> bool:
        return self.models_to_backfill is None or model_fqn in self.models_to_backfill

    @property
    def plan_id(self) -> str:
        return self.environment.plan_id

    @property
    def is_prod(self) -> bool:
        return not self.is_dev


class PlanStatus(str, Enum):
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"

    @property
    def is_started(self) -> bool:
        return self == PlanStatus.STARTED

    @property
    def is_failed(self) -> bool:
        return self == PlanStatus.FAILED

    @property
    def is_finished(self) -> bool:
        return self == PlanStatus.FINISHED


# millions of these can be created, pydantic has significant overhead
@dataclass
class SnapshotIntervals:
    snapshot_id: SnapshotId
    intervals: Intervals

    @property
    def merged_intervals(self) -> Intervals:
        return merge_intervals(self.intervals)

    def format_intervals(self, unit: t.Optional[IntervalUnit] = None) -> str:
        return format_intervals(self.merged_intervals, unit)


def earliest_interval_start(
    snapshots: t.Collection[Snapshot], execution_time: t.Optional[TimeLike] = None
) -> datetime:
    earliest_start = earliest_start_date(snapshots, relative_to=execution_time)
    earliest_interval_starts = [s.intervals[0][0] for s in snapshots if s.intervals]
    return (
        min(earliest_start, to_datetime(min(earliest_interval_starts)))
        if earliest_interval_starts
        else earliest_start
    )
