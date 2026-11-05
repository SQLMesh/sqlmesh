import typing as t

from dataclasses import dataclass
from sqlmesh.core import constants as c
from sqlmesh.core.environment import EnvironmentStatements, EnvironmentNamingInfo, Environment
from sqlmesh.core.plan.common import should_force_rebuild
from sqlmesh.core.plan.definition import EvaluatablePlan
from sqlmesh.core.state_sync import StateReader
from sqlmesh.core.scheduler import merged_missing_intervals, SnapshotToIntervals
from sqlmesh.core.snapshot.definition import (
    DeployabilityIndex,
    Snapshot,
    SnapshotTableInfo,
    SnapshotId,
    snapshots_to_dag,
)
from sqlmesh.utils.errors import PlanError


@dataclass
class BeforeAllStage:
    """Run environment statements before every other stage.

    Args:
        statements: Environment statements to run before every other stage.
        all_snapshots: All snapshots in the plan by name.
    """

    statements: t.List[EnvironmentStatements]
    all_snapshots: t.Dict[str, Snapshot]


@dataclass
class AfterAllStage:
    """Run environment statements after all other stages.

    Args:
        statements: Environment statements to run after all other stages.
        all_snapshots: All snapshots in the plan by name.
    """

    statements: t.List[EnvironmentStatements]
    all_snapshots: t.Dict[str, Snapshot]


@dataclass
class CreateSnapshotRecordsStage:
    """Create new snapshot reecords in the state.

    Args:
        snapshots: New snapshots to create records for.
    """

    snapshots: t.List[Snapshot]


@dataclass
class PhysicalLayerUpdateStage:
    """Update the physical layer by creating physical tables and views for given snapshots.

    Args:
        snapshots: Snapshots to create physical tables and views for. This collection can be empty in which case
            no physical layer update is needed. This can be useful to report the lack of physical layer updates
            back to the user.
        all_snapshots: All snapshots in the plan by snapshot ID.
        snapshots_with_missing_intervals: Snapshots that have missing intervals.
        deployability_index: Deployability index for this stage.
    """

    snapshots: t.List[Snapshot]
    all_snapshots: t.Dict[SnapshotId, Snapshot]
    snapshots_with_missing_intervals: t.Set[SnapshotId]
    deployability_index: DeployabilityIndex


@dataclass
class PhysicalLayerSchemaCreationStage:
    """Create the physical schemas for the given snapshots.

    Args:
        snapshots: Snapshots to create physical schemas for.
        deployability_index: Deployability index for this stage.
    """

    snapshots: t.List[Snapshot]
    deployability_index: DeployabilityIndex


@dataclass
class AuditOnlyRunStage:
    """Run audits only for given snapshots.

    Args:
        snapshots: Snapshots to run audits for.
    """

    snapshots: t.List[Snapshot]


@dataclass
class RestatementStage:
    """Clear intervals from state for snapshots in *other* environments, when restatements are requested in prod.

    This stage is effectively a "marker" stage to trigger the plan evaluator to perform the "clear intervals" logic after the BackfillStage has completed.
    The "clear intervals" logic is executed just-in-time using the latest state available in order to pick up new snapshots that may have
    been created while the BackfillStage was running, which is why we do not build a list of snapshots to clear at plan time and defer to evaluation time.

    Note that this stage is only present on `prod` plans because dev plans do not need to worry about clearing intervals in other environments.

    Args:
        all_snapshots: All snapshots in the plan by name. Note that this does not include the snapshots from other environments that will get their
            intervals cleared, it's included here as an optimization to prevent having to re-fetch the current plan's snapshots
    """

    all_snapshots: t.Dict[str, Snapshot]


@dataclass
class BackfillStage:
    """Backfill given missing intervals.

    Args:
        snapshot_to_intervals: Intervals to backfill. This collection can be empty in which case no backfill is needed.
            This can be useful to report the lack of backfills back to the user.
        selected_snapshot_ids: The snapshots to include in the run DAG.
        all_snapshots: All snapshots in the plan by name.
        deployability_index: Deployability index for this stage.
        before_promote: Whether this stage is before the promotion stage.
    """

    snapshot_to_intervals: SnapshotToIntervals
    selected_snapshot_ids: t.Set[SnapshotId]
    all_snapshots: t.Dict[str, Snapshot]
    deployability_index: DeployabilityIndex
    before_promote: bool = True


@dataclass
class EnvironmentRecordUpdateStage:
    """Update the environment record in the state.

    Args:
        no_gaps_snapshot_names: Names of snapshots for which there should be no interval gaps.
        all_snapshots: All snapshots in the plan by name.
        selected_snapshot_ids: The snapshots to include in a just-in-time no-gaps backfill.
        deployability_index: Deployability index for a just-in-time no-gaps backfill.
    """

    no_gaps_snapshot_names: t.Set[str]
    all_snapshots: t.Dict[str, Snapshot]
    selected_snapshot_ids: t.Set[SnapshotId]
    deployability_index: DeployabilityIndex


@dataclass
class MigrateSchemasStage:
    """Migrate schemas of physical tables for given snapshots.

    Args:
        snapshots: Snapshots to migrate schemas for.
        all_snapshots: All snapshots in the plan by snapshot ID.
        deployability_index: Deployability index for this stage.
    """

    snapshots: t.List[Snapshot]
    all_snapshots: t.Dict[SnapshotId, Snapshot]
    deployability_index: DeployabilityIndex


@dataclass
class VirtualLayerUpdateStage:
    """Update the virtual layer by creating and deleting views for given snapshots.

    Args:
        promoted_snapshots: Snapshots to create views for.
        demoted_snapshots: Snapshots to delete views for.
        demoted_environment_naming_info: Environment naming info of the previous environment record.
        all_snapshots: All snapshots in the plan by snapshot ID.
        deployability_index: Deployability index for this stage.
    """

    promoted_snapshots: t.Set[SnapshotTableInfo]
    demoted_snapshots: t.Set[SnapshotTableInfo]
    demoted_environment_naming_info: t.Optional[EnvironmentNamingInfo]
    all_snapshots: t.Dict[SnapshotId, Snapshot]
    deployability_index: DeployabilityIndex


@dataclass
class UnpauseStage:
    """Unpause given snapshots that are being deployed to prod.

    Args:
        promoted_snapshots: Snapshots to unpause.
    """

    promoted_snapshots: t.Set[SnapshotTableInfo]


@dataclass
class FinalizeEnvironmentStage:
    """Finalize the enviornment record in the state.

    Finalization means that all stages have been applied and that the environment has been transitioned
    to the new state successfully. This should be the last stage in the plan application process.
    """

    pass


PlanStage = t.Union[
    BeforeAllStage,
    AfterAllStage,
    CreateSnapshotRecordsStage,
    PhysicalLayerUpdateStage,
    PhysicalLayerSchemaCreationStage,
    AuditOnlyRunStage,
    RestatementStage,
    BackfillStage,
    EnvironmentRecordUpdateStage,
    MigrateSchemasStage,
    VirtualLayerUpdateStage,
    UnpauseStage,
    FinalizeEnvironmentStage,
]


class PlanStagesBuilder:
    """The builder for the plan stages.

    Args:
        state_reader: The state reader to use to read the snapshots and environment.
        default_catalog: The default catalog to use for the snapshots.
    """

    def __init__(
        self,
        state_reader: StateReader,
        default_catalog: t.Optional[str],
    ):
        self.state_reader = state_reader
        self.default_catalog = default_catalog

    def build(self, plan: EvaluatablePlan) -> t.List[PlanStage]:
        """Builds the plan stages for the given plan.

        NOTE: Building the plan stages should NOT produce any side effects in the state or the data warehouse.

        Args:
            plan: The plan to build the stages for.

        Returns:
            A list of plan stages.
        """
        new_snapshots = {s.snapshot_id: s for s in plan.new_snapshots}
        stored_snapshots = self.state_reader.get_snapshots(plan.environment.snapshots)
        snapshots = {**new_snapshots, **stored_snapshots}
        snapshots_by_name = {s.name: s for s in snapshots.values()}
        dag = snapshots_to_dag(snapshots.values())

        all_selected_for_backfill_snapshots = {
            s.snapshot_id for s in snapshots.values() if plan.is_selected_for_backfill(s.name)
        }
        existing_environment = self.state_reader.get_environment(plan.environment.name)

        self._adjust_intervals(snapshots_by_name, plan, existing_environment)

        deployability_index = DeployabilityIndex.create(snapshots, start=plan.start)
        if plan.is_dev:
            before_promote_snapshots = all_selected_for_backfill_snapshots
            after_promote_snapshots = set()
            snapshots_with_schema_migration = []
        else:
            before_promote_snapshots = {
                s.snapshot_id
                for s in snapshots.values()
                if (deployability_index.is_representative(s) or s.is_seed)
                and plan.is_selected_for_backfill(s.name)
            }
            after_promote_snapshots = all_selected_for_backfill_snapshots - before_promote_snapshots
            deployability_index = DeployabilityIndex.all_deployable()

            snapshot_ids_with_schema_migration = [
                s.snapshot_id for s in snapshots.values() if s.requires_schema_migration_in_prod
            ]
            # Include all upstream dependencies of snapshots that require schema migration to make sure
            # the upstream tables are created before the schema updates are applied
            snapshots_with_schema_migration = [
                snapshots[s_id]
                for s_id in dag.subdag(*snapshot_ids_with_schema_migration)
                if snapshots[s_id].supports_schema_migration_in_prod
            ]

        snapshots_to_intervals = self._missing_intervals(
            plan, snapshots_by_name, deployability_index
        )
        needs_backfill = (
            not plan.empty_backfill and not plan.skip_backfill and bool(snapshots_to_intervals)
        )
        missing_intervals_before_promote: SnapshotToIntervals = {}
        missing_intervals_after_promote: SnapshotToIntervals = {}
        if needs_backfill:
            for snapshot, intervals in snapshots_to_intervals.items():
                if snapshot.snapshot_id in before_promote_snapshots:
                    missing_intervals_before_promote[snapshot] = intervals
                elif snapshot.snapshot_id in after_promote_snapshots:
                    missing_intervals_after_promote[snapshot] = intervals

        promoted_snapshots, demoted_snapshots, demoted_environment_naming_info = (
            self._get_promoted_demoted_snapshots(plan, existing_environment)
        )

        stages: t.List[PlanStage] = []

        before_all_stage = self._get_before_all_stage(plan, snapshots_by_name)
        if before_all_stage:
            stages.append(before_all_stage)

        if plan.new_snapshots:
            stages.append(CreateSnapshotRecordsStage(snapshots=plan.new_snapshots))

        snapshots_to_create = self._get_snapshots_to_create(plan, snapshots)
        if snapshots_to_create:
            stages.append(
                PhysicalLayerSchemaCreationStage(
                    snapshots=snapshots_to_create, deployability_index=deployability_index
                )
            )
        if not needs_backfill:
            stages.append(
                self._get_physical_layer_update_stage(
                    plan,
                    snapshots_to_create,
                    snapshots,
                    snapshots_to_intervals,
                    deployability_index,
                )
            )

        audit_only_snapshots = self._get_audit_only_snapshots(new_snapshots)
        if audit_only_snapshots:
            stages.append(AuditOnlyRunStage(snapshots=list(audit_only_snapshots.values())))

        if missing_intervals_before_promote:
            stages.append(
                BackfillStage(
                    snapshot_to_intervals=missing_intervals_before_promote,
                    selected_snapshot_ids={
                        s_id
                        for s_id in before_promote_snapshots
                        if plan.is_selected_for_backfill(s_id.name)
                    },
                    all_snapshots=snapshots_by_name,
                    deployability_index=deployability_index,
                )
            )
        elif not needs_backfill:
            # Append an empty backfill stage so that explainer can show that the stage is skipped
            stages.append(
                BackfillStage(
                    snapshot_to_intervals={},
                    selected_snapshot_ids=set(),
                    all_snapshots=snapshots_by_name,
                    deployability_index=deployability_index,
                )
            )

        # note: "restatement stage" (which is clearing intervals in state - not actually performing the restatements, that's the backfill stage)
        # needs to come *after* the backfill stage so that at no time do other plans / runs see empty prod intervals and compete with this plan to try to fill them.
        # in addition, when we update intervals in state, we only clear intervals from dev snapshots to force dev models to be backfilled based on the new prod data.
        # we can leave prod intervals alone because by the time this plan finishes, the intervals in state have not actually changed, since restatement replaces
        # data for existing intervals and does not produce new ones
        restatement_stage = self._get_restatement_stage(plan, snapshots_by_name)
        if restatement_stage:
            stages.append(restatement_stage)

        stages.append(
            EnvironmentRecordUpdateStage(
                no_gaps_snapshot_names={s.name for s in before_promote_snapshots},
                all_snapshots=snapshots_by_name,
                selected_snapshot_ids=before_promote_snapshots,
                deployability_index=deployability_index,
            )
        )

        if snapshots_with_schema_migration:
            stages.append(
                MigrateSchemasStage(
                    snapshots=snapshots_with_schema_migration,
                    all_snapshots=snapshots,
                    deployability_index=deployability_index,
                )
            )

        if not plan.is_dev and not plan.ensure_finalized_snapshots and promoted_snapshots:
            # Only unpause at this point if we don't have to use the finalized snapshots
            # for subsequent plan applications. Otherwise, unpause right before updating
            # the virtual layer.
            stages.append(UnpauseStage(promoted_snapshots=promoted_snapshots))

        if missing_intervals_after_promote:
            stages.append(
                BackfillStage(
                    snapshot_to_intervals=missing_intervals_after_promote,
                    selected_snapshot_ids={
                        s_id
                        for s_id in after_promote_snapshots
                        if plan.is_selected_for_backfill(s_id.name)
                    },
                    all_snapshots=snapshots_by_name,
                    deployability_index=deployability_index,
                )
            )

        if not plan.is_dev and plan.ensure_finalized_snapshots and promoted_snapshots:
            # Unpause right before updating the virtual layer and finalizing the environment in case when
            # we need to use the finalized snapshots for subsequent plan applications.
            # Otherwise, unpause right after updatig the environment record.
            stages.append(UnpauseStage(promoted_snapshots=promoted_snapshots))

        full_demoted_snapshots = self.state_reader.get_snapshots(
            s.snapshot_id for s in demoted_snapshots if s.snapshot_id not in snapshots
        )
        virtual_layer_update_stage = self._get_virtual_layer_update_stage(
            promoted_snapshots,
            demoted_snapshots,
            demoted_environment_naming_info,
            snapshots | full_demoted_snapshots,
            deployability_index,
            plan.is_dev,
        )
        if virtual_layer_update_stage:
            stages.append(virtual_layer_update_stage)

        stages.append(FinalizeEnvironmentStage())

        after_all_stage = self._get_after_all_stage(plan, snapshots_by_name)
        if after_all_stage:
            stages.append(after_all_stage)

        return stages

    def _get_before_all_stage(
        self, plan: EvaluatablePlan, snapshots_by_name: t.Dict[str, Snapshot]
    ) -> t.Optional[BeforeAllStage]:
        before_all = [
            environment_statements
            for environment_statements in plan.environment_statements or []
            if environment_statements.before_all
        ]
        return (
            BeforeAllStage(statements=before_all, all_snapshots=snapshots_by_name)
            if before_all
            else None
        )

    def _get_after_all_stage(
        self, plan: EvaluatablePlan, snapshots_by_name: t.Dict[str, Snapshot]
    ) -> t.Optional[AfterAllStage]:
        after_all = [
            environment_statements
            for environment_statements in plan.environment_statements or []
            if environment_statements.after_all
        ]
        return (
            AfterAllStage(statements=after_all, all_snapshots=snapshots_by_name)
            if after_all
            else None
        )

    def _get_restatement_stage(
        self, plan: EvaluatablePlan, snapshots_by_name: t.Dict[str, Snapshot]
    ) -> t.Optional[RestatementStage]:
        if plan.restate_all_snapshots:
            if plan.is_dev:
                raise PlanError(
                    "Clearing intervals from state across dev model versions is only valid for prod plans"
                )

            if plan.restatements:
                return RestatementStage(
                    all_snapshots=snapshots_by_name,
                )

        return None

    def _get_physical_layer_update_stage(
        self,
        plan: EvaluatablePlan,
        snapshots_to_create: t.List[Snapshot],
        all_snapshots: t.Dict[SnapshotId, Snapshot],
        snapshots_to_intervals: SnapshotToIntervals,
        deployability_index: DeployabilityIndex,
    ) -> PhysicalLayerUpdateStage:
        return PhysicalLayerUpdateStage(
            snapshots=snapshots_to_create,
            all_snapshots=all_snapshots,
            snapshots_with_missing_intervals={
                s.snapshot_id
                for s in snapshots_to_intervals
                if plan.is_selected_for_backfill(s.name)
            },
            deployability_index=deployability_index,
        )

    def _get_virtual_layer_update_stage(
        self,
        promoted_snapshots: t.Set[SnapshotTableInfo],
        demoted_snapshots: t.Set[SnapshotTableInfo],
        demoted_environment_naming_info: t.Optional[EnvironmentNamingInfo],
        all_snapshots: t.Dict[SnapshotId, Snapshot],
        deployability_index: DeployabilityIndex,
        is_dev: bool,
    ) -> t.Optional[VirtualLayerUpdateStage]:
        def _should_update_virtual_layer(snapshot: SnapshotTableInfo) -> bool:
            # Skip virtual layer update for snapshots with virtual environment support disabled
            virtual_environment_enabled = is_dev or snapshot.virtual_environment_mode.is_full
            return snapshot.is_model and not snapshot.is_symbolic and virtual_environment_enabled

        promoted_snapshots = {s for s in promoted_snapshots if _should_update_virtual_layer(s)}
        demoted_snapshots = {s for s in demoted_snapshots if _should_update_virtual_layer(s)}
        if not promoted_snapshots and not demoted_snapshots:
            return None

        return VirtualLayerUpdateStage(
            promoted_snapshots=promoted_snapshots,
            demoted_snapshots=demoted_snapshots,
            demoted_environment_naming_info=demoted_environment_naming_info,
            all_snapshots=all_snapshots,
            deployability_index=deployability_index,
        )

    def _get_promoted_demoted_snapshots(
        self, plan: EvaluatablePlan, existing_environment: t.Optional[Environment]
    ) -> t.Tuple[
        t.Set[SnapshotTableInfo], t.Set[SnapshotTableInfo], t.Optional[EnvironmentNamingInfo]
    ]:
        if existing_environment:
            new_table_infos = {
                table_info.name: table_info for table_info in plan.environment.promoted_snapshots
            }
            existing_table_infos = {
                table_info.name: table_info
                for table_info in existing_environment.promoted_snapshots
            }
            views_that_changed_location = {
                existing_table_info
                for existing_table_info in existing_environment.promoted_snapshots
                if existing_table_info.name in new_table_infos
                and existing_table_info.qualified_view_name.for_environment(
                    existing_environment.naming_info
                )
                != new_table_infos[existing_table_info.name].qualified_view_name.for_environment(
                    plan.environment.naming_info
                )
            }
            missing_model_names = set(existing_table_infos) - {
                s.name for s in plan.environment.promoted_snapshots
            }
            demoted_snapshots = {
                existing_table_infos[name] for name in missing_model_names
            } | views_that_changed_location
        else:
            demoted_snapshots = set()

        promoted_snapshots = set(plan.environment.promoted_snapshots)
        if existing_environment and plan.environment.can_partially_promote(existing_environment):
            promoted_snapshots -= set(existing_environment.promoted_snapshots)

        demoted_environment_naming_info = (
            existing_environment.naming_info if demoted_snapshots and existing_environment else None
        )

        return (
            promoted_snapshots,
            demoted_snapshots,
            demoted_environment_naming_info,
        )

    def _missing_intervals(
        self,
        plan: EvaluatablePlan,
        snapshots_by_name: t.Dict[str, Snapshot],
        deployability_index: DeployabilityIndex,
    ) -> SnapshotToIntervals:
        return merged_missing_intervals(
            snapshots=snapshots_by_name.values(),
            start=plan.start,
            end=plan.end,
            execution_time=plan.execution_time,
            restatements={
                snapshots_by_name[name].snapshot_id: interval
                for name, interval in plan.restatements.items()
            },
            deployability_index=deployability_index,
            end_bounded=plan.end_bounded,
            ignore_cron=plan.ignore_cron,
            start_override_per_model=plan.start_override_per_model,
            end_override_per_model=plan.end_override_per_model,
        )

    def _get_audit_only_snapshots(
        self, new_snapshots: t.Dict[SnapshotId, Snapshot]
    ) -> t.Dict[SnapshotId, Snapshot]:
        metadata_snapshots = []
        for snapshot in new_snapshots.values():
            if (
                not snapshot.is_metadata
                or not snapshot.is_model
                or not snapshot.evaluatable
                or not snapshot.previous_version
            ):
                continue

            metadata_snapshots.append(snapshot)

        # Bulk load all the previous snapshots
        previous_snapshot_ids = [
            s.previous_version.snapshot_id(s.name) for s in metadata_snapshots if s.previous_version
        ]
        previous_snapshots = {
            s.name: s for s in self.state_reader.get_snapshots(previous_snapshot_ids).values()
        }

        # Check if any of the snapshots have modifications to the audits field by comparing the hashes
        audit_snapshots = {}
        for snapshot in metadata_snapshots:
            if snapshot.name not in previous_snapshots:
                continue

            previous_snapshot = previous_snapshots[snapshot.name]
            new_audits_hash = snapshot.model.audit_metadata_hash()
            previous_audit_hash = previous_snapshot.model.audit_metadata_hash()

            if snapshot.model.audits and previous_audit_hash != new_audits_hash:
                audit_snapshots[snapshot.snapshot_id] = snapshot

        return audit_snapshots

    def _get_snapshots_to_create(
        self, plan: EvaluatablePlan, snapshots: t.Dict[SnapshotId, Snapshot]
    ) -> t.List[Snapshot]:
        promoted_snapshot_ids = (
            set(plan.environment.promoted_snapshot_ids)
            if plan.environment.promoted_snapshot_ids is not None
            else None
        )

        def _should_create(s: Snapshot) -> bool:
            if not s.is_model or s.is_symbolic:
                return False
            # Only create tables for snapshots that we're planning to promote or that were selected for backfill
            return (
                plan.is_selected_for_backfill(s.name)
                or promoted_snapshot_ids is None
                or s.snapshot_id in promoted_snapshot_ids
            )

        return [s for s in snapshots.values() if _should_create(s)]

    def _adjust_intervals(
        self,
        snapshots_by_name: t.Dict[str, Snapshot],
        plan: EvaluatablePlan,
        existing_environment: t.Optional[Environment],
    ) -> None:
        # Make sure the intervals are up to date and restatements are reflected
        self.state_reader.refresh_snapshot_intervals(snapshots_by_name.values())

        if not existing_environment:
            existing_environment = self.state_reader.get_environment(c.PROD)

        if existing_environment:
            new_snapshot_ids = set()
            new_snapshot_versions = set()
            for s in snapshots_by_name.values():
                if s.is_model:
                    new_snapshot_ids.add(s.snapshot_id)
                    new_snapshot_versions.add(s.name_version)
            # Only compare to old snapshots that share the same version as the new snapshots
            old_snapshot_ids = {
                s.snapshot_id
                for s in existing_environment.snapshots
                if s.is_model
                and s.name_version in new_snapshot_versions
                and s.snapshot_id not in new_snapshot_ids
            }
            if old_snapshot_ids:
                old_snapshots = self.state_reader.get_snapshots(old_snapshot_ids)
                for old in old_snapshots.values():
                    new = snapshots_by_name.get(old.name)
                    if not new or old.version != new.version:
                        continue
                    if should_force_rebuild(old, new):
                        # If the difference between 2 snapshots requires a full rebuild,
                        # then clear the intervals for the new snapshot.
                        new.intervals = []

        for new_snapshot in plan.new_snapshots:
            if new_snapshot.is_forward_only:
                # Forward-only snapshots inherit intervals in dev because of cloning
                new_snapshot.dev_intervals = new_snapshot.intervals.copy()
        for s_name, interval in plan.restatements.items():
            snapshots_by_name[s_name].remove_interval(interval)


def build_plan_stages(
    plan: EvaluatablePlan,
    state_reader: StateReader,
    default_catalog: t.Optional[str],
) -> t.List[PlanStage]:
    return PlanStagesBuilder(state_reader, default_catalog).build(plan)
