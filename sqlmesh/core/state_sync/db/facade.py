"""
# StateSync

State sync is how SQLMesh keeps track of environments and their states, e.g. snapshots.

# StateReader

StateReader provides a subset of the functionalities of the StateSync class. As its name
implies, it only allows for read-only operations on snapshots and environment states.

# EngineAdapterStateSync

The provided `sqlmesh.core.state_sync.EngineAdapterStateSync` leverages an existing engine
adapter to read and write state to the underlying data store.
"""

from __future__ import annotations

import contextlib
import logging
import typing as t
from pathlib import Path
from datetime import datetime


from sqlmesh.core.console import Console, get_console
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.environment import Environment, EnvironmentStatements, EnvironmentSummary
from sqlmesh.core.snapshot import (
    Snapshot,
    SnapshotIdAndVersion,
    SnapshotId,
    SnapshotIdLike,
    SnapshotIdAndVersionLike,
    SnapshotInfoLike,
    SnapshotIntervals,
    SnapshotNameVersion,
    SnapshotTableInfo,
    start_date,
)
from sqlmesh.core.snapshot.definition import (
    Interval,
)
from sqlmesh.core.state_sync.base import (
    StateSync,
    Versions,
)
from sqlmesh.core.state_sync.common import (
    EnvironmentsChunk,
    SnapshotsChunk,
    VersionsChunk,
    transactional,
    StateStream,
    chunk_iterable,
    EnvironmentWithStatements,
    ExpiredSnapshotBatch,
    PromotionResult,
    ExpiredBatchRange,
)
from sqlmesh.core.state_sync.db.interval import IntervalState
from sqlmesh.core.state_sync.db.environment import EnvironmentState
from sqlmesh.core.state_sync.db.snapshot import SnapshotState
from sqlmesh.core.state_sync.db.version import VersionState
from sqlmesh.core.state_sync.db.migrator import StateMigrator, _backup_table_name
from sqlmesh.utils.date import TimeLike, to_timestamp, time_like_to_str, now_timestamp
from sqlmesh.utils.errors import ConflictingPlanError, SQLMeshError

logger = logging.getLogger(__name__)


T = t.TypeVar("T")


class EngineAdapterStateSync(StateSync):
    """Manages state of nodes and snapshot with an existing engine adapter.

    This state sync is convenient to use because it requires no additional setup.
    You can reuse the same engine/warehouse that your data is stored in.

    Args:
        engine_adapter: The EngineAdapter to use to store and fetch snapshots.
        schema: The schema to store state metadata in. If None or empty string then no schema is defined
        console: The console to log information to.
        cache_dir: The cache path, used for caching snapshot models.
    """

    def __init__(
        self,
        engine_adapter: EngineAdapter,
        schema: t.Optional[str],
        console: t.Optional[Console] = None,
        cache_dir: Path = Path(),
    ):
        self.interval_state = IntervalState(engine_adapter, schema=schema)
        self.environment_state = EnvironmentState(engine_adapter, schema=schema)
        self.snapshot_state = SnapshotState(engine_adapter, schema=schema, cache_dir=cache_dir)
        self.version_state = VersionState(engine_adapter, schema=schema)
        self.migrator = StateMigrator(
            engine_adapter,
            version_state=self.version_state,
            snapshot_state=self.snapshot_state,
            environment_state=self.environment_state,
            interval_state=self.interval_state,
            console=console,
        )
        # Make sure that if an empty string is provided that we treat it as None
        self.schema = schema or None
        self.engine_adapter = engine_adapter
        self.console = console or get_console()

    @transactional()
    def push_snapshots(self, snapshots: t.Iterable[Snapshot]) -> None:
        """Pushes snapshots to the state store, merging them with existing ones.

        This method first finds all existing snapshots in the store and merges them with
        the local snapshots. It will then delete all existing snapshots and then
        insert all the local snapshots. This can be made safer with locks or merge/upsert.

        Args:
            snapshots: The snapshots to push.
        """
        snapshots_by_id = {}
        for snapshot in snapshots:
            if not snapshot.version:
                raise SQLMeshError(
                    f"Snapshot {snapshot} has not been versioned yet. Create a plan before pushing a snapshot."
                )
            snapshots_by_id[snapshot.snapshot_id] = snapshot

        existing = self.snapshots_exist(snapshots_by_id)

        if existing:
            logger.error(
                "Snapshots %s already exists. This could be due to a concurrent plan or a hash collision. If this is a hash collision, add a stamp to your model.",
                str(existing),
            )

            for sid in tuple(snapshots_by_id):
                if sid in existing:
                    snapshots_by_id.pop(sid)

        snapshots = snapshots_by_id.values()
        if snapshots:
            self.snapshot_state.push_snapshots(snapshots)

    @transactional()
    def promote(
        self,
        environment: Environment,
        no_gaps_snapshot_names: t.Optional[t.Set[str]] = None,
        environment_statements: t.Optional[t.List[EnvironmentStatements]] = None,
    ) -> PromotionResult:
        """Update the environment to reflect the current state.

        This method verifies that snapshots have been pushed.

        Args:
            environment: The environment to promote.
            no_gaps_snapshot_names: A set of snapshot names to check for data gaps. If None,
                all snapshots will be checked. The data gap check ensures that models that are already a
                part of the target environment have no data gaps when compared against previous
                snapshots for same models.

        Returns:
           A tuple of (added snapshot table infos, removed snapshot table infos, and environment target suffix for the removed table infos)
        """
        logger.info("Promoting environment '%s'", environment.name)

        missing = {s.snapshot_id for s in environment.snapshots} - self.snapshots_exist(
            environment.snapshots
        )
        if missing:
            raise SQLMeshError(
                f"Missing snapshots {missing}. Make sure to push and backfill your snapshots."
            )

        existing_environment = self.environment_state.get_environment(
            environment.name, lock_for_update=True
        )

        existing_table_infos = (
            {table_info.name: table_info for table_info in existing_environment.promoted_snapshots}
            if existing_environment
            else {}
        )
        table_infos = {table_info.name: table_info for table_info in environment.promoted_snapshots}
        views_that_changed_location: t.Set[SnapshotTableInfo] = set()
        if existing_environment:
            views_that_changed_location = {
                existing_table_info
                for name, existing_table_info in existing_table_infos.items()
                if name in table_infos
                and existing_table_info.qualified_view_name.for_environment(
                    existing_environment.naming_info
                )
                != table_infos[name].qualified_view_name.for_environment(environment.naming_info)
            }
            if not existing_environment.expired:
                if environment.previous_plan_id != existing_environment.plan_id:
                    raise ConflictingPlanError(
                        f"Another plan ({existing_environment.plan_id}) was applied to the target environment '{environment.name}' while your current plan "
                        f"({environment.plan_id}) was still in progress, interrupting it. Please re-apply your plan to resolve this error."
                    )
                if no_gaps_snapshot_names != set():
                    snapshots = self.get_snapshots(environment.snapshots).values()
                    self._ensure_no_gaps(
                        snapshots,
                        existing_environment,
                        no_gaps_snapshot_names,
                    )
            demoted_snapshots = set(existing_environment.snapshots) - set(environment.snapshots)
            # Update the updated_at attribute.
            self.snapshot_state.touch_snapshots(demoted_snapshots)

        missing_models = set(existing_table_infos) - {
            snapshot.name for snapshot in environment.promoted_snapshots
        }

        added_table_infos = set(table_infos.values())
        if existing_environment and environment.can_partially_promote(existing_environment):
            # Only promote new snapshots.
            added_table_infos -= set(existing_environment.promoted_snapshots)

        self.environment_state.update_environment(environment)

        # If it is an empty list, we want to update the environment statements
        # To reflect there are no statements anymore in this environment
        if environment_statements is not None:
            self.environment_state.update_environment_statements(
                environment.name, environment.plan_id, environment_statements
            )

        removed = {existing_table_infos[name] for name in missing_models}.union(
            views_that_changed_location
        )

        return PromotionResult(
            added=sorted(added_table_infos),
            removed=list(removed),
            removed_environment_naming_info=(
                existing_environment.naming_info if removed and existing_environment else None
            ),
        )

    @transactional()
    def finalize(self, environment: Environment) -> None:
        """Finalize the target environment, indicating that this environment has been
        fully promoted and is ready for use.

        Args:
            environment: The target environment to finalize.
        """
        self.environment_state.finalize(environment)

    @transactional()
    def unpause_snapshots(
        self, snapshots: t.Collection[SnapshotInfoLike], unpaused_dt: TimeLike
    ) -> None:
        self.snapshot_state.unpause_snapshots(snapshots, unpaused_dt)

    def invalidate_environment(self, name: str, protect_prod: bool = True) -> None:
        self.environment_state.invalidate_environment(name, protect_prod)

    def get_expired_snapshots(
        self,
        *,
        batch_range: ExpiredBatchRange,
        current_ts: t.Optional[int] = None,
        ignore_ttl: bool = False,
    ) -> t.Optional[ExpiredSnapshotBatch]:
        current_ts = current_ts or now_timestamp()
        return self.snapshot_state.get_expired_snapshots(
            environments=self.environment_state.get_environments(),
            current_ts=current_ts,
            ignore_ttl=ignore_ttl,
            batch_range=batch_range,
        )

    def get_expired_environments(
        self, current_ts: int, name: t.Optional[str] = None
    ) -> t.List[EnvironmentSummary]:
        return self.environment_state.get_expired_environments(current_ts=current_ts, name=name)

    @transactional()
    def delete_expired_snapshots(
        self,
        batch_range: ExpiredBatchRange,
        ignore_ttl: bool = False,
        current_ts: t.Optional[int] = None,
    ) -> None:
        batch = self.get_expired_snapshots(
            ignore_ttl=ignore_ttl,
            current_ts=current_ts,
            batch_range=batch_range,
        )
        if batch and batch.expired_snapshot_ids:
            self.snapshot_state.delete_snapshots(batch.expired_snapshot_ids)
            self.interval_state.cleanup_intervals(batch.cleanup_tasks, batch.expired_snapshot_ids)

    @transactional()
    def delete_expired_environments(
        self, current_ts: t.Optional[int] = None, name: t.Optional[str] = None
    ) -> t.List[EnvironmentSummary]:
        current_ts = current_ts or now_timestamp()
        return self.environment_state.delete_expired_environments(current_ts=current_ts, name=name)

    def delete_snapshots(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> None:
        self.snapshot_state.delete_snapshots(snapshot_ids)

    def snapshots_exist(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> t.Set[SnapshotId]:
        return self.snapshot_state.snapshots_exist(snapshot_ids)

    def nodes_exist(self, names: t.Iterable[str], exclude_external: bool = False) -> t.Set[str]:
        return self.snapshot_state.nodes_exist(names, exclude_external)

    def remove_state(self, including_backup: bool = False) -> None:
        """Removes the state store objects."""
        for table in (
            self.snapshot_state.snapshots_table,
            self.snapshot_state.auto_restatements_table,
            self.environment_state.environments_table,
            self.environment_state.environment_statements_table,
            self.interval_state.intervals_table,
            self.version_state.versions_table,
        ):
            self.engine_adapter.drop_table(table)
            if including_backup:
                self.engine_adapter.drop_table(_backup_table_name(table))

        self.snapshot_state.clear_cache()

    def reset(self, default_catalog: t.Optional[str]) -> None:
        """Resets the state store to the state when it was first initialized."""
        self.remove_state()
        self.migrate(default_catalog)

    @transactional()
    def update_auto_restatements(
        self, next_auto_restatement_ts: t.Dict[SnapshotNameVersion, t.Optional[int]]
    ) -> None:
        self.snapshot_state.update_auto_restatements(next_auto_restatement_ts)

    def get_environment(self, environment: str) -> t.Optional[Environment]:
        return self.environment_state.get_environment(environment)

    def get_environment_statements(self, environment: str) -> t.List[EnvironmentStatements]:
        return self.environment_state.get_environment_statements(environment)

    def get_environments(self) -> t.List[Environment]:
        """Fetches all environments.

        Returns:
            A list of all environments.
        """
        return self.environment_state.get_environments()

    def get_environments_summary(self) -> t.List[EnvironmentSummary]:
        """Fetches all environment names along with expiry datetime.

        Returns:
            A list of all environment summaries.
        """
        return self.environment_state.get_environments_summary()

    def get_snapshots(
        self,
        snapshot_ids: t.Iterable[SnapshotIdLike],
    ) -> t.Dict[SnapshotId, Snapshot]:
        """Fetches snapshots from the state.

        Args:
            snapshot_ids: The snapshot IDs to fetch.

        Returns:
            A dict of snapshots.
        """
        snapshots = self.snapshot_state.get_snapshots(snapshot_ids)
        intervals = self.interval_state.get_snapshot_intervals(snapshots.values())
        Snapshot.hydrate_with_intervals_by_version(snapshots.values(), intervals)
        return snapshots

    def get_snapshots_by_names(
        self,
        snapshot_names: t.Iterable[str],
        current_ts: t.Optional[int] = None,
        exclude_expired: bool = True,
    ) -> t.Set[SnapshotIdAndVersion]:
        return self.snapshot_state.get_snapshots_by_names(
            snapshot_names=snapshot_names, current_ts=current_ts, exclude_expired=exclude_expired
        )

    @transactional()
    def add_interval(
        self,
        snapshot: Snapshot,
        start: TimeLike,
        end: TimeLike,
        is_dev: bool = False,
        last_altered_ts: t.Optional[int] = None,
    ) -> None:
        super().add_interval(snapshot, start, end, is_dev, last_altered_ts)

    @transactional()
    def add_snapshots_intervals(self, snapshots_intervals: t.Sequence[SnapshotIntervals]) -> None:
        intervals_to_insert = []
        for snapshot_intervals in snapshots_intervals:
            snapshot_intervals = snapshot_intervals.copy(
                update={
                    "intervals": _remove_partial_intervals(
                        snapshot_intervals.intervals, snapshot_intervals.snapshot_id, is_dev=False
                    ),
                    "dev_intervals": _remove_partial_intervals(
                        snapshot_intervals.dev_intervals,
                        snapshot_intervals.snapshot_id,
                        is_dev=True,
                    ),
                }
            )
            if not snapshot_intervals.is_empty():
                intervals_to_insert.append(snapshot_intervals)
        if intervals_to_insert:
            self.interval_state.add_snapshots_intervals(intervals_to_insert)

    @transactional()
    def remove_intervals(
        self,
        snapshot_intervals: t.Sequence[t.Tuple[SnapshotIdAndVersionLike, Interval]],
        remove_shared_versions: bool = False,
    ) -> None:
        self.interval_state.remove_intervals(snapshot_intervals, remove_shared_versions)

    @transactional()
    def compact_intervals(self) -> None:
        self.interval_state.compact_intervals()

    def refresh_snapshot_intervals(self, snapshots: t.Collection[Snapshot]) -> t.List[Snapshot]:
        return self.interval_state.refresh_snapshot_intervals(snapshots)

    def max_interval_end_per_model(
        self,
        environment: str,
        models: t.Optional[t.Set[str]] = None,
        ensure_finalized_snapshots: bool = False,
    ) -> t.Dict[str, int]:
        env = self.get_environment(environment)
        if not env:
            return {}

        snapshots = (
            env.snapshots if not ensure_finalized_snapshots else env.finalized_or_current_snapshots
        )
        if models is not None:
            snapshots = [s for s in snapshots if s.name in models]

        if not snapshots:
            return {}

        return self.interval_state.max_interval_end_per_model(snapshots)

    def recycle(self) -> None:
        self.engine_adapter.recycle()

    def close(self) -> None:
        self.engine_adapter.close()

    @transactional()
    def migrate(
        self,
        skip_backup: bool = False,
        promoted_snapshots_only: bool = True,
    ) -> None:
        """Migrate the state sync to the latest SQLMesh / SQLGlot version."""
        self.migrator.migrate(
            self.schema,
            skip_backup=skip_backup,
            promoted_snapshots_only=promoted_snapshots_only,
        )

    @transactional()
    def rollback(self) -> None:
        """Rollback to the previous migration."""
        self.migrator.rollback()

    @transactional()
    def export(self, environment_names: t.Optional[t.List[str]] = None) -> StateStream:
        versions = self.get_versions(
            validate=True
        )  # will throw if the state db hasnt been created or there is a version mismatch

        snapshot_ids_to_export: t.Set[SnapshotId] = set()
        selected_environments: t.List[Environment] = []
        if environment_names:
            for env_name in environment_names:
                environment = self.get_environment(env_name)
                if not environment:
                    raise SQLMeshError(f"No such environment: {env_name}")
                selected_environments.append(environment)
        else:
            selected_environments = self.get_environments()

        for env in selected_environments:
            snapshot_ids_to_export |= set([s.snapshot_id for s in env.snapshots or []])

        def _export_snapshots() -> t.Iterator[Snapshot]:
            for chunk in chunk_iterable(snapshot_ids_to_export, SnapshotState.SNAPSHOT_BATCH_SIZE):
                yield from self.get_snapshots(chunk).values()

        def _export_environments() -> t.Iterator[EnvironmentWithStatements]:
            for env in selected_environments:
                yield EnvironmentWithStatements(
                    environment=env, statements=self.get_environment_statements(env.name)
                )

        return StateStream.from_iterators(
            versions=versions,
            snapshots=_export_snapshots(),
            environments=_export_environments(),
        )

    @transactional()
    def import_(self, stream: StateStream, clear: bool = True) -> None:
        existing_versions = self.get_versions()

        for state_chunk in stream:
            if isinstance(state_chunk, VersionsChunk):
                # SQLMesh major/minor version must match so that we can be sure the JSON contained in the state file
                # is compatible with our Pydantic model definitions. Patch versions dont need to match because the assumption
                # is that they dont contain any breaking changes
                incoming_versions = state_chunk.versions
                if (
                    incoming_versions.minor_sqlmesh_version
                    != existing_versions.minor_sqlmesh_version
                ):
                    raise SQLMeshError(
                        f"SQLMesh version mismatch. You are running '{existing_versions.sqlmesh_version}' but the state file was created with '{incoming_versions.sqlmesh_version}'.\n"
                        "Please upgrade/downgrade your SQLMesh version to match the state file before performing the import."
                    )

                if clear:
                    self.reset(default_catalog=None)

            if isinstance(state_chunk, SnapshotsChunk):
                auto_restatements: t.Dict[SnapshotNameVersion, t.Optional[int]] = {}

                for snapshot_chunk in chunk_iterable(
                    state_chunk, SnapshotState.SNAPSHOT_BATCH_SIZE
                ):
                    snapshot_chunk = list(snapshot_chunk)
                    overwrite_existing_snapshots = (
                        not clear
                    )  # if clear=True, all existing snapshots were dropped anyway
                    self.snapshot_state.push_snapshots(
                        snapshot_chunk, overwrite=overwrite_existing_snapshots
                    )
                    self.add_snapshots_intervals((s.snapshot_intervals for s in snapshot_chunk))

                    auto_restatements.update(
                        {
                            s.name_version: s.next_auto_restatement_ts
                            for s in snapshot_chunk
                            if s.next_auto_restatement_ts
                        }
                    )

                self.update_auto_restatements(auto_restatements)

            if isinstance(state_chunk, EnvironmentsChunk):
                for environment_with_statements in state_chunk:
                    environment = environment_with_statements.environment
                    self.environment_state.update_environment(environment)
                    self.environment_state.update_environment_statements(
                        environment.name,
                        environment.plan_id,
                        environment_with_statements.statements,
                    )

    def state_type(self) -> str:
        return self.engine_adapter.dialect

    def _get_versions(self) -> Versions:
        return self.version_state.get_versions()

    def _ensure_no_gaps(
        self,
        target_snapshots: t.Iterable[Snapshot],
        target_environment: Environment,
        snapshot_names: t.Optional[t.Set[str]],
    ) -> None:
        target_snapshots_by_name = {s.name: s for s in target_snapshots}

        changed_version_prev_snapshots_by_name = {
            s.name: s
            for s in target_environment.snapshots
            if s.name in target_snapshots_by_name
            and target_snapshots_by_name[s.name].version != s.version
        }

        prev_snapshots = self.get_snapshots(
            changed_version_prev_snapshots_by_name.values()
        ).values()
        cache: t.Dict[str, datetime] = {}

        for prev_snapshot in prev_snapshots:
            target_snapshot = target_snapshots_by_name[prev_snapshot.name]
            if (
                (snapshot_names is None or prev_snapshot.name in snapshot_names)
                and target_snapshot.is_incremental
                and prev_snapshot.is_incremental
                and prev_snapshot.intervals
            ):
                start = to_timestamp(
                    start_date(target_snapshot, target_snapshots_by_name.values(), cache)
                )
                end = prev_snapshot.intervals[-1][1]

                if start < end:
                    missing_intervals = target_snapshot.missing_intervals(
                        start, end, end_bounded=True
                    )

                    if missing_intervals:
                        raise SQLMeshError(
                            f"Detected missing intervals for model {target_snapshot.name}, interrupting your current plan. "
                            "Please re-apply your plan to resolve this error."
                        )

    @contextlib.contextmanager
    def _transaction(self) -> t.Iterator[None]:
        with self.engine_adapter.transaction():
            yield


def _remove_partial_intervals(
    intervals: t.List[Interval], snapshot_id: t.Optional[SnapshotId], *, is_dev: bool
) -> t.List[Interval]:
    results = []
    for start_ts, end_ts in intervals:
        if start_ts < end_ts:
            logger.info(
                "Adding %s (%s, %s) for snapshot %s",
                "dev interval" if is_dev else "interval",
                time_like_to_str(start_ts),
                time_like_to_str(end_ts),
                snapshot_id,
            )
            results.append((start_ts, end_ts))
        else:
            logger.info(
                "Skipping partial interval (%s, %s) for snapshot %s",
                start_ts,
                end_ts,
                snapshot_id,
            )
    return results
