from __future__ import annotations

import logging
import re
import typing as t
from collections import defaultdict
from functools import cached_property
from datetime import datetime


from sqlmesh.core.console import PlanBuilderConsole, get_console
from sqlmesh.core.config import (
    AutoCategorizationMode,
    CategorizerConfig,
    EnvironmentSuffixTarget,
)
from sqlmesh.core.context_diff import ContextDiff
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.plan.common import should_force_rebuild, is_breaking_kind_change
from sqlmesh.core.plan.definition import (
    Plan,
    SnapshotMapping,
    UserProvidedFlags,
    earliest_interval_start,
)
from sqlmesh.core.schema_diff import (
    get_schema_differ,
    has_drop_alteration,
    has_additive_alteration,
    TableAlterOperation,
)
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotChangeCategory,
)
from sqlmesh.core.snapshot.categorizer import categorize_change
from sqlmesh.core.snapshot.definition import Interval, SnapshotId
from sqlmesh.utils import columns_to_types_all_known, random_id
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import (
    TimeLike,
    now,
    to_datetime,
    yesterday_ds,
    to_timestamp,
    time_like_to_str,
    is_relative,
)
from sqlmesh.utils.errors import NoChangesPlanError, PlanError

logger = logging.getLogger(__name__)


class PlanBuilder:
    """Plan Builder constructs a Plan based on user choices for how they want to backfill, preview, etc. their changes.

    Args:
        context_diff: The context diff that the plan is based on.
        start: The start time to backfill data.
        end: The end time to backfill data.
        execution_time: The date/time time reference to use for execution time. Defaults to now.
            If :start or :end are relative time expressions, they are interpreted as relative to the :execution_time
        apply: The callback to apply the plan.
        restate_models: A list of models for which the data should be restated for the time range
            specified in this plan. Note: models defined outside SQLMesh (external) won't be a part
            of the restatement.
        restate_all_snapshots: If restatements are present, this flag indicates whether or not the intervals
            being restated should be cleared from state for other versions of this model (typically, versions that are present in other environments).
            If set to None, the default behaviour is to not clear anything unless the target environment is prod.
        backfill_models: A list of fully qualified model names for which the data should be backfilled as part of this plan.
        no_gaps:  Whether to ensure that new snapshots for nodes that are already a
            part of the target environment have no data gaps when compared against previous
            snapshots for same nodes.
        skip_backfill: Whether to skip the backfill step.
        empty_backfill: Like skip_backfill, but also records processed intervals.
        is_dev: Whether this plan is for development purposes.
        forward_only: Whether the purpose of the plan is to make forward only changes.
        allow_destructive_models: A list of fully qualified model names whose forward-only changes are allowed to be destructive.
        allow_additive_models: A list of fully qualified model names whose forward-only changes are allowed to be additive.
        environment_ttl: The period of time that a development environment should exist before being deleted.
        categorizer_config: Auto categorization settings.
        auto_categorization_enabled: Whether to apply auto categorization.
        effective_from: The effective date from which to apply forward-only changes on production.
        include_unmodified: Indicates whether to include unmodified nodes in the target development environment.
        environment_suffix_target: Indicates whether to append the environment name to the schema or table name.
        default_start: The default plan start to use if not specified.
        default_end: The default plan end to use if not specified.
        enable_preview: Whether to enable preview for forward-only models in development environments.
        end_bounded: If set to true, the missing intervals will be bounded by the target end date, disregarding lookback,
            allow_partials, and other attributes that could cause the intervals to exceed the target end date.
        ensure_finalized_snapshots: Whether to compare against snapshots from the latest finalized
            environment state, or to use whatever snapshots are in the current environment state even if
            the environment is not finalized.
        start_override_per_model: A mapping of model FQNs to target start dates.
        end_override_per_model: A mapping of model FQNs to target end dates.
        ignore_cron: Whether to ignore the node's cron schedule when computing missing intervals.
        explain: Whether to explain the plan instead of applying it.
    """

    def __init__(
        self,
        context_diff: ContextDiff,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        apply: t.Optional[t.Callable[[Plan], None]] = None,
        restate_models: t.Optional[t.Iterable[str]] = None,
        restate_all_snapshots: bool = False,
        backfill_models: t.Optional[t.Iterable[str]] = None,
        no_gaps: bool = False,
        skip_backfill: bool = False,
        empty_backfill: bool = False,
        is_dev: bool = False,
        forward_only: bool = False,
        allow_destructive_models: t.Optional[t.Iterable[str]] = None,
        allow_additive_models: t.Optional[t.Iterable[str]] = None,
        environment_ttl: t.Optional[str] = None,
        environment_suffix_target: EnvironmentSuffixTarget = EnvironmentSuffixTarget.default,
        environment_catalog_mapping: t.Optional[t.Dict[re.Pattern, str]] = None,
        categorizer_config: t.Optional[CategorizerConfig] = None,
        auto_categorization_enabled: bool = True,
        effective_from: t.Optional[TimeLike] = None,
        include_unmodified: bool = False,
        default_start: t.Optional[TimeLike] = None,
        default_end: t.Optional[TimeLike] = None,
        enable_preview: bool = False,
        end_bounded: bool = False,
        ensure_finalized_snapshots: bool = False,
        explain: bool = False,
        ignore_cron: bool = False,
        start_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        end_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        console: t.Optional[PlanBuilderConsole] = None,
        user_provided_flags: t.Optional[t.Dict[str, UserProvidedFlags]] = None,
        selected_models: t.Optional[t.Set[str]] = None,
    ):
        self._context_diff = context_diff
        self._no_gaps = no_gaps
        self._skip_backfill = skip_backfill
        self._empty_backfill = empty_backfill
        self._is_dev = is_dev
        self._forward_only = forward_only
        self._allow_destructive_models = set(
            allow_destructive_models if allow_destructive_models is not None else []
        )
        self._allow_additive_models = set(
            allow_additive_models if allow_additive_models is not None else []
        )
        self._enable_preview = enable_preview
        self._end_bounded = end_bounded
        self._ensure_finalized_snapshots = ensure_finalized_snapshots
        self._ignore_cron = ignore_cron
        self._start_override_per_model = start_override_per_model
        self._end_override_per_model = end_override_per_model
        self._environment_ttl = environment_ttl
        self._categorizer_config = categorizer_config or CategorizerConfig()
        self._auto_categorization_enabled = auto_categorization_enabled
        self._include_unmodified = include_unmodified
        self._restate_models = set(restate_models) if restate_models is not None else None
        self._restate_all_snapshots = restate_all_snapshots
        self._effective_from = effective_from

        # note: this deliberately doesnt default to now() here.
        # There may be an significant delay between the PlanBuilder producing a Plan and the Plan actually being run
        # so if execution_time=None is passed to the PlanBuilder, then the resulting Plan should also have execution_time=None
        # in order to prevent the Plan that was intended to run "as at now" from having "now" fixed to some time in the past
        # ref: https://github.com/SQLMesh/sqlmesh/pull/4702#discussion_r2140696156
        self._execution_time = execution_time

        self._backfill_models = backfill_models
        self._end = end or default_end
        self._default_start = default_start
        self._apply = apply
        self._console = console or get_console()
        self._choices: t.Dict[SnapshotId, SnapshotChangeCategory] = {}
        self._user_provided_flags = user_provided_flags
        self._selected_models = selected_models
        self._explain = explain

        self._start = start
        if not self._start and (
            self._forward_only_preview_needed or self._non_forward_only_preview_needed
        ):
            self._start = default_start or yesterday_ds()

        self._plan_id: str = random_id()
        self._model_fqn_to_snapshot = {s.name: s for s in self._context_diff.snapshots.values()}

        self.override_start = start is not None
        self.override_end = end is not None
        self.environment_naming_info = EnvironmentNamingInfo.from_environment_catalog_mapping(
            environment_catalog_mapping or {},
            name=self._context_diff.environment,
            suffix_target=environment_suffix_target,
            normalize_name=self._context_diff.normalize_environment_name,
            gateway_managed=self._context_diff.gateway_managed_virtual_layer,
        )

        self._latest_plan: t.Optional[Plan] = None

    @property
    def is_start_and_end_allowed(self) -> bool:
        """Indicates whether this plan allows to set the start and end dates."""
        return self._is_dev or bool(self._restate_models)

    @property
    def start(self) -> t.Optional[TimeLike]:
        if self._start and is_relative(self._start):
            # only do this for relative expressions otherwise inclusive date strings like '2020-01-01' can be turned into exclusive timestamps eg '2020-01-01 00:00:00'
            return to_datetime(self._start, relative_base=to_datetime(self.execution_time))
        return self._start

    @property
    def end(self) -> t.Optional[TimeLike]:
        if self._end and is_relative(self._end):
            # only do this for relative expressions otherwise inclusive date strings like '2020-01-01' can be turned into exclusive timestamps eg '2020-01-01 00:00:00'
            return to_datetime(self._end, relative_base=to_datetime(self.execution_time))
        return self._end

    @cached_property
    def execution_time(self) -> TimeLike:
        # this is cached to return a stable value from now() in the places where the execution time matters for resolving relative date strings
        # during the plan building process
        return self._execution_time or now()

    def set_start(self, new_start: TimeLike) -> PlanBuilder:
        self._start = new_start
        self.override_start = True
        self._latest_plan = None
        return self

    def set_end(self, new_end: TimeLike) -> PlanBuilder:
        self._end = new_end
        self.override_end = True
        self._latest_plan = None
        return self

    def set_effective_from(self, effective_from: t.Optional[TimeLike]) -> PlanBuilder:
        """Sets the effective date for all new snapshots in the plan.

        Note: this is only applicable for forward-only plans.

        Args:
            effective_from: The effective date to set.
        """
        self._effective_from = effective_from
        if effective_from and self._is_dev and not self.override_start:
            self._start = effective_from
        self._latest_plan = None
        return self

    def set_choice(self, snapshot: Snapshot, choice: SnapshotChangeCategory) -> PlanBuilder:
        """Sets a snapshot version based on the user choice.

        Args:
            snapshot: The target snapshot.
            choice: The user decision on how to version the target snapshot and its children.
        """
        if not self._is_new_snapshot(snapshot):
            raise PlanError(
                f"A choice can't be changed for the existing version of {snapshot.name}."
            )
        if (
            not self._context_diff.directly_modified(snapshot.name)
            and snapshot.snapshot_id not in self._context_diff.added
        ):
            raise PlanError(f"Only directly modified models can be categorized ({snapshot.name}).")

        self._choices[snapshot.snapshot_id] = choice
        self._latest_plan = None
        return self

    def apply(self) -> None:
        """Builds and applies the plan."""
        if not self._apply:
            raise PlanError("Plan was not initialized with an applier.")
        self._apply(self.build())

    def build(self) -> Plan:
        """Builds the plan."""
        if self._latest_plan:
            return self._latest_plan

        self._ensure_new_env_with_changes()
        self._ensure_valid_date_range()
        self._ensure_no_broken_references()

        self._apply_effective_from()

        dag = self._build_dag()
        directly_modified, indirectly_modified = self._build_directly_and_indirectly_modified(dag)

        self._check_destructive_additive_changes(directly_modified)
        self._categorize_snapshots(dag, indirectly_modified)
        self._adjust_snapshot_intervals()

        deployability_index = (
            DeployabilityIndex.create(
                self._context_diff.snapshots.values(),
                start=self._start,
                start_override_per_model=self._start_override_per_model,
            )
            if self._is_dev
            else DeployabilityIndex.all_deployable()
        )

        restatements = self._build_restatements(
            dag,
            earliest_interval_start(self._context_diff.snapshots.values(), self.execution_time),
        )
        models_to_backfill = self._build_models_to_backfill(dag, restatements)

        end_override_per_model = self._end_override_per_model
        if end_override_per_model and self.override_end:
            # If the end date was provided explicitly by a user, then interval end for each individual
            # model should be ignored.
            end_override_per_model = None

        # this deliberately uses the passed in self._execution_time and not self.execution_time cached property
        # the reason is because that there can be a delay between the Plan being built and the Plan being actually run,
        # so this ensures that an _execution_time of None can be propagated to the Plan and thus be re-resolved to
        # the current timestamp of when the Plan is eventually run
        plan_execution_time = self._execution_time

        plan = Plan(
            context_diff=self._context_diff,
            plan_id=self._plan_id,
            provided_start=self.start,
            provided_end=self.end,
            is_dev=self._is_dev,
            skip_backfill=self._skip_backfill,
            empty_backfill=self._empty_backfill,
            no_gaps=self._no_gaps,
            forward_only=self._forward_only,
            explain=self._explain,
            allow_destructive_models=t.cast(t.Set, self._allow_destructive_models),
            allow_additive_models=t.cast(t.Set, self._allow_additive_models),
            include_unmodified=self._include_unmodified,
            environment_ttl=self._environment_ttl,
            environment_naming_info=self.environment_naming_info,
            directly_modified=directly_modified,
            indirectly_modified=indirectly_modified,
            deployability_index=deployability_index,
            selected_models_to_restate=self._restate_models,
            restatements=restatements,
            restate_all_snapshots=self._restate_all_snapshots,
            start_override_per_model=self._start_override_per_model,
            end_override_per_model=end_override_per_model,
            selected_models_to_backfill=self._backfill_models,
            models_to_backfill=models_to_backfill,
            effective_from=self._effective_from,
            execution_time=plan_execution_time,
            end_bounded=self._end_bounded,
            ensure_finalized_snapshots=self._ensure_finalized_snapshots,
            ignore_cron=self._ignore_cron,
            user_provided_flags=self._user_provided_flags,
            selected_models=self._selected_models,
        )
        self._latest_plan = plan
        return plan

    def _build_dag(self) -> DAG[SnapshotId]:
        dag: DAG[SnapshotId] = DAG()
        for s_id, context_snapshot in self._context_diff.snapshots.items():
            dag.add(s_id, context_snapshot.parents)
        return dag

    def _build_restatements(
        self, dag: DAG[SnapshotId], earliest_interval_start: TimeLike
    ) -> t.Dict[SnapshotId, Interval]:
        restate_models = self._restate_models
        if restate_models == set():
            # This is a warning but we print this as error since the Console is lacking API for warnings.
            self._console.log_error(
                "Provided restated models do not match any models. No models will be included in plan."
            )
            return {}

        restatements: t.Dict[SnapshotId, Interval] = {}
        forward_only_preview_needed = self._forward_only_preview_needed
        is_preview = False
        if not restate_models and forward_only_preview_needed:
            # Add model names for new forward-only snapshots to the restatement list
            # in order to compute previews.
            restate_models = {
                s.name
                for s in self._context_diff.new_snapshots.values()
                if s.is_model
                and not s.is_symbolic
                and (s.is_forward_only or s.model.forward_only)
                and not s.is_no_preview
                and (
                    # Metadata changes should not be previewed.
                    self._context_diff.directly_modified(s.name)
                    or self._context_diff.indirectly_modified(s.name)
                )
            }
            is_preview = True

        if not restate_models:
            return {}

        start = self._start or earliest_interval_start
        end = self._end or now()

        # Add restate snapshots and their downstream snapshots
        for model_fqn in restate_models:
            if model_fqn not in self._model_fqn_to_snapshot:
                raise PlanError(f"Cannot restate model '{model_fqn}'. Model does not exist.")

        # Get restatement intervals for all restated snapshots and make sure that if an incremental snapshot expands it's
        # restatement range that it's downstream dependencies all expand their restatement ranges as well.
        for s_id in dag:
            snapshot = self._context_diff.snapshots[s_id]

            if is_preview and snapshot.is_no_preview:
                continue

            # Since we are traversing the graph in topological order and the largest interval range is pushed down
            # the graph we just have to check our immediate parents in the graph and not the whole upstream graph.
            restating_parents = [
                self._context_diff.snapshots[s] for s in snapshot.parents if s in restatements
            ]

            if not restating_parents and snapshot.name not in restate_models:
                continue

            if not forward_only_preview_needed:
                if self._is_dev and not snapshot.is_paused:
                    self._console.log_warning(
                        f"Cannot restate model '{snapshot.name}' because the current version is used in production. "
                        "Run the restatement against the production environment instead to restate this model."
                    )
                    continue
                elif (not self._is_dev or not snapshot.is_paused) and snapshot.disable_restatement:
                    self._console.log_warning(
                        f"Cannot restate model '{snapshot.name}'. "
                        "Restatement is disabled for this model to prevent possible data loss. "
                        "If you want to restate this model, change the model's `disable_restatement` setting to `false`."
                    )
                    continue
                elif snapshot.is_seed:
                    logger.info("Skipping restatement for model '%s'", snapshot.name)
                    continue

            possible_intervals = {
                restatements[p.snapshot_id] for p in restating_parents if p.is_incremental
            }
            possible_intervals.add(
                snapshot.get_removal_interval(
                    start,
                    end,
                    self._execution_time,
                    strict=False,
                    is_preview=is_preview,
                )
            )
            snapshot_start = min(i[0] for i in possible_intervals)
            snapshot_end = max(i[1] for i in possible_intervals)

            # We may be tasked with restating a time range smaller than the target snapshot interval unit
            # For example, restating an hour of Hourly Model A, which has a downstream dependency of Daily Model B
            # we need to ensure the whole affected day in Model B is restated
            floored_snapshot_start = snapshot.node.interval_unit.cron_floor(snapshot_start)
            floored_snapshot_end = snapshot.node.interval_unit.cron_floor(snapshot_end)
            if to_timestamp(floored_snapshot_end) < snapshot_end:
                snapshot_start = to_timestamp(floored_snapshot_start)
                snapshot_end = to_timestamp(
                    snapshot.node.interval_unit.cron_next(floored_snapshot_end)
                )

            restatements[s_id] = (snapshot_start, snapshot_end)

        return restatements

    def _build_directly_and_indirectly_modified(
        self, dag: DAG[SnapshotId]
    ) -> t.Tuple[t.Set[SnapshotId], SnapshotMapping]:
        """Builds collections of directly and indirectly modified snapshots.

        Returns:
            The tuple in which the first element contains a list of added and directly modified
            snapshots while the second element contains a mapping of indirectly modified snapshots.
        """
        directly_modified = set()
        all_indirectly_modified = set()

        for s_id in dag:
            if s_id.name in self._context_diff.modified_snapshots:
                if self._context_diff.directly_modified(s_id.name):
                    directly_modified.add(s_id)
                else:
                    all_indirectly_modified.add(s_id)
            elif s_id in self._context_diff.added:
                directly_modified.add(s_id)

        indirectly_modified: SnapshotMapping = defaultdict(set)
        for snapshot in directly_modified:
            for downstream_s_id in dag.downstream(snapshot.snapshot_id):
                if downstream_s_id in all_indirectly_modified:
                    indirectly_modified[snapshot.snapshot_id].add(downstream_s_id)

        return (
            directly_modified,
            indirectly_modified,
        )

    def _build_models_to_backfill(
        self, dag: DAG[SnapshotId], restatements: t.Collection[SnapshotId]
    ) -> t.Optional[t.Set[str]]:
        backfill_models = (
            self._backfill_models
            if self._backfill_models is not None
            else [r.name for r in restatements]
            # Only backfill models explicitly marked for restatement.
            if self._restate_models
            else None
        )
        if backfill_models is None:
            return None
        return {
            self._context_diff.snapshots[s_id].name
            for s_id in dag.subdag(
                *[
                    self._model_fqn_to_snapshot[m].snapshot_id
                    for m in backfill_models
                    if m in self._model_fqn_to_snapshot
                ]
            ).sorted
        }

    def _adjust_snapshot_intervals(self) -> None:
        for new, old in self._context_diff.modified_snapshots.values():
            if not new.is_model or not old.is_model:
                continue
            is_same_version = old.version_get_or_generate() == new.version_get_or_generate()
            if is_same_version and should_force_rebuild(old, new):
                # If the difference between 2 snapshots requires a full rebuild,
                # then clear the intervals for the new snapshot.
                self._context_diff.snapshots[new.snapshot_id].intervals = []
            elif new.snapshot_id in self._context_diff.new_snapshots:
                new.intervals = []
                new.dev_intervals = []
                if is_same_version:
                    new.merge_intervals(old)
                    if new.is_forward_only:
                        new.dev_intervals = new.intervals.copy()

    def _check_destructive_additive_changes(self, directly_modified: t.Set[SnapshotId]) -> None:
        for s_id in sorted(directly_modified):
            if s_id.name not in self._context_diff.modified_snapshots:
                continue

            snapshot = self._context_diff.snapshots[s_id]
            needs_destructive_check = snapshot.needs_destructive_check(
                self._allow_destructive_models
            )
            needs_additive_check = snapshot.needs_additive_check(self._allow_additive_models)
            # should we raise/warn if this snapshot has/inherits a destructive change?
            should_raise_or_warn = (self._is_forward_only_change(s_id) or self._forward_only) and (
                needs_destructive_check or needs_additive_check
            )

            if not should_raise_or_warn or not snapshot.is_model:
                continue

            new, old = self._context_diff.modified_snapshots[snapshot.name]

            # we must know all columns_to_types to determine whether a change is destructive
            old_columns_to_types = old.model.columns_to_types or {}
            new_columns_to_types = new.model.columns_to_types or {}

            if columns_to_types_all_known(old_columns_to_types) and columns_to_types_all_known(
                new_columns_to_types
            ):
                alter_operations = t.cast(
                    t.List[TableAlterOperation],
                    get_schema_differ(snapshot.model.dialect).compare_columns(
                        new.name,
                        old_columns_to_types,
                        new_columns_to_types,
                        ignore_destructive=new.model.on_destructive_change.is_ignore,
                        ignore_additive=new.model.on_additive_change.is_ignore,
                    ),
                )

                snapshot_name = snapshot.name
                model_dialect = snapshot.model.dialect

                if needs_destructive_check and has_drop_alteration(alter_operations):
                    self._console.log_destructive_change(
                        snapshot_name,
                        alter_operations,
                        model_dialect,
                        error=not snapshot.model.on_destructive_change.is_warn,
                    )
                    if snapshot.model.on_destructive_change.is_error:
                        raise PlanError(
                            "Plan requires a destructive change to a forward-only model."
                        )

                if needs_additive_check and has_additive_alteration(alter_operations):
                    self._console.log_additive_change(
                        snapshot_name,
                        alter_operations,
                        model_dialect,
                        error=not snapshot.model.on_additive_change.is_warn,
                    )
                    if snapshot.model.on_additive_change.is_error:
                        raise PlanError("Plan requires an additive change to a forward-only model.")

    def _categorize_snapshots(
        self, dag: DAG[SnapshotId], indirectly_modified: SnapshotMapping
    ) -> None:
        """Automatically categorizes snapshots that can be automatically categorized and
        returns a list of added and directly modified snapshots as well as the mapping of
        indirectly modified snapshots.
        """

        # Iterating in DAG order since a category for a snapshot may depend on the categories
        # assigned to its upstream dependencies.
        for s_id in dag:
            snapshot = self._context_diff.snapshots.get(s_id)

            if not snapshot or not self._is_new_snapshot(snapshot):
                continue

            forward_only = self._forward_only or self._is_forward_only_change(s_id)
            if forward_only and s_id.name in self._context_diff.modified_snapshots:
                new, old = self._context_diff.modified_snapshots[s_id.name]
                if is_breaking_kind_change(old, new) or snapshot.is_seed:
                    # Breaking kind changes and seed changes can't be forward-only.
                    forward_only = False

            if s_id in self._choices:
                snapshot.categorize_as(self._choices[s_id], forward_only)
                continue

            if s_id in self._context_diff.added:
                snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only)
            elif s_id.name in self._context_diff.modified_snapshots:
                self._categorize_snapshot(snapshot, forward_only, dag, indirectly_modified)

    def _categorize_snapshot(
        self,
        snapshot: Snapshot,
        forward_only: bool,
        dag: DAG[SnapshotId],
        indirectly_modified: SnapshotMapping,
    ) -> None:
        s_id = snapshot.snapshot_id

        if self._context_diff.directly_modified(s_id.name):
            if self._auto_categorization_enabled:
                new, old = self._context_diff.modified_snapshots[s_id.name]
                if is_breaking_kind_change(old, new):
                    snapshot.categorize_as(SnapshotChangeCategory.BREAKING, False)
                    return

                s_id_with_missing_columns: t.Optional[SnapshotId] = None
                this_sid_with_downstream = indirectly_modified.get(s_id, set()) | {s_id}
                for downstream_s_id in this_sid_with_downstream:
                    downstream_snapshot = self._context_diff.snapshots[downstream_s_id]
                    if (
                        downstream_snapshot.is_model
                        and downstream_snapshot.model.columns_to_types is None
                    ):
                        s_id_with_missing_columns = downstream_s_id
                        break

                if s_id_with_missing_columns is None:
                    change_category = categorize_change(new, old, config=self._categorizer_config)
                    if change_category is not None:
                        snapshot.categorize_as(change_category, forward_only)
                else:
                    mode = self._categorizer_config.dict().get(
                        new.model.source_type, AutoCategorizationMode.OFF
                    )
                    if mode == AutoCategorizationMode.FULL:
                        snapshot.categorize_as(SnapshotChangeCategory.BREAKING, forward_only)
        elif self._context_diff.indirectly_modified(snapshot.name):
            if snapshot.is_materialized_view and not forward_only:
                # We categorize changes as breaking to allow for instantaneous switches in a virtual layer.
                # Otherwise, there might be a potentially long downtime during MVs recreation.
                # In the case of forward-only changes this optimization is not applicable because we want to continue
                # using the same (existing) table version.
                snapshot.categorize_as(SnapshotChangeCategory.INDIRECT_BREAKING, forward_only)
                return

            all_upstream_forward_only = set()
            all_upstream_categories = set()
            direct_parent_categories = set()

            for p_id in dag.upstream(s_id):
                parent = self._context_diff.snapshots.get(p_id)

                if parent and self._is_new_snapshot(parent):
                    all_upstream_categories.add(parent.change_category)
                    all_upstream_forward_only.add(parent.is_forward_only)
                    if p_id in snapshot.parents:
                        direct_parent_categories.add(parent.change_category)

            if all_upstream_forward_only == {True} or (
                snapshot.is_model and snapshot.model.forward_only
            ):
                forward_only = True

            if direct_parent_categories.intersection(
                {SnapshotChangeCategory.BREAKING, SnapshotChangeCategory.INDIRECT_BREAKING}
            ):
                snapshot.categorize_as(SnapshotChangeCategory.INDIRECT_BREAKING, forward_only)
            elif not direct_parent_categories:
                snapshot.categorize_as(
                    self._get_orphaned_indirect_change_category(snapshot), forward_only
                )
            elif all_upstream_categories == {SnapshotChangeCategory.METADATA}:
                snapshot.categorize_as(SnapshotChangeCategory.METADATA, forward_only)
            else:
                snapshot.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING, forward_only)
        else:
            # Metadata updated.
            snapshot.categorize_as(SnapshotChangeCategory.METADATA, forward_only)

    def _get_orphaned_indirect_change_category(
        self, indirect_snapshot: Snapshot
    ) -> SnapshotChangeCategory:
        """Sometimes an indirectly changed downstream snapshot ends up with no directly changed parents introduced in the same plan.
        This may happen when 2 or more parent models were changed independently in different plans and then the changes were
        merged together and applied in a single plan. As a result, a combination of 2 or more previously changed parents produces
        a new downstream snapshot not previously seen.

        This function is used to infer the correct change category for such downstream snapshots based on change categories of their parents.
        """
        previous_snapshot = self._context_diff.modified_snapshots[indirect_snapshot.name][1]
        previous_parent_snapshot_ids = {p.name: p for p in previous_snapshot.parents}

        current_parent_snapshots = [
            self._context_diff.snapshots[p_id]
            for p_id in indirect_snapshot.parents
            if p_id in self._context_diff.snapshots
        ]

        indirect_category: t.Optional[SnapshotChangeCategory] = None
        for current_parent_snapshot in current_parent_snapshots:
            if current_parent_snapshot.name not in previous_parent_snapshot_ids:
                # This is a new parent so falling back to INDIRECT_BREAKING
                return SnapshotChangeCategory.INDIRECT_BREAKING
            pevious_parent_snapshot_id = previous_parent_snapshot_ids[current_parent_snapshot.name]

            if current_parent_snapshot.snapshot_id == pevious_parent_snapshot_id:
                # There were no new versions of this parent since the previous version of this snapshot,
                # so we can skip it
                continue

            # Find the previous snapshot ID of the same parent in the historical chain
            previous_parent_found = False
            previous_parent_categories = set()
            for pv in reversed(current_parent_snapshot.all_versions):
                pv_snapshot_id = pv.snapshot_id(current_parent_snapshot.name)
                if pv_snapshot_id == pevious_parent_snapshot_id:
                    previous_parent_found = True
                    break
                previous_parent_categories.add(pv.change_category)

            if not previous_parent_found:
                # The previous parent is not in the historical chain so falling back to INDIRECT_BREAKING
                return SnapshotChangeCategory.INDIRECT_BREAKING

            if previous_parent_categories.intersection(
                {SnapshotChangeCategory.BREAKING, SnapshotChangeCategory.INDIRECT_BREAKING}
            ):
                # One of the new parents in the chain was breaking so this indirect snapshot is breaking
                return SnapshotChangeCategory.INDIRECT_BREAKING

            if previous_parent_categories.intersection(
                {
                    SnapshotChangeCategory.NON_BREAKING,
                    SnapshotChangeCategory.INDIRECT_NON_BREAKING,
                }
            ):
                # All changes in the chain were non-breaking so this indirect snapshot can be non-breaking too
                indirect_category = SnapshotChangeCategory.INDIRECT_NON_BREAKING
            elif (
                previous_parent_categories == {SnapshotChangeCategory.METADATA}
                and indirect_category is None
            ):
                # All changes in the chain were metadata so this indirect snapshot can be metadata too
                indirect_category = SnapshotChangeCategory.METADATA

        return indirect_category or SnapshotChangeCategory.INDIRECT_BREAKING

    def _apply_effective_from(self) -> None:
        if self._effective_from:
            if not self._forward_only:
                raise PlanError("Effective date can only be set for a forward-only plan.")
            if to_datetime(self._effective_from) > now():
                raise PlanError("Effective date cannot be in the future.")

        for snapshot in self._context_diff.new_snapshots.values():
            if (
                snapshot.evaluatable
                and not snapshot.disable_restatement
                and (not snapshot.full_history_restatement_only or not snapshot.is_incremental)
            ):
                snapshot.effective_from = self._effective_from

    def _is_forward_only_change(self, s_id: SnapshotId) -> bool:
        if not self._context_diff.directly_modified(
            s_id.name
        ) and not self._context_diff.indirectly_modified(s_id.name):
            return False
        snapshot = self._context_diff.snapshots[s_id]
        if snapshot.name in self._context_diff.modified_snapshots:
            _, old = self._context_diff.modified_snapshots[snapshot.name]
            # If the model kind has changed in a breaking way, then we can't consider this to be a forward-only change.
            if snapshot.is_model and is_breaking_kind_change(old, snapshot):
                return False
        return (
            snapshot.is_model and snapshot.model.forward_only and bool(snapshot.previous_versions)
        )

    def _is_new_snapshot(self, snapshot: Snapshot) -> bool:
        """Returns True if the given snapshot is a new snapshot in this plan."""
        return snapshot.snapshot_id in self._context_diff.new_snapshots

    def _ensure_valid_date_range(self) -> None:
        if (self.override_start or self.override_end) and not self.is_start_and_end_allowed:
            raise PlanError(
                "The start and end dates can't be set for a production plan without restatements."
            )

        if (start := self.start) and (end := self.end):
            if to_datetime(start) > to_datetime(end):
                raise PlanError(
                    f"Plan end date: '{time_like_to_str(end)}' must be after the plan start date: '{time_like_to_str(start)}'"
                )

        if end := self.end:
            if to_datetime(end) > to_datetime(self.execution_time):
                raise PlanError(
                    f"Plan end date: '{time_like_to_str(end)}' cannot be in the future (execution time: '{time_like_to_str(self.execution_time)}')"
                )

        # Validate model-specific start/end dates
        if (start := self.start or self._default_start) and (end := self.end):
            start_ts = to_datetime(start)
            end_ts = to_datetime(end)
            if start_ts > end_ts:
                models_to_check: t.Set[str] = (
                    set(self._backfill_models or [])
                    | set(self._context_diff.modified_snapshots.keys())
                    | {s.name for s in self._context_diff.added}
                    | set((self._end_override_per_model or {}).keys())
                )
                for model_name in models_to_check:
                    if snapshot := self._model_fqn_to_snapshot.get(model_name):
                        if snapshot.node.start is None or to_datetime(snapshot.node.start) > end_ts:
                            raise PlanError(
                                f"Model '{model_name}': Start date / time '({time_like_to_str(start_ts)})' can't be greater than end date / time '({time_like_to_str(end_ts)})'.\n"
                                f"Set the `start` attribute in your project config model defaults to avoid this issue."
                            )

    def _ensure_no_broken_references(self) -> None:
        for snapshot in self._context_diff.snapshots.values():
            broken_references = {
                x.name for x in self._context_diff.removed_snapshots.values() if not x.is_external
            } & {x for x in snapshot.node.depends_on}
            if broken_references:
                broken_references_msg = ", ".join(f"'{x}'" for x in broken_references)
                raise PlanError(
                    f"""Removed {broken_references_msg} are referenced in '{snapshot.name}'. Please remove broken references before proceeding."""
                )

    def _ensure_new_env_with_changes(self) -> None:
        if (
            self._is_dev
            and not self._include_unmodified
            and self._context_diff.is_new_environment
            and not self._context_diff.has_snapshot_changes
            and not self._context_diff.has_environment_statements_changes
            and not self._backfill_models
        ):
            raise NoChangesPlanError(
                f"Creating a new environment requires a change, but project files match the `{self._context_diff.create_from}` environment. Make a change or use the --include-unmodified flag to create a new environment without changes."
            )

    @cached_property
    def _forward_only_preview_needed(self) -> bool:
        """Determines whether the plan should compute previews for forward-only changes (if there are any)."""
        return self._is_dev and (
            self._forward_only
            or (
                self._enable_preview
                and any(
                    snapshot.model.forward_only
                    for snapshot in self._modified_and_added_snapshots
                    if snapshot.is_model
                )
            )
        )

    @cached_property
    def _non_forward_only_preview_needed(self) -> bool:
        if not self._is_dev:
            return False
        for snapshot in self._modified_and_added_snapshots:
            if not snapshot.is_model:
                continue
            if (
                not snapshot.virtual_environment_mode.is_full
                or snapshot.model.auto_restatement_cron is not None
            ):
                return True
        return False

    @cached_property
    def _modified_and_added_snapshots(self) -> t.List[Snapshot]:
        return [
            snapshot
            for snapshot in self._context_diff.snapshots.values()
            if snapshot.name in self._context_diff.modified_snapshots
            or snapshot.snapshot_id in self._context_diff.added
        ]
