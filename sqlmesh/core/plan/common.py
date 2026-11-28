from __future__ import annotations
import typing as t
import logging
from dataclasses import dataclass, field

from sqlmesh.core.state_sync import StateReader
from sqlmesh.core.snapshot import Snapshot, SnapshotId, SnapshotIdAndVersion, SnapshotNameVersion
from sqlmesh.core.snapshot.definition import Interval
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import now_timestamp

logger = logging.getLogger(__name__)


def should_force_rebuild(old: Snapshot, new: Snapshot) -> bool:
    if new.is_view and new.is_indirect_non_breaking and not new.is_forward_only:
        # View models always need to be rebuilt to reflect updated upstream dependencies
        return True
    if new.is_seed and not (
        new.is_metadata
        and new.previous_version
        and new.previous_version.snapshot_id(new.name) == old.snapshot_id
    ):
        # Seed models always need to be rebuilt to reflect changes in the seed file
        # Unless only their metadata has been updated (eg description added) and the seed file has not been touched
        return True
    return is_breaking_kind_change(old, new)


def is_breaking_kind_change(old: Snapshot, new: Snapshot) -> bool:
    if new.is_model != old.is_model:
        # If one is a model and the other isn't, then we need to rebuild
        return True
    if not new.is_model or not old.is_model:
        # If neither are models, then we don't need to rebuild
        # Note that the remaining checks only apply to model snapshots
        return False
    if old.virtual_environment_mode != new.virtual_environment_mode:
        # If the virtual environment mode has changed, then we need to rebuild
        return True
    if old.model.kind.name == new.model.kind.name:
        # If the kind hasn't changed, then we don't need to rebuild
        return False
    if not old.is_incremental or not new.is_incremental:
        # If either is not incremental, then we need to rebuild
        return True
    if old.model.partitioned_by == new.model.partitioned_by:
        # If the partitioning hasn't changed, then we don't need to rebuild
        return False
    return True


@dataclass
class SnapshotIntervalClearRequest:
    # affected snapshot
    snapshot: SnapshotIdAndVersion

    # which interval to clear
    interval: Interval

    # which environments this snapshot is currently promoted
    # note that this can be empty if the snapshot exists because its ttl has not expired
    # but it is not part of any particular environment
    environment_names: t.Set[str] = field(default_factory=set)

    @property
    def snapshot_id(self) -> SnapshotId:
        return self.snapshot.snapshot_id

    @property
    def sorted_environment_names(self) -> t.List[str]:
        return list(sorted(self.environment_names))


def identify_restatement_intervals_across_snapshot_versions(
    state_reader: StateReader,
    prod_restatements: t.Dict[str, Interval],
    disable_restatement_models: t.Set[str],
    loaded_snapshots: t.Dict[SnapshotId, Snapshot],
    current_ts: t.Optional[int] = None,
) -> t.Dict[SnapshotId, SnapshotIntervalClearRequest]:
    """
    Given a map of snapshot names + intervals to restate in prod:
        - Look up matching snapshots (match based on name - regardless of version, to get all versions)
        - For each match, also match downstream snapshots in each dev environment while filtering out models that have restatement disabled
        - Return a list of all snapshots that are affected + the interval that needs to be cleared for each

    The goal here is to produce a list of intervals to invalidate across all dev snapshots so that a subsequent plan or
    cadence run in those environments causes the intervals to be repopulated.
    """
    if not prod_restatements:
        return {}

    # Although :loaded_snapshots is sourced from RestatementStage.all_snapshots, since the only time we ever need
    # to clear intervals across all environments is for prod, the :loaded_snapshots here are always from prod
    prod_name_versions: t.Set[SnapshotNameVersion] = {
        s.name_version for s in loaded_snapshots.values()
    }

    snapshot_intervals_to_clear: t.Dict[SnapshotId, SnapshotIntervalClearRequest] = {}

    for env_summary in state_reader.get_environments_summary():
        # Fetch the full environment object one at a time to avoid loading all environments into memory at once
        env = state_reader.get_environment(env_summary.name)
        if not env:
            logger.warning("Environment %s not found", env_summary.name)
            continue

        snapshots_by_name = {s.name: s.table_info for s in env.snapshots}

        # We dont just restate matching snapshots, we also have to restate anything downstream of them
        # so that if A gets restated in prod and dev has A <- B <- C, B and C get restated in dev
        env_dag = DAG({s.name: {p.name for p in s.parents} for s in env.snapshots})

        for restate_snapshot_name, interval in prod_restatements.items():
            if restate_snapshot_name not in snapshots_by_name:
                # snapshot is not promoted in this environment
                continue

            affected_snapshot_names = [
                x
                for x in ([restate_snapshot_name] + env_dag.downstream(restate_snapshot_name))
                if x not in disable_restatement_models
            ]

            for affected_snapshot_name in affected_snapshot_names:
                affected_snapshot = snapshots_by_name[affected_snapshot_name]

                # Don't clear intervals for a dev snapshot if it shares the same physical version with prod.
                # Otherwise, prod will be affected by what should be a dev operation
                if affected_snapshot.name_version in prod_name_versions:
                    continue

                clear_request = snapshot_intervals_to_clear.get(affected_snapshot.snapshot_id)
                if not clear_request:
                    clear_request = SnapshotIntervalClearRequest(
                        snapshot=affected_snapshot.id_and_version, interval=interval
                    )
                    snapshot_intervals_to_clear[affected_snapshot.snapshot_id] = clear_request
                else:
                    clear_request.interval = (
                        min(clear_request.interval[0], interval[0]),
                        max(clear_request.interval[1], interval[1]),
                    )

                clear_request.environment_names |= set([env.name])

    # snapshot_intervals_to_clear now contains the entire hierarchy of affected snapshots based
    # on building the DAG for each environment and including downstream snapshots
    # but, what if there are affected snapshots that arent part of any environment?
    unique_snapshot_names = set(snapshot_id.name for snapshot_id in snapshot_intervals_to_clear)

    current_ts = current_ts or now_timestamp()
    all_matching_non_prod_snapshots = {
        s.snapshot_id: s
        for s in state_reader.get_snapshots_by_names(
            snapshot_names=unique_snapshot_names, current_ts=current_ts, exclude_expired=True
        )
        # Don't clear intervals for a snapshot if it shares the same physical version with prod.
        # Otherwise, prod will be affected by what should be a dev operation
        if s.name_version not in prod_name_versions
    }

    # identify the ones that we havent picked up yet, which are the ones that dont exist in any environment
    if remaining_snapshot_ids := set(all_matching_non_prod_snapshots).difference(
        snapshot_intervals_to_clear
    ):
        # these snapshot id's exist in isolation and may be related to a downstream dependency of the :prod_restatements,
        # rather than directly related, so we can't simply look up the interval to clear based on :prod_restatements.
        # To figure out the interval that should be cleared, we can match to the existing list based on name
        # and conservatively take the widest interval that shows up
        snapshot_name_to_widest_interval: t.Dict[str, Interval] = {}
        for s_id, clear_request in snapshot_intervals_to_clear.items():
            current_start, current_end = snapshot_name_to_widest_interval.get(
                s_id.name, clear_request.interval
            )
            next_start, next_end = clear_request.interval

            next_start = min(current_start, next_start)
            next_end = max(current_end, next_end)

            snapshot_name_to_widest_interval[s_id.name] = (next_start, next_end)

        for remaining_snapshot_id in remaining_snapshot_ids:
            remaining_snapshot = all_matching_non_prod_snapshots[remaining_snapshot_id]
            snapshot_intervals_to_clear[remaining_snapshot_id] = SnapshotIntervalClearRequest(
                snapshot=remaining_snapshot,
                interval=snapshot_name_to_widest_interval[remaining_snapshot_id.name],
            )

    # for any affected full_history_restatement_only snapshots, we need to widen the intervals being restated to
    # include the whole time range for that snapshot. This requires a call to state to load the full snapshot record,
    # so we only do it if necessary
    full_history_restatement_snapshot_ids = [
        # FIXME: full_history_restatement_only is just one indicator that the snapshot can only be fully refreshed, the other one is Model.depends_on_self
        # however, to figure out depends_on_self, we have to render all the model queries which, alongside having to fetch full snapshots from state,
        # is problematic in secure environments that are deliberately isolated from arbitrary user code (since rendering a query may require user macros to be present)
        # So for now, these are not considered
        s_id
        for s_id, s in snapshot_intervals_to_clear.items()
        if s.snapshot.full_history_restatement_only
    ]
    if full_history_restatement_snapshot_ids:
        # only load full snapshot records that we havent already loaded
        additional_snapshots = state_reader.get_snapshots(
            [
                s.snapshot_id
                for s in full_history_restatement_snapshot_ids
                if s.snapshot_id not in loaded_snapshots
            ]
        )

        all_snapshots = loaded_snapshots | additional_snapshots

        for full_snapshot_id in full_history_restatement_snapshot_ids:
            full_snapshot = all_snapshots[full_snapshot_id]
            intervals_to_clear = snapshot_intervals_to_clear[full_snapshot_id]

            original_start, original_end = intervals_to_clear.interval

            # get_removal_interval() widens intervals if necessary
            new_interval = full_snapshot.get_removal_interval(
                start=original_start, end=original_end
            )

            intervals_to_clear.interval = new_interval

    return snapshot_intervals_to_clear
