"""
# PlanEvaluator

A plan evaluator is responsible for evaluating a plan when it is being applied.

# Evaluation steps

At a high level, when a plan is evaluated, SQLMesh will:
- Push new snapshots to the state sync.
- Create snapshot tables.
- Backfill data.
- Promote the snapshots.

Refer to `sqlmesh.core.plan`.
"""

import abc
import logging
import typing as t
from sqlmesh.core import analytics
from sqlmesh.core import constants as c
from sqlmesh.core.console import Console, get_console
from sqlmesh.core.environment import EnvironmentNamingInfo, execute_environment_statements
from sqlmesh.core.macros import RuntimeStage
from sqlmesh.core.snapshot.definition import to_view_mapping, SnapshotTableInfo
from sqlmesh.core.plan import stages
from sqlmesh.core.plan.definition import EvaluatablePlan
from sqlmesh.core.scheduler import Scheduler
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotEvaluator,
    SnapshotIntervals,
    SnapshotId,
    SnapshotInfoLike,
    SnapshotCreationFailedError,
)
from sqlmesh.utils import to_snake_case
from sqlmesh.core.state_sync import StateSync
from sqlmesh.core.plan.common import identify_restatement_intervals_across_snapshot_versions
from sqlmesh.utils import CorrelationId
from sqlmesh.utils.concurrency import NodeExecutionFailedError
from sqlmesh.utils.errors import PlanError, ConflictingPlanError, SQLMeshError
from sqlmesh.utils.date import now, to_timestamp

logger = logging.getLogger(__name__)


class PlanEvaluator(abc.ABC):
    @abc.abstractmethod
    def evaluate(
        self,
        plan: EvaluatablePlan,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
    ) -> None:
        """Evaluates a plan by pushing snapshots and backfilling data.

        Given a plan, it pushes snapshots into the state and then kicks off
        the backfill process for all affected snapshots. Once backfill is done,
        snapshots that are part of the plan are promoted in the environment targeted
        by this plan.

        Args:
            plan: The plan to evaluate.
            circuit_breaker: The circuit breaker to use.
        """


class BuiltInPlanEvaluator(PlanEvaluator):
    def __init__(
        self,
        state_sync: StateSync,
        snapshot_evaluator: SnapshotEvaluator,
        create_scheduler: t.Callable[[t.Iterable[Snapshot], SnapshotEvaluator], Scheduler],
        default_catalog: t.Optional[str],
        console: t.Optional[Console] = None,
    ):
        self.state_sync = state_sync
        self.snapshot_evaluator = snapshot_evaluator
        self.create_scheduler = create_scheduler
        self.default_catalog = default_catalog
        self.console = console or get_console()
        self._circuit_breaker: t.Optional[t.Callable[[], bool]] = None

    def evaluate(
        self,
        plan: EvaluatablePlan,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
    ) -> None:
        self._circuit_breaker = circuit_breaker
        self.snapshot_evaluator = self.snapshot_evaluator.set_correlation_id(
            CorrelationId.from_plan_id(plan.plan_id)
        )

        self.console.start_plan_evaluation(plan)
        analytics.collector.on_plan_apply_start(
            plan=plan,
            engine_type=self.snapshot_evaluator.adapter.dialect,
            state_sync_type=self.state_sync.state_type(),
            scheduler_type=c.BUILTIN,
        )

        try:
            plan_stages = stages.build_plan_stages(plan, self.state_sync, self.default_catalog)
            self._evaluate_stages(plan_stages, plan)
        except Exception as e:
            analytics.collector.on_plan_apply_end(plan_id=plan.plan_id, error=e)
            raise
        else:
            analytics.collector.on_plan_apply_end(plan_id=plan.plan_id)
        finally:
            self.snapshot_evaluator.recycle()
            self.console.stop_plan_evaluation()

    def _evaluate_stages(
        self, plan_stages: t.List[stages.PlanStage], plan: EvaluatablePlan
    ) -> None:
        for stage in plan_stages:
            stage_name = stage.__class__.__name__
            handler_name = f"visit_{to_snake_case(stage_name)}"
            if not hasattr(self, handler_name):
                raise SQLMeshError(f"Unexpected plan stage: {stage_name}")
            logger.info("Evaluating plan stage %s", stage_name)
            handler = getattr(self, handler_name)
            handler(stage, plan)

    def visit_before_all_stage(self, stage: stages.BeforeAllStage, plan: EvaluatablePlan) -> None:
        execute_environment_statements(
            adapter=self.snapshot_evaluator.adapter,
            environment_statements=stage.statements,
            runtime_stage=RuntimeStage.BEFORE_ALL,
            environment_naming_info=plan.environment.naming_info,
            default_catalog=self.default_catalog,
            snapshots=stage.all_snapshots,
            start=plan.start,
            end=plan.end,
            execution_time=plan.execution_time,
            selected_models=plan.selected_models,
        )

    def visit_after_all_stage(self, stage: stages.AfterAllStage, plan: EvaluatablePlan) -> None:
        execute_environment_statements(
            adapter=self.snapshot_evaluator.adapter,
            environment_statements=stage.statements,
            runtime_stage=RuntimeStage.AFTER_ALL,
            environment_naming_info=plan.environment.naming_info,
            default_catalog=self.default_catalog,
            snapshots=stage.all_snapshots,
            start=plan.start,
            end=plan.end,
            execution_time=plan.execution_time,
            selected_models=plan.selected_models,
        )

    def visit_create_snapshot_records_stage(
        self, stage: stages.CreateSnapshotRecordsStage, plan: EvaluatablePlan
    ) -> None:
        self.state_sync.push_snapshots(stage.snapshots)
        analytics.collector.on_snapshots_created(
            new_snapshots=stage.snapshots, plan_id=plan.plan_id
        )
        # Update the intervals for the new forward-only snapshots
        self._update_intervals_for_new_snapshots(stage.snapshots)

    def visit_physical_layer_update_stage(
        self, stage: stages.PhysicalLayerUpdateStage, plan: EvaluatablePlan
    ) -> None:
        skip_message = "" if plan.restatements else "\nSKIP: No physical layer updates to perform"

        snapshots_to_create = stage.snapshots
        if not snapshots_to_create:
            self.console.log_success(skip_message)
            return

        completion_status = None
        progress_stopped = False
        try:
            completion_status = self.snapshot_evaluator.create(
                snapshots_to_create,
                stage.all_snapshots,
                allow_destructive_snapshots=plan.allow_destructive_models,
                allow_additive_snapshots=plan.allow_additive_models,
                deployability_index=stage.deployability_index,
                on_start=lambda x: self.console.start_creation_progress(
                    x, plan.environment, self.default_catalog
                ),
                on_complete=self.console.update_creation_progress,
            )
            if completion_status.is_nothing_to_do:
                self.console.log_success(skip_message)
                return
        except SnapshotCreationFailedError as ex:
            self.console.stop_creation_progress(success=False)
            progress_stopped = True

            for error in ex.errors:
                logger.info(str(error), exc_info=error)

            self.console.log_skipped_models({s.name for s in ex.skipped})
            self.console.log_failed_models(ex.errors)

            raise PlanError("Plan application failed.")
        finally:
            if not progress_stopped:
                self.console.stop_creation_progress(
                    success=completion_status is not None and completion_status.is_success
                )

    def visit_physical_layer_schema_creation_stage(
        self, stage: stages.PhysicalLayerSchemaCreationStage, plan: EvaluatablePlan
    ) -> None:
        try:
            self.snapshot_evaluator.create_physical_schemas(
                stage.snapshots, stage.deployability_index
            )
        except Exception as ex:
            raise PlanError("Plan application failed.") from ex

    def visit_backfill_stage(self, stage: stages.BackfillStage, plan: EvaluatablePlan) -> None:
        if plan.empty_backfill:
            intervals_to_add = []
            for snapshot in stage.all_snapshots.values():
                if not snapshot.evaluatable or not plan.is_selected_for_backfill(snapshot.name):
                    # Skip snapshots that are not evaluatable or not selected for backfill.
                    continue
                intervals = [
                    snapshot.inclusive_exclusive(plan.start, plan.end, strict=False, expand=False)
                ]
                is_deployable = stage.deployability_index.is_deployable(snapshot)
                intervals_to_add.append(
                    SnapshotIntervals(
                        name=snapshot.name,
                        identifier=snapshot.identifier,
                        version=snapshot.version,
                        dev_version=snapshot.dev_version,
                        intervals=intervals if is_deployable else [],
                        dev_intervals=intervals if not is_deployable else [],
                    )
                )
            self.state_sync.add_snapshots_intervals(intervals_to_add)
            self.console.log_success("SKIP: No model batches to execute")
            return

        if not stage.snapshot_to_intervals:
            self.console.log_success("SKIP: No model batches to execute")
            return

        scheduler = self.create_scheduler(stage.all_snapshots.values(), self.snapshot_evaluator)
        errors, _ = scheduler.run_merged_intervals(
            merged_intervals=stage.snapshot_to_intervals,
            deployability_index=stage.deployability_index,
            environment_naming_info=plan.environment.naming_info,
            execution_time=plan.execution_time,
            circuit_breaker=self._circuit_breaker,
            start=plan.start,
            end=plan.end,
            allow_destructive_snapshots=plan.allow_destructive_models,
            allow_additive_snapshots=plan.allow_additive_models,
            selected_snapshot_ids=stage.selected_snapshot_ids,
            selected_models=plan.selected_models,
            is_restatement=bool(plan.restatements),
            skip_audits=plan.skip_audits,
        )
        if errors:
            raise PlanError("Plan application failed.")

    def visit_audit_only_run_stage(
        self, stage: stages.AuditOnlyRunStage, plan: EvaluatablePlan
    ) -> None:
        if plan.skip_audits:
            return

        audit_snapshots = stage.snapshots
        if not audit_snapshots:
            return

        # If there are any snapshots to be audited, we'll reuse the scheduler's internals to audit them
        scheduler = self.create_scheduler(audit_snapshots, self.snapshot_evaluator)
        completion_status = scheduler.audit(
            plan.environment,
            plan.start,
            plan.end,
            execution_time=plan.execution_time,
            end_bounded=plan.end_bounded,
            start_override_per_model=plan.start_override_per_model,
            end_override_per_model=plan.end_override_per_model,
        )

        if completion_status.is_failure:
            raise PlanError("Plan application failed.")

    def visit_restatement_stage(
        self, stage: stages.RestatementStage, plan: EvaluatablePlan
    ) -> None:
        # Restating intervals on prod plans means that once the data for the intervals being restated has been backfilled
        # (which happens in the backfill stage) then we need to clear those intervals *from state* across all other environments.
        #
        # This ensures that work done in dev environments can still be promoted to prod by forcing dev environments to
        # re-run intervals that changed in prod (because after this stage runs they are cleared from state and thus show as missing)
        #
        # It also means that any new dev environments created while this restatement plan was running also get the
        # correct intervals cleared because we look up matching snapshots as at right now and not as at the time the plan
        # was created, which could have been several hours ago if there was a lot of data to restate.
        #
        # Without this rule, its possible that promoting a dev table to prod will introduce old data to prod

        intervals_to_clear = identify_restatement_intervals_across_snapshot_versions(
            state_reader=self.state_sync,
            prod_restatements=plan.restatements,
            disable_restatement_models=plan.disabled_restatement_models,
            loaded_snapshots={s.snapshot_id: s for s in stage.all_snapshots.values()},
            current_ts=to_timestamp(plan.execution_time or now()),
        )

        if not intervals_to_clear:
            # Nothing to do
            return

        # While the restatements were being processed, did any of the snapshots being restated get new versions deployed?
        # If they did, they will not reflect the data that just got restated, so we need to notify the user
        deployed_during_restatement: t.Dict[
            str, t.Tuple[SnapshotTableInfo, SnapshotTableInfo]
        ] = {}  # tuple of (restated_snapshot, current_prod_snapshot)

        if deployed_env := self.state_sync.get_environment(plan.environment.name):
            promoted_snapshots_by_name = {s.name: s for s in deployed_env.snapshots}

            for name in plan.restatements:
                snapshot = stage.all_snapshots[name]
                version = snapshot.table_info.version
                if (
                    prod_snapshot := promoted_snapshots_by_name.get(name)
                ) and prod_snapshot.version != version:
                    deployed_during_restatement[name] = (
                        snapshot.table_info,
                        prod_snapshot.table_info,
                    )

        # we need to *not* clear the intervals on the snapshots where new versions were deployed while the restatement was running in order to prevent
        # subsequent plans from having unexpected intervals to backfill.
        # we instead list the affected models and abort the plan with an error so the user can decide what to do
        # (either re-attempt the restatement plan or leave things as they are)
        filtered_intervals_to_clear = [
            (s.snapshot, s.interval)
            for s in intervals_to_clear.values()
            if s.snapshot.name not in deployed_during_restatement
        ]

        if filtered_intervals_to_clear:
            # We still clear intervals in other envs for models that were successfully restated without having new versions promoted during restatement
            self.state_sync.remove_intervals(
                snapshot_intervals=filtered_intervals_to_clear,
                remove_shared_versions=plan.is_prod,
            )

        if deployed_env and deployed_during_restatement:
            self.console.log_models_updated_during_restatement(
                list(deployed_during_restatement.values()),
                plan.environment.naming_info,
                self.default_catalog,
            )
            raise ConflictingPlanError(
                f"Another plan ({deployed_env.summary.plan_id}) deployed new versions of {len(deployed_during_restatement)} models in the target environment '{plan.environment.name}' while they were being restated by this plan.\n"
                "Please re-apply your plan if these new versions should be restated."
            )

    def visit_environment_record_update_stage(
        self, stage: stages.EnvironmentRecordUpdateStage, plan: EvaluatablePlan
    ) -> None:
        self.state_sync.promote(
            plan.environment,
            no_gaps_snapshot_names=stage.no_gaps_snapshot_names if plan.no_gaps else set(),
            environment_statements=plan.environment_statements,
        )

    def visit_migrate_schemas_stage(
        self, stage: stages.MigrateSchemasStage, plan: EvaluatablePlan
    ) -> None:
        try:
            self.snapshot_evaluator.migrate(
                stage.snapshots,
                stage.all_snapshots,
                allow_destructive_snapshots=plan.allow_destructive_models,
                allow_additive_snapshots=plan.allow_additive_models,
                deployability_index=stage.deployability_index,
            )
        except NodeExecutionFailedError as ex:
            raise PlanError(str(ex.__cause__) if ex.__cause__ else str(ex))

    def visit_unpause_stage(self, stage: stages.UnpauseStage, plan: EvaluatablePlan) -> None:
        self.state_sync.unpause_snapshots(stage.promoted_snapshots, plan.end)

    def visit_virtual_layer_update_stage(
        self, stage: stages.VirtualLayerUpdateStage, plan: EvaluatablePlan
    ) -> None:
        environment = plan.environment

        self.console.start_promotion_progress(
            list(stage.promoted_snapshots) + list(stage.demoted_snapshots),
            environment.naming_info,
            self.default_catalog,
        )

        completed = False
        try:
            self._promote_snapshots(
                plan,
                [stage.all_snapshots[s.snapshot_id] for s in stage.promoted_snapshots],
                environment.naming_info,
                deployability_index=stage.deployability_index,
                on_complete=lambda s: self.console.update_promotion_progress(s, True),
                snapshots=stage.all_snapshots,
            )
            if stage.demoted_environment_naming_info:
                self._demote_snapshots(
                    [stage.all_snapshots[s.snapshot_id] for s in stage.demoted_snapshots],
                    stage.demoted_environment_naming_info,
                    deployability_index=stage.deployability_index,
                    on_complete=lambda s: self.console.update_promotion_progress(s, False),
                    snapshots=stage.all_snapshots,
                )

            completed = True
        finally:
            self.console.stop_promotion_progress(success=completed)

    def visit_finalize_environment_stage(
        self, stage: stages.FinalizeEnvironmentStage, plan: EvaluatablePlan
    ) -> None:
        self.state_sync.finalize(plan.environment)

    def _promote_snapshots(
        self,
        plan: EvaluatablePlan,
        target_snapshots: t.Iterable[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        snapshots: t.Dict[SnapshotId, Snapshot],
        deployability_index: t.Optional[DeployabilityIndex] = None,
        on_complete: t.Optional[t.Callable[[SnapshotInfoLike], None]] = None,
    ) -> None:
        self.snapshot_evaluator.promote(
            target_snapshots,
            start=plan.start,
            end=plan.end,
            execution_time=plan.execution_time or now(),
            snapshots=snapshots,
            table_mapping=to_view_mapping(
                snapshots.values(),
                environment_naming_info,
                default_catalog=self.default_catalog,
                dialect=self.snapshot_evaluator.adapter.dialect,
            ),
            environment_naming_info=environment_naming_info,
            deployability_index=deployability_index,
            on_complete=on_complete,
        )

    def _demote_snapshots(
        self,
        target_snapshots: t.Iterable[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        snapshots: t.Dict[SnapshotId, Snapshot],
        deployability_index: t.Optional[DeployabilityIndex] = None,
        on_complete: t.Optional[t.Callable[[SnapshotInfoLike], None]] = None,
    ) -> None:
        self.snapshot_evaluator.demote(
            target_snapshots,
            environment_naming_info,
            table_mapping=to_view_mapping(
                snapshots.values(),
                environment_naming_info,
                default_catalog=self.default_catalog,
                dialect=self.snapshot_evaluator.adapter.dialect,
            ),
            deployability_index=deployability_index,
            on_complete=on_complete,
        )

    def _update_intervals_for_new_snapshots(self, snapshots: t.Collection[Snapshot]) -> None:
        snapshots_intervals: t.List[SnapshotIntervals] = []
        for snapshot in snapshots:
            if snapshot.is_forward_only:
                snapshots_intervals.append(
                    SnapshotIntervals(
                        name=snapshot.name,
                        identifier=snapshot.identifier,
                        version=snapshot.version,
                        dev_version=snapshot.dev_version,
                        dev_intervals=snapshot.dev_intervals,
                    )
                )

        if snapshots_intervals:
            self.state_sync.add_snapshots_intervals(snapshots_intervals)
