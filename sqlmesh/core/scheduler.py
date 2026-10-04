from __future__ import annotations
from dataclasses import dataclass
import abc
import logging
import threading
import typing as t
import time
from datetime import datetime
from sqlglot import exp
from sqlmesh.core import constants as c
from sqlmesh.core.console import Console, get_console
from sqlmesh.core.environment import EnvironmentNamingInfo, execute_environment_statements
from sqlmesh.core.macros import RuntimeStage
from sqlmesh.core.model.definition import AuditResult
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.notification_target import (
    NotificationEvent,
    NotificationTargetManager,
)
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotId,
    SnapshotIdBatch,
    SnapshotEvaluator,
    apply_auto_restatements,
    earliest_start_date,
    missing_intervals,
    merge_intervals,
    snapshots_to_dag,
    Intervals,
)
from sqlmesh.core.snapshot.definition import check_ready_intervals
from sqlmesh.core.snapshot.definition import (
    Interval,
    expand_range,
    parent_snapshots_by_name,
)
from sqlmesh.core.state_sync import StateSync
from sqlmesh.utils import CompletionStatus
from sqlmesh.utils.concurrency import (
    concurrent_apply_to_dag,
    concurrent_apply_to_values,
    NodeExecutionFailedError,
)
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import (
    TimeLike,
    now_timestamp,
    validate_date_range,
)
from sqlmesh.utils.errors import (
    AuditError,
    NodeAuditsErrors,
    CircuitBreakerError,
    SQLMeshError,
    SignalEvalError,
)

if t.TYPE_CHECKING:
    from sqlmesh.core.context import ExecutionContext

logger = logging.getLogger(__name__)
SnapshotToIntervals = t.Dict[Snapshot, Intervals]


class SchedulingUnit(abc.ABC):
    snapshot_name: str

    def __lt__(self, other: SchedulingUnit) -> bool:
        return (self.__class__.__name__, self.snapshot_name) < (
            other.__class__.__name__,
            other.snapshot_name,
        )


@dataclass(frozen=True)
class EvaluateNode(SchedulingUnit):
    snapshot_name: str
    interval: Interval
    batch_index: int

    def __lt__(self, other: SchedulingUnit) -> bool:
        if not isinstance(other, EvaluateNode):
            return super().__lt__(other)
        return (self.__class__.__name__, self.snapshot_name, self.interval, self.batch_index) < (
            other.__class__.__name__,
            other.snapshot_name,
            other.interval,
            other.batch_index,
        )


@dataclass(frozen=True)
class CreateNode(SchedulingUnit):
    snapshot_name: str


@dataclass(frozen=True)
class DummyNode(SchedulingUnit):
    snapshot_name: str


class Scheduler:
    """Schedules and manages the evaluation of snapshots.

    The scheduler evaluates multiple snapshots with date intervals in the correct
    topological order. It consults the state sync to understand what intervals for each
    snapshot needs to be backfilled.

    The scheduler comes equipped with a simple ThreadPoolExecutor based evaluation engine.

    Args:
        snapshots: A collection of snapshots.
        snapshot_evaluator: The snapshot evaluator to execute queries.
        state_sync: The state sync to pull saved snapshots.
        max_workers: The maximum number of parallel queries to run.
        console: The rich instance used for printing scheduling information.
    """

    def __init__(
        self,
        snapshots: t.Iterable[Snapshot],
        snapshot_evaluator: SnapshotEvaluator,
        state_sync: StateSync,
        default_catalog: t.Optional[str],
        max_workers: int = 1,
        console: t.Optional[Console] = None,
        notification_target_manager: t.Optional[NotificationTargetManager] = None,
    ):
        self.state_sync = state_sync
        self.snapshots = {s.snapshot_id: s for s in snapshots}
        self.snapshots_by_name = {snapshot.name: snapshot for snapshot in self.snapshots.values()}
        self.snapshot_per_version = _resolve_one_snapshot_per_version(self.snapshots.values())
        self.default_catalog = default_catalog
        self.snapshot_evaluator = snapshot_evaluator
        self.max_workers = max_workers
        self.console = console or get_console()
        self.notification_target_manager = (
            notification_target_manager or NotificationTargetManager()
        )

    def merged_missing_intervals(
        self,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        restatements: t.Optional[t.Dict[SnapshotId, Interval]] = None,
        start_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        end_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        ignore_cron: bool = False,
        end_bounded: bool = False,
        selected_snapshots: t.Optional[t.Set[str]] = None,
    ) -> SnapshotToIntervals:
        """Find the largest contiguous date interval parameters based only on what is missing.

        For each node name, find all dependencies and look for a stored snapshot from the metastore. If a snapshot is found,
        calculate the missing intervals that need to be processed given the passed in start and end intervals.

        This is a superset of what may actually get processed at runtime based on things like batch size, signal readiness, etc.

        Args:
            start: The start of the run. Defaults to the min node start date.
            end: The end of the run. Defaults to now.
            execution_time: The date/time reference to use for execution time. Defaults to now.
            deployability_index: Determines snapshots that are deployable in the context of this evaluation.
            restatements: A set of snapshot names being restated.
            start_override_per_model: A mapping of model FQNs to target start dates.
            end_override_per_model: A mapping of model FQNs to target end dates.
            ignore_cron: Whether to ignore the node's cron schedule.
            end_bounded: If set to true, the returned intervals will be bounded by the target end date, disregarding lookback,
                allow_partials, and other attributes that could cause the intervals to exceed the target end date.
            selected_snapshots: A set of snapshot names to run. If not provided, all snapshots will be run.
        """
        snapshots_to_intervals = merged_missing_intervals(
            snapshots=self.snapshot_per_version.values(),
            start=start,
            end=end,
            execution_time=execution_time,
            deployability_index=deployability_index,
            restatements=restatements,
            start_override_per_model=start_override_per_model,
            end_override_per_model=end_override_per_model,
            ignore_cron=ignore_cron,
            end_bounded=end_bounded,
        )
        # Filtering snapshots after computing missing intervals because we need all snapshots in order
        # to correctly infer start dates.
        if selected_snapshots is not None:
            snapshots_to_intervals = {
                s: i for s, i in snapshots_to_intervals.items() if s.name in selected_snapshots
            }
        return snapshots_to_intervals

    def evaluate(
        self,
        snapshot: Snapshot,
        start: TimeLike,
        end: TimeLike,
        execution_time: TimeLike,
        deployability_index: DeployabilityIndex,
        batch_index: int,
        environment_naming_info: t.Optional[EnvironmentNamingInfo] = None,
        allow_destructive_snapshots: t.Optional[t.Set[str]] = None,
        allow_additive_snapshots: t.Optional[t.Set[str]] = None,
        target_table_exists: t.Optional[bool] = None,
        **kwargs: t.Any,
    ) -> t.List[AuditResult]:
        """Evaluate a snapshot and add the processed interval to the state sync.

        Args:
            snapshot: Snapshot to evaluate.
            start: The start datetime to render.
            end: The end datetime to render.
            execution_time: The date/time time reference to use for execution time. Defaults to now.
            allow_destructive_snapshots: Snapshots for which destructive schema changes are allowed.
            allow_additive_snapshots: Snapshots for which additive schema changes are allowed.
            deployability_index: Determines snapshots that are deployable in the context of this evaluation.
            batch_index: If the snapshot is part of a batch of related snapshots; which index in the batch is it
            auto_restatement_enabled: Whether to enable auto restatements.
            target_table_exists: Whether the target table exists. If None, the table will be checked for existence.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            Tuple of list of all audit results from the evaluation and list of non-blocking audit errors to warn.
        """
        validate_date_range(start, end)

        snapshots = parent_snapshots_by_name(snapshot, self.snapshots)

        is_deployable = deployability_index.is_deployable(snapshot)

        wap_id = self.snapshot_evaluator.evaluate(
            snapshot,
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            allow_destructive_snapshots=allow_destructive_snapshots,
            allow_additive_snapshots=allow_additive_snapshots,
            deployability_index=deployability_index,
            batch_index=batch_index,
            target_table_exists=target_table_exists,
            **kwargs,
        )
        audit_results = self._audit_snapshot(
            snapshot=snapshot,
            environment_naming_info=environment_naming_info,
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            deployability_index=deployability_index,
            wap_id=wap_id,
            **kwargs,
        )

        self.state_sync.add_interval(
            snapshot, start, end, is_dev=not is_deployable, last_altered_ts=now_timestamp()
        )
        return audit_results

    def run(
        self,
        environment: str | EnvironmentNamingInfo,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        restatements: t.Optional[t.Dict[SnapshotId, Interval]] = None,
        start_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        end_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        ignore_cron: bool = False,
        end_bounded: bool = False,
        selected_snapshots: t.Optional[t.Set[str]] = None,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        auto_restatement_enabled: bool = False,
        run_environment_statements: bool = False,
    ) -> CompletionStatus:
        return self._run_or_audit(
            environment=environment,
            start=start,
            end=end,
            execution_time=execution_time,
            remove_intervals=restatements,
            start_override_per_model=start_override_per_model,
            end_override_per_model=end_override_per_model,
            ignore_cron=ignore_cron,
            end_bounded=end_bounded,
            selected_snapshots=selected_snapshots,
            circuit_breaker=circuit_breaker,
            deployability_index=deployability_index,
            auto_restatement_enabled=auto_restatement_enabled,
            run_environment_statements=run_environment_statements,
        )

    def audit(
        self,
        environment: str | EnvironmentNamingInfo,
        start: TimeLike,
        end: TimeLike,
        execution_time: t.Optional[TimeLike] = None,
        start_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        end_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        ignore_cron: bool = False,
        end_bounded: bool = False,
        selected_snapshots: t.Optional[t.Set[str]] = None,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        run_environment_statements: bool = False,
    ) -> CompletionStatus:
        # Remove the intervals from the snapshots that will be audited so that they can be recomputed
        # by _run_or_audit as "missing intervals" to reuse the rest of it's logic
        remove_intervals = {}
        for snapshot in self.snapshots.values():
            removal_intervals = snapshot.get_removal_interval(
                start, end, execution_time, is_preview=True
            )
            remove_intervals[snapshot.snapshot_id] = removal_intervals

        return self._run_or_audit(
            environment=environment,
            start=start,
            end=end,
            execution_time=execution_time,
            remove_intervals=remove_intervals,
            start_override_per_model=start_override_per_model,
            end_override_per_model=end_override_per_model,
            ignore_cron=ignore_cron,
            end_bounded=end_bounded,
            selected_snapshots=selected_snapshots,
            circuit_breaker=circuit_breaker,
            deployability_index=deployability_index,
            run_environment_statements=run_environment_statements,
            audit_only=True,
        )

    def batch_intervals(
        self,
        merged_intervals: SnapshotToIntervals,
        deployability_index: t.Optional[DeployabilityIndex],
        environment_naming_info: EnvironmentNamingInfo,
        dag: t.Optional[DAG[SnapshotId]] = None,
        is_restatement: bool = False,
    ) -> t.Dict[Snapshot, Intervals]:
        dag = dag or snapshots_to_dag(merged_intervals)

        snapshot_intervals: t.Dict[SnapshotId, t.Tuple[Snapshot, t.List[Interval]]] = {
            snapshot.snapshot_id: (
                snapshot,
                [
                    i
                    for interval in intervals
                    for i in _expand_range_as_interval(*interval, snapshot.node.interval_unit)
                ],
            )
            for snapshot, intervals in merged_intervals.items()
        }
        snapshot_batches: t.Dict[Snapshot, Intervals] = {}
        all_unready_intervals: t.Dict[str, set[Interval]] = {}
        for snapshot_id in dag:
            if snapshot_id not in snapshot_intervals:
                continue
            snapshot, intervals = snapshot_intervals[snapshot_id]
            unready = set(intervals)

            from sqlmesh.core.context import ExecutionContext

            adapter = self.snapshot_evaluator.get_adapter(snapshot.model_gateway)

            parent_intervals: Intervals = []
            for parent_id in snapshot.parents:
                parent_snapshot, _ = snapshot_intervals.get(parent_id, (None, []))
                if not parent_snapshot or parent_snapshot.is_external:
                    continue

                parent_intervals.extend(snapshot_batches[parent_snapshot])

            context = ExecutionContext(
                adapter,
                self.snapshots_by_name,
                deployability_index,
                default_dialect=adapter.dialect,
                default_catalog=self.default_catalog,
                is_restatement=is_restatement,
                parent_intervals=parent_intervals,
            )

            intervals = self._check_ready_intervals(
                snapshot,
                intervals,
                context,
                environment_naming_info,
            )
            unready -= set(intervals)

            for parent in snapshot.parents:
                if parent.name in all_unready_intervals:
                    unready.update(all_unready_intervals[parent.name])

            all_unready_intervals[snapshot.name] = unready

            batches = []
            batch_size = snapshot.node.batch_size
            next_batch: t.List[t.Tuple[int, int]] = []

            for interval in interval_diff(
                intervals, merge_intervals(unready), uninterrupted=snapshot.depends_on_past
            ):
                if (batch_size and len(next_batch) >= batch_size) or (
                    next_batch and interval[0] != next_batch[-1][-1]
                ):
                    batches.append((next_batch[0][0], next_batch[-1][-1]))
                    next_batch = []

                next_batch.append(interval)

            if next_batch:
                batches.append((next_batch[0][0], next_batch[-1][-1]))

            snapshot_batches[snapshot] = batches

        return snapshot_batches

    def run_merged_intervals(
        self,
        *,
        merged_intervals: SnapshotToIntervals,
        deployability_index: DeployabilityIndex,
        environment_naming_info: EnvironmentNamingInfo,
        execution_time: t.Optional[TimeLike] = None,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        allow_destructive_snapshots: t.Optional[t.Set[str]] = None,
        selected_models: t.Optional[t.Set[str]] = None,
        allow_additive_snapshots: t.Optional[t.Set[str]] = None,
        selected_snapshot_ids: t.Optional[t.Set[SnapshotId]] = None,
        run_environment_statements: bool = False,
        audit_only: bool = False,
        auto_restatement_triggers: t.Dict[SnapshotId, t.List[SnapshotId]] = {},
        is_restatement: bool = False,
    ) -> t.Tuple[t.List[NodeExecutionFailedError[SchedulingUnit]], t.List[SchedulingUnit]]:
        """Runs precomputed batches of missing intervals.

        Args:
            merged_intervals: The snapshots and contiguous interval ranges to evaluate.
            deployability_index: Determines snapshots that are deployable in the context of this evaluation.
            environment_naming_info: The environment naming info the user is targeting when applying their change.
            execution_time: The date/time reference to use for execution time.
            circuit_breaker: An optional handler which checks if the run should be aborted.
            start: The start of the run.
            end: The end of the run.
            allow_destructive_snapshots: Snapshots for which destructive schema changes are allowed.
            allow_additive_snapshots: Snapshots for which additive schema changes are allowed.
            selected_snapshot_ids: The snapshots to include in the run DAG. If None, all snapshots with missing intervals will be included.

        Returns:
            A tuple of errors and skipped intervals.
        """
        execution_time = execution_time or now_timestamp()

        selected_snapshots = [self.snapshots[sid] for sid in (selected_snapshot_ids or set())]
        if not selected_snapshots:
            selected_snapshots = list(merged_intervals)

        # Build the full DAG from all snapshots to preserve transitive dependencies
        full_dag = snapshots_to_dag(self.snapshots.values())

        # Create a subdag that includes the selected snapshots and all their upstream dependencies
        # This ensures that transitive dependencies are preserved even when intermediate nodes are not selected
        selected_snapshot_ids_set = {s.snapshot_id for s in selected_snapshots}
        snapshot_dag = full_dag.subdag(*selected_snapshot_ids_set)

        batched_intervals = self.batch_intervals(
            merged_intervals,
            deployability_index,
            environment_naming_info,
            dag=snapshot_dag,
            is_restatement=is_restatement,
        )
        self.console.start_evaluation_progress(
            batched_intervals,
            environment_naming_info,
            self.default_catalog,
            audit_only=audit_only,
        )

        if run_environment_statements:
            environment_statements = self.state_sync.get_environment_statements(
                environment_naming_info.name
            )
            execute_environment_statements(
                adapter=self.snapshot_evaluator.adapter,
                environment_statements=environment_statements,
                runtime_stage=RuntimeStage.BEFORE_ALL,
                environment_naming_info=environment_naming_info,
                default_catalog=self.default_catalog,
                snapshots=self.snapshots_by_name,
                start=start,
                end=end,
                execution_time=execution_time,
                selected_models=selected_models,
            )

        try:
            with self.snapshot_evaluator.concurrent_context():
                # We only need to create physical tables if the snapshot is not representative
                # or if it needs backfill.
                snapshots_to_create_candidates = [
                    s
                    for s in selected_snapshots
                    if not deployability_index.is_representative(s) or s in batched_intervals
                ]
                snapshots_to_create = {
                    s.snapshot_id
                    for s in self.snapshot_evaluator.get_snapshots_to_create(
                        snapshots_to_create_candidates, deployability_index
                    )
                }

                dag = self._dag(
                    batched_intervals,
                    snapshot_dag=snapshot_dag,
                    snapshots_to_create=snapshots_to_create,
                )

                if audit_only:
                    errors, skipped_intervals = self._run_audits_concurrently(
                        batched_intervals=batched_intervals,
                        dag=dag,
                        deployability_index=deployability_index,
                        environment_naming_info=environment_naming_info,
                        execution_time=execution_time,
                        circuit_breaker=circuit_breaker,
                        auto_restatement_triggers=auto_restatement_triggers,
                        allow_destructive_snapshots=allow_destructive_snapshots,
                        allow_additive_snapshots=allow_additive_snapshots,
                    )
                else:
                    def run_node(node: SchedulingUnit) -> None:
                        if circuit_breaker and circuit_breaker():
                            raise CircuitBreakerError()
                        if isinstance(node, DummyNode):
                            return

                        snapshot = self.snapshots_by_name[node.snapshot_name]

                        if isinstance(node, EvaluateNode):
                            assert execution_time  # mypy
                            assert deployability_index  # mypy
                            node_start, node_end = node.interval

                            # If batch_index > 0, then the target table must exist since the first batch would have created it
                            target_table_exists = (
                                snapshot.snapshot_id not in snapshots_to_create
                                or node.batch_index > 0
                            )

                            def _do_evaluate() -> t.List[AuditResult]:
                                return self.evaluate(
                                    snapshot=snapshot,
                                    environment_naming_info=environment_naming_info,
                                    start=node_start,
                                    end=node_end,
                                    execution_time=execution_time,
                                    deployability_index=deployability_index,
                                    batch_index=node.batch_index,
                                    allow_destructive_snapshots=allow_destructive_snapshots,
                                    allow_additive_snapshots=allow_additive_snapshots,
                                    target_table_exists=target_table_exists,
                                    selected_models=selected_models,
                                )

                            self._run_node_with_progress(
                                snapshot=snapshot,
                                node=node,
                                batched_intervals=batched_intervals,
                                auto_restatement_triggers=auto_restatement_triggers,
                                work_fn=_do_evaluate,
                            )
                        elif isinstance(node, CreateNode):
                            self.snapshot_evaluator.create_snapshot(
                                snapshot=snapshot,
                                snapshots=self.snapshots_by_name,
                                deployability_index=deployability_index,
                                allow_destructive_snapshots=allow_destructive_snapshots or set(),
                                allow_additive_snapshots=allow_additive_snapshots or set(),
                            )

                    errors, skipped_intervals = concurrent_apply_to_dag(
                        dag,
                        run_node,
                        self.max_workers,
                        raise_on_error=False,
                    )

                self.console.stop_evaluation_progress(success=not errors)

                skipped_snapshots = {
                    i.snapshot_name for i in skipped_intervals if isinstance(i, EvaluateNode)
                }
                self.console.log_skipped_models(skipped_snapshots)
                for skipped in skipped_snapshots:
                    logger.info(f"SKIPPED snapshot {skipped}\n")

                for error in errors:
                    if isinstance(error.__cause__, CircuitBreakerError):
                        raise error.__cause__
                    logger.info(str(error), exc_info=error)

                self.console.log_failed_models(errors)

                return errors, skipped_intervals
        finally:
            if run_environment_statements:
                execute_environment_statements(
                    adapter=self.snapshot_evaluator.adapter,
                    environment_statements=environment_statements,
                    runtime_stage=RuntimeStage.AFTER_ALL,
                    environment_naming_info=environment_naming_info,
                    default_catalog=self.default_catalog,
                    snapshots=self.snapshots_by_name,
                    start=start,
                    end=end,
                    execution_time=execution_time,
                    selected_models=selected_models,
                )

            self.state_sync.recycle()

    def _dag(
        self,
        batches: SnapshotToIntervals,
        snapshot_dag: t.Optional[DAG[SnapshotId]] = None,
        snapshots_to_create: t.Optional[t.Set[SnapshotId]] = None,
    ) -> DAG[SchedulingUnit]:
        """Builds a DAG of snapshot intervals to be evaluated.

        Args:
            batches: The batches of snapshots and intervals to evaluate.
            snapshot_dag: The DAG of all snapshots.
            snapshots_to_create: The snapshots with missing physical tables.

        Returns:
            A DAG of snapshot intervals to be evaluated.
        """

        intervals_per_snapshot = {
            snapshot.name: intervals for snapshot, intervals in batches.items()
        }
        snapshots_to_create = snapshots_to_create or set()
        original_snapshots_to_create = snapshots_to_create.copy()
        upstream_dependencies_cache: t.Dict[SnapshotId, t.Set[SchedulingUnit]] = {}

        snapshot_dag = snapshot_dag or snapshots_to_dag(batches)
        dag = DAG[SchedulingUnit]()

        for snapshot_id in snapshot_dag:
            if snapshot_id.name not in self.snapshots_by_name:
                continue

            snapshot = self.snapshots_by_name[snapshot_id.name]
            intervals = intervals_per_snapshot.get(snapshot.name, [])

            upstream_dependencies: t.Set[SchedulingUnit] = set()

            for p_sid in snapshot.parents:
                upstream_dependencies.update(
                    self._find_upstream_dependencies(
                        p_sid,
                        intervals_per_snapshot,
                        original_snapshots_to_create,
                        upstream_dependencies_cache,
                    )
                )

            batch_concurrency = snapshot.node.batch_concurrency
            batch_size = snapshot.node.batch_size
            if snapshot.depends_on_past:
                batch_concurrency = 1

            create_node: t.Optional[CreateNode] = None
            if snapshot.snapshot_id in original_snapshots_to_create and (
                snapshot.is_incremental_by_time_range
                or ((not batch_concurrency or batch_concurrency > 1) and batch_size)
                or not intervals
            ):
                # Add a separate node for table creation in case when there multiple concurrent
                # evaluation nodes or when there are no intervals to evaluate.
                create_node = CreateNode(snapshot_name=snapshot.name)
                dag.add(create_node, upstream_dependencies)
                snapshots_to_create.remove(snapshot.snapshot_id)

            for i, interval in enumerate(intervals):
                node = EvaluateNode(snapshot_name=snapshot.name, interval=interval, batch_index=i)

                if create_node:
                    dag.add(node, [create_node])
                else:
                    dag.add(node, upstream_dependencies)

                if len(intervals) > 1:
                    dag.add(DummyNode(snapshot_name=snapshot.name), [node])

                if batch_concurrency and i >= batch_concurrency:
                    batch_idx_to_wait_for = i - batch_concurrency
                    dag.add(
                        node,
                        [
                            EvaluateNode(
                                snapshot_name=snapshot.name,
                                interval=intervals[batch_idx_to_wait_for],
                                batch_index=batch_idx_to_wait_for,
                            ),
                        ],
                    )
        return dag

    def _find_upstream_dependencies(
        self,
        parent_sid: SnapshotId,
        intervals_per_snapshot: t.Dict[str, Intervals],
        snapshots_to_create: t.Set[SnapshotId],
        cache: t.Dict[SnapshotId, t.Set[SchedulingUnit]],
    ) -> t.Set[SchedulingUnit]:
        if parent_sid not in self.snapshots:
            return set()
        if parent_sid in cache:
            return cache[parent_sid]

        p_intervals = intervals_per_snapshot.get(parent_sid.name, [])

        parent_node: t.Optional[SchedulingUnit] = None
        if p_intervals:
            if len(p_intervals) > 1:
                parent_node = DummyNode(snapshot_name=parent_sid.name)
            else:
                interval = p_intervals[0]
                parent_node = EvaluateNode(
                    snapshot_name=parent_sid.name, interval=interval, batch_index=0
                )
        elif parent_sid in snapshots_to_create:
            parent_node = CreateNode(snapshot_name=parent_sid.name)

        if parent_node is not None:
            cache[parent_sid] = {parent_node}
            return {parent_node}

        # This snapshot has no intervals and doesn't need creation which means
        # that it can be a transitive dependency
        transitive_deps: t.Set[SchedulingUnit] = set()
        parent_snapshot = self.snapshots[parent_sid]
        for grandparent_sid in parent_snapshot.parents:
            transitive_deps.update(
                self._find_upstream_dependencies(
                    grandparent_sid, intervals_per_snapshot, snapshots_to_create, cache
                )
            )
        cache[parent_sid] = transitive_deps
        return transitive_deps

    def _run_or_audit(
        self,
        environment: str | EnvironmentNamingInfo,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        remove_intervals: t.Optional[t.Dict[SnapshotId, Interval]] = None,
        start_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        end_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
        ignore_cron: bool = False,
        end_bounded: bool = False,
        selected_snapshots: t.Optional[t.Set[str]] = None,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        auto_restatement_enabled: bool = False,
        run_environment_statements: bool = False,
        audit_only: bool = False,
    ) -> CompletionStatus:
        """Concurrently runs or audits all snapshots in topological order.

        Args:
            environment: The environment naming info the user is targeting when applying their change.
                Can just be the environment name if the user is targeting a remote environment and wants to get the remote
                naming info
            start: The start of the run. Defaults to the min node start date.
            end: The end of the run. Defaults to now.
            execution_time: The date/time time reference to use for execution time. Defaults to now.
            remove_intervals: A dict of snapshots to their intervals. For evaluation, these are the intervals that will be restated. For audits,
                              these are the intervals that will be reaudited
            start_override_per_model: A mapping of model FQNs to target start dates.
            end_override_per_model: A mapping of model FQNs to target end dates.
            ignore_cron: Whether to ignore the node's cron schedule.
            end_bounded: If set to true, the evaluated intervals will be bounded by the target end date, disregarding lookback,
                allow_partials, and other attributes that could cause the intervals to exceed the target end date.
            selected_snapshots: A set of snapshot names to run. If not provided, all snapshots will be run.
            circuit_breaker: An optional handler which checks if the run should be aborted.
            deployability_index: Determines snapshots that are deployable in the context of this render.
            auto_restatement_enabled: Whether to enable auto restatements.

        Returns:
            True if the execution was successful and False otherwise.
        """
        validate_date_range(start, end)
        if isinstance(environment, str):
            env = self.state_sync.get_environment(environment)
            if not env:
                raise SQLMeshError(
                    "Was not provided an environment suffix target and the environment doesn't exist."
                    "Are you running for the first time and need to run plan/apply first?"
                )
            environment_naming_info = env.naming_info
        else:
            environment_naming_info = environment

        deployability_index = deployability_index or (
            DeployabilityIndex.create(self.snapshots.values(), start=start)
            if environment_naming_info.name != c.PROD
            else DeployabilityIndex.all_deployable()
        )
        execution_time = execution_time or now_timestamp()

        self.state_sync.refresh_snapshot_intervals(self.snapshots.values())
        for s_id, interval in (remove_intervals or {}).items():
            self.snapshots[s_id].remove_interval(interval)

        all_auto_restatement_triggers: t.Dict[SnapshotId, t.List[SnapshotId]] = {}
        if auto_restatement_enabled:
            auto_restated_intervals, all_auto_restatement_triggers = apply_auto_restatements(
                self.snapshots, execution_time
            )
            self.state_sync.add_snapshots_intervals(auto_restated_intervals)
            self.state_sync.update_auto_restatements(
                {s.name_version: s.next_auto_restatement_ts for s in self.snapshots.values()}
            )

        merged_intervals = self.merged_missing_intervals(
            start,
            end,
            execution_time,
            deployability_index=deployability_index,
            restatements=remove_intervals,
            start_override_per_model=start_override_per_model,
            end_override_per_model=end_override_per_model,
            ignore_cron=ignore_cron,
            end_bounded=end_bounded,
            selected_snapshots=selected_snapshots,
        )
        if not merged_intervals:
            return CompletionStatus.NOTHING_TO_DO

        auto_restatement_triggers: t.Dict[SnapshotId, t.List[SnapshotId]] = {}
        if all_auto_restatement_triggers:
            merged_intervals_snapshots = {snapshot.snapshot_id for snapshot in merged_intervals}
            auto_restatement_triggers = {
                s_id: all_auto_restatement_triggers.get(s_id, [])
                for s_id in merged_intervals_snapshots
            }

        errors, _ = self.run_merged_intervals(
            merged_intervals=merged_intervals,
            deployability_index=deployability_index,
            environment_naming_info=environment_naming_info,
            execution_time=execution_time,
            circuit_breaker=circuit_breaker,
            start=start,
            end=end,
            run_environment_statements=run_environment_statements,
            audit_only=audit_only,
            auto_restatement_triggers=auto_restatement_triggers,
            selected_models={
                s.node.dbt_unique_id for s in merged_intervals if s.node.dbt_unique_id
            },
        )

        return CompletionStatus.FAILURE if errors else CompletionStatus.SUCCESS

    def _audit_snapshot(
        self,
        snapshot: Snapshot,
        deployability_index: DeployabilityIndex,
        snapshots: t.Dict[str, Snapshot],
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        wap_id: t.Optional[str] = None,
        environment_naming_info: t.Optional[EnvironmentNamingInfo] = None,
        **kwargs: t.Any,
    ) -> t.List[AuditResult]:
        is_deployable = deployability_index.is_deployable(snapshot)

        audit_results = self.snapshot_evaluator.audit(
            snapshot=snapshot,
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            deployability_index=deployability_index,
            wap_id=wap_id,
            **kwargs,
        )

        audit_errors_to_raise: t.List[AuditError] = []
        audit_errors_to_warn: t.List[AuditError] = []
        for audit_result in (result for result in audit_results if result.count):
            error = AuditError(
                audit_name=audit_result.audit.name,
                audit_args=audit_result.audit_args,
                model=snapshot.model_or_none,
                count=t.cast(int, audit_result.count),
                query=t.cast(exp.Query, audit_result.query),
                adapter_dialect=self.snapshot_evaluator.adapter.dialect,
            )
            self.notification_target_manager.notify(NotificationEvent.AUDIT_FAILURE, error)
            if is_deployable and snapshot.node.owner:
                self.notification_target_manager.notify_user(
                    NotificationEvent.AUDIT_FAILURE, snapshot.node.owner, error
                )
            if audit_result.blocking:
                audit_errors_to_raise.append(error)
            else:
                audit_errors_to_warn.append(error)

        if audit_errors_to_raise:
            raise NodeAuditsErrors(audit_errors_to_raise)

        if environment_naming_info:
            for audit_error in audit_errors_to_warn:
                display_name = snapshot.display_name(
                    environment_naming_info,
                    self.default_catalog,
                    self.snapshot_evaluator.adapter.dialect,
                )
                self.console.log_warning(
                    f"\n{display_name}: {audit_error}.",
                    f"{audit_error}. Audit query:\n{audit_error.query.sql(audit_error.adapter_dialect)}",
                )

        return audit_results

    def _run_node_with_progress(
        self,
        *,
        snapshot: Snapshot,
        node: EvaluateNode,
        batched_intervals: t.Dict[Snapshot, Intervals],
        auto_restatement_triggers: t.Dict[SnapshotId, t.List[SnapshotId]],
        work_fn: t.Callable[[], t.List[AuditResult]],
    ) -> None:
        """Runs a work function for a node while tracking progress and audit results.

        Args:
            snapshot: The snapshot being processed.
            node: The evaluate node.
            batched_intervals: The batched intervals per snapshot.
            auto_restatement_triggers: Auto restatement trigger info per snapshot.
            work_fn: A callable that performs the actual work and returns audit results.
        """
        self.console.start_snapshot_evaluation_progress(snapshot)
        execution_start_ts = now_timestamp()
        evaluation_duration_ms: t.Optional[int] = None

        audit_results: t.List[AuditResult] = []
        try:
            audit_results = work_fn()
            evaluation_duration_ms = now_timestamp() - execution_start_ts
        finally:
            num_audits = len(audit_results)
            num_audits_failed = sum(1 for result in audit_results if result.count)

            execution_stats = self.snapshot_evaluator.execution_tracker.get_execution_stats(
                SnapshotIdBatch(snapshot_id=snapshot.snapshot_id, batch_id=node.batch_index)
            )

            self.console.update_snapshot_evaluation_progress(
                snapshot,
                batched_intervals[snapshot][node.batch_index],
                node.batch_index,
                evaluation_duration_ms,
                num_audits - num_audits_failed,
                num_audits_failed,
                execution_stats=execution_stats,
                auto_restatement_triggers=auto_restatement_triggers.get(snapshot.snapshot_id),
            )

    def _run_audits_concurrently(
        self,
        *,
        batched_intervals: t.Dict[Snapshot, Intervals],
        dag: DAG[SchedulingUnit],
        deployability_index: DeployabilityIndex,
        environment_naming_info: EnvironmentNamingInfo,
        execution_time: TimeLike,
        circuit_breaker: t.Optional[t.Callable[[], bool]],
        auto_restatement_triggers: t.Dict[SnapshotId, t.List[SnapshotId]],
        allow_destructive_snapshots: t.Optional[t.Set[str]],
        allow_additive_snapshots: t.Optional[t.Set[str]],
    ) -> t.Tuple[t.List[NodeExecutionFailedError[SchedulingUnit]], t.List[SchedulingUnit]]:
        """Runs all audits across all snapshots in a single flat thread pool.

        Audits are read-only SELECT queries with no side effects, so they can safely
        run concurrently even across snapshots that have DAG dependencies. This fills
        all concurrent_tasks slots at once instead of processing level-by-level as the
        DAG executor would.

        Args:
            batched_intervals: The batched intervals to audit per snapshot.
            dag: The scheduling DAG for the selected snapshots.
            deployability_index: Determines snapshots that are deployable.
            environment_naming_info: The environment naming info.
            execution_time: The date/time reference to use for execution time.
            circuit_breaker: An optional handler which checks if the run should be aborted.
            auto_restatement_triggers: Auto restatement trigger info per snapshot.

        Returns:
            A tuple of errors and skipped intervals.
        """
        errors: t.List[NodeExecutionFailedError[SchedulingUnit]] = []
        skipped_intervals: t.List[SchedulingUnit] = []
        blocked_audit_nodes: t.Set[EvaluateNode] = set()

        create_nodes = [node for node in dag if isinstance(node, CreateNode)]

        if create_nodes:
            create_dag = dag.prune(*create_nodes)

            resolved_destructive = allow_destructive_snapshots or set()
            resolved_additive = allow_additive_snapshots or set()

            def run_create_node(node: SchedulingUnit) -> None:
                if circuit_breaker and circuit_breaker():
                    raise CircuitBreakerError()

                assert isinstance(node, CreateNode)
                snapshot = self.snapshots_by_name[node.snapshot_name]
                self.snapshot_evaluator.create_snapshot(
                    snapshot=snapshot,
                    snapshots=self.snapshots_by_name,
                    deployability_index=deployability_index,
                    allow_destructive_snapshots=resolved_destructive,
                    allow_additive_snapshots=resolved_additive,
                )

            create_errors, skipped_create_nodes = concurrent_apply_to_dag(
                create_dag,
                run_create_node,
                self.max_workers,
                raise_on_error=False,
            )
            errors.extend(create_errors)
            skipped_intervals.extend(skipped_create_nodes)

            for error in create_errors:
                if isinstance(error.__cause__, CircuitBreakerError):
                    raise error.__cause__

            failed_or_skipped_create_nodes = {error.node for error in create_errors}
            failed_or_skipped_create_nodes.update(skipped_create_nodes)

            if failed_or_skipped_create_nodes:
                blocked_audit_nodes = {
                    node
                    for create_node in failed_or_skipped_create_nodes
                    for node in dag.downstream(create_node)
                    if isinstance(node, EvaluateNode)
                }
                skipped_intervals.extend(blocked_audit_nodes)

        if blocked_audit_nodes:
            batched_intervals = {
                snapshot: [
                    interval
                    for batch_index, interval in enumerate(intervals)
                    if EvaluateNode(
                        snapshot_name=snapshot.name,
                        interval=interval,
                        batch_index=batch_index,
                    )
                    not in blocked_audit_nodes
                ]
                for snapshot, intervals in batched_intervals.items()
            }
            batched_intervals = {
                snapshot: intervals for snapshot, intervals in batched_intervals.items() if intervals
            }

        # Flatten all (snapshot, interval, batch_index) tasks across all snapshots
        audit_tasks: t.List[EvaluateNode] = [
            EvaluateNode(snapshot_name=snapshot.name, interval=interval, batch_index=batch_index)
            for snapshot, intervals in batched_intervals.items()
            for batch_index, interval in enumerate(intervals)
        ]

        errors_lock = threading.Lock()
        cancelled = threading.Event()

        def run_audit_task(node: EvaluateNode) -> None:
            if cancelled.is_set():
                return
            if circuit_breaker and circuit_breaker():
                cancelled.set()
                return

            snapshot = self.snapshots_by_name[node.snapshot_name]
            node_start, node_end = node.interval

            def _do_audit() -> t.List[AuditResult]:
                return self._audit_snapshot(
                    snapshot=snapshot,
                    environment_naming_info=environment_naming_info,
                    deployability_index=deployability_index,
                    snapshots=self.snapshots_by_name,
                    start=node_start,
                    end=node_end,
                    execution_time=execution_time,
                    # Run audits sequentially within each snapshot to avoid nested
                    # thread pool multiplication (max_workers threads are already active).
                    audit_concurrent_tasks=1,
                )

            self._run_node_with_progress(
                snapshot=snapshot,
                node=node,
                batched_intervals=batched_intervals,
                auto_restatement_triggers=auto_restatement_triggers,
                work_fn=_do_audit,
            )

        def run_audit_task_collecting_errors(node: EvaluateNode) -> None:
            try:
                run_audit_task(node)
            except CircuitBreakerError:
                cancelled.set()
            except Exception as ex:
                error: NodeExecutionFailedError[SchedulingUnit] = NodeExecutionFailedError(node)
                error.__cause__ = ex
                with errors_lock:
                    errors.append(error)

        concurrent_apply_to_values(audit_tasks, run_audit_task_collecting_errors, self.max_workers)

        if cancelled.is_set():
            raise CircuitBreakerError()

        return errors, skipped_intervals

    def _check_ready_intervals(
        self,
        snapshot: Snapshot,
        intervals: Intervals,
        context: ExecutionContext,
        environment_naming_info: EnvironmentNamingInfo,
    ) -> Intervals:
        """Checks if the intervals are ready for evaluation for the given snapshot.

        This implementation also includes the signal progress tracking.
        Note that this will handle gaps in the provided intervals. The returned intervals
        may introduce new gaps.

        Args:
            snapshot: The snapshot to check.
            intervals: The intervals to check.
            context: The context to use.
            environment_naming_info: The environment naming info to use.

        Returns:
            The intervals that are ready for evaluation.
        """
        signals = snapshot.is_model and snapshot.model.render_signal_calls()

        if not (signals and signals.signals_to_kwargs):
            return intervals

        self.console.start_signal_progress(
            snapshot,
            self.default_catalog,
            environment_naming_info or EnvironmentNamingInfo(),
        )

        for signal_idx, (signal_name, kwargs) in enumerate(signals.signals_to_kwargs.items()):
            # Capture intervals before signal check for display
            intervals_to_check = merge_intervals(intervals)

            signal_start_ts = time.perf_counter()

            try:
                intervals = check_ready_intervals(
                    signals.prepared_python_env[signal_name],
                    intervals,
                    context,
                    python_env=signals.python_env,
                    dialect=snapshot.model.dialect,
                    path=snapshot.model._path,
                    snapshot=snapshot,
                    kwargs=kwargs,
                )
            except SQLMeshError as e:
                raise SignalEvalError(
                    f"{e} '{signal_name}' for '{snapshot.model.name}' at {snapshot.model._path}"
                )

            duration = time.perf_counter() - signal_start_ts

            self.console.update_signal_progress(
                snapshot=snapshot,
                signal_name=signal_name,
                signal_idx=signal_idx,
                total_signals=len(signals.signals_to_kwargs),
                ready_intervals=merge_intervals(intervals),
                check_intervals=intervals_to_check,
                duration=duration,
            )

        self.console.stop_signal_progress()

        return intervals


def merged_missing_intervals(
    snapshots: t.Collection[Snapshot],
    start: t.Optional[TimeLike] = None,
    end: t.Optional[TimeLike] = None,
    execution_time: t.Optional[TimeLike] = None,
    deployability_index: t.Optional[DeployabilityIndex] = None,
    restatements: t.Optional[t.Dict[SnapshotId, Interval]] = None,
    start_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
    end_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
    ignore_cron: bool = False,
    end_bounded: bool = False,
) -> SnapshotToIntervals:
    """Find the largest contiguous date interval parameters based only on what is missing.

    For each node name, find all dependencies and look for a stored snapshot from the metastore. If a snapshot is found,
    calculate the missing intervals that need to be processed given the passed in start and end intervals.

    This is a superset of what may actually get processed at runtime based on things like batch size, signal readiness, etc.

    Args:
        snapshots: A set of target snapshots for which intervals should be computed.
        start: The start of the run. Defaults to the min node start date.
        end: The end of the run. Defaults to now.
        execution_time: The date/time reference to use for execution time. Defaults to now.
        deployability_index: Determines snapshots that are deployable in the context of this evaluation.
        restatements: A set of snapshot names being restated.
        start_override_per_model: A mapping of model FQNs to target start dates.
        end_override_per_model: A mapping of model FQNs to target end dates.
        ignore_cron: Whether to ignore the node's cron schedule.
        end_bounded: If set to true, the returned intervals will be bounded by the target end date, disregarding lookback,
            allow_partials, and other attributes that could cause the intervals to exceed the target end date.
    """
    restatements = restatements or {}
    validate_date_range(start, end)

    return compute_interval_params(
        snapshots,
        start=start or earliest_start_date(snapshots),
        end=end or now_timestamp(),
        deployability_index=deployability_index,
        execution_time=execution_time or now_timestamp(),
        restatements=restatements,
        start_override_per_model=start_override_per_model,
        end_override_per_model=end_override_per_model,
        ignore_cron=ignore_cron,
        end_bounded=end_bounded,
    )


def compute_interval_params(
    snapshots: t.Collection[Snapshot],
    *,
    start: TimeLike,
    end: TimeLike,
    deployability_index: t.Optional[DeployabilityIndex] = None,
    execution_time: t.Optional[TimeLike] = None,
    restatements: t.Optional[t.Dict[SnapshotId, Interval]] = None,
    start_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
    end_override_per_model: t.Optional[t.Dict[str, datetime]] = None,
    ignore_cron: bool = False,
    end_bounded: bool = False,
) -> SnapshotToIntervals:
    """Find the largest contiguous date interval parameters based only on what is missing.

    For each node name, find all dependencies and look for a stored snapshot from the metastore. If a snapshot is found,
    calculate the missing intervals that need to be processed given the passed in start and end intervals.

    This is a superset of what may actually get processed at runtime based on things like batch size, signal readiness, etc.

    Args:
        snapshots: A set of target snapshots for which intervals should be computed.
        start: Start of the interval.
        end: End of the interval.
        deployability_index: Determines snapshots that are deployable in the context of this evaluation.
        execution_time: The date/time reference to use for execution time.
        restatements: A dict of snapshot names being restated and their intervals.
        start_override_per_model: A mapping of model FQNs to target start dates.
        end_override_per_model: A mapping of model FQNs to target end dates.
        ignore_cron: Whether to ignore the node's cron schedule.
        end_bounded: If set to true, the returned intervals will be bounded by the target end date, disregarding lookback,
            allow_partials, and other attributes that could cause the intervals to exceed the target end date.

    Returns:
        A dict containing all snapshots needing to be run with their associated interval params.
    """
    snapshot_merged_intervals = {}

    for snapshot, intervals in missing_intervals(
        snapshots,
        start=start,
        end=end,
        execution_time=execution_time,
        restatements=restatements,
        deployability_index=deployability_index,
        start_override_per_model=start_override_per_model,
        end_override_per_model=end_override_per_model,
        ignore_cron=ignore_cron,
        end_bounded=end_bounded,
    ).items():
        contiguous_batch = []
        next_batch: Intervals = []

        for interval in intervals:
            if next_batch and interval[0] != next_batch[-1][-1]:
                contiguous_batch.append((next_batch[0][0], next_batch[-1][-1]))
                next_batch = []
            next_batch.append(interval)
        if next_batch:
            contiguous_batch.append((next_batch[0][0], next_batch[-1][-1]))
        snapshot_merged_intervals[snapshot] = contiguous_batch

    return snapshot_merged_intervals


def interval_diff(
    intervals_a: Intervals, intervals_b: Intervals, uninterrupted: bool = False
) -> Intervals:
    if not intervals_a or not intervals_b:
        return intervals_a

    index_a, index_b = 0, 0
    len_a = len(intervals_a)
    len_b = len(intervals_b)

    results = []

    while index_a < len_a and index_b < len_b:
        interval_a = intervals_a[index_a]
        interval_b = intervals_b[index_b]

        if interval_a[0] >= interval_b[1]:
            index_b += 1
        elif interval_b[0] >= interval_a[1]:
            results.append(interval_a)
            index_a += 1
        else:
            if uninterrupted:
                return results

            if interval_a[0] >= interval_b[0]:
                index_a += 1
            else:
                index_b += 1

    if index_a < len_a:
        interval_a = intervals_a[index_a]
        if interval_a[0] >= interval_b[1] or interval_b[0] >= interval_a[1]:
            results.extend(intervals_a[index_a:])

    return results


def _resolve_one_snapshot_per_version(
    snapshots: t.Iterable[Snapshot],
) -> t.Dict[t.Tuple[str, str], Snapshot]:
    snapshot_per_version: t.Dict[t.Tuple[str, str], Snapshot] = {}
    for snapshot in snapshots:
        key = (snapshot.name, snapshot.version_get_or_generate())
        if key not in snapshot_per_version:
            snapshot_per_version[key] = snapshot
        else:
            prev_snapshot = snapshot_per_version[key]
            if snapshot.unpaused_ts and (
                not prev_snapshot.unpaused_ts or snapshot.created_ts > prev_snapshot.created_ts
            ):
                snapshot_per_version[key] = snapshot

    return snapshot_per_version


def _expand_range_as_interval(
    start_ts: int, end_ts: int, interval_unit: IntervalUnit
) -> t.List[Interval]:
    values = expand_range(start_ts, end_ts, interval_unit)
    return [(values[i], values[i + 1]) for i in range(len(values) - 1)]
