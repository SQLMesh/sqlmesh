from __future__ import annotations

import typing as t
import json
import logging
from pathlib import Path
from collections import defaultdict
from sqlglot import exp

from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.state_sync.db.utils import (
    snapshot_name_filter,
    snapshot_name_version_filter,
    snapshot_id_filter,
    fetchone,
    fetchall,
)
from sqlmesh.core.environment import Environment
from sqlmesh.core.model import SeedModel, ModelKindName
from sqlmesh.core.snapshot.cache import SnapshotCache
from sqlmesh.core.snapshot import (
    SnapshotIdLike,
    SnapshotNameVersionLike,
    SnapshotTableCleanupTask,
    SnapshotNameVersion,
    SnapshotInfoLike,
    Snapshot,
    SnapshotIdAndVersion,
    SnapshotId,
    SnapshotFingerprint,
)
from sqlmesh.core.state_sync.common import (
    RowBoundary,
    ExpiredSnapshotBatch,
    ExpiredBatchRange,
    LimitBoundary,
)
from sqlmesh.utils.migration import index_text_type, blob_text_type
from sqlmesh.utils.date import now_timestamp, TimeLike, to_timestamp
from sqlmesh.utils import unique

if t.TYPE_CHECKING:
    import pandas as pd


logger = logging.getLogger(__name__)


class SnapshotState:
    SNAPSHOT_BATCH_SIZE = 1000

    def __init__(
        self,
        engine_adapter: EngineAdapter,
        schema: t.Optional[str] = None,
        cache_dir: Path = Path(),
    ):
        self.engine_adapter = engine_adapter
        self.snapshots_table = exp.table_("_snapshots", db=schema)
        self.auto_restatements_table = exp.table_("_auto_restatements", db=schema)

        index_type = index_text_type(engine_adapter.dialect)
        blob_type = blob_text_type(engine_adapter.dialect)
        self._snapshot_columns_to_types = {
            "name": exp.DataType.build(index_type),
            "identifier": exp.DataType.build(index_type),
            "version": exp.DataType.build(index_type),
            "dev_version": exp.DataType.build(index_type),
            "snapshot": exp.DataType.build(blob_type),
            "kind_name": exp.DataType.build("text"),
            "updated_ts": exp.DataType.build("bigint"),
            "unpaused_ts": exp.DataType.build("bigint"),
            "ttl_ms": exp.DataType.build("bigint"),
            "unrestorable": exp.DataType.build("boolean"),
            "forward_only": exp.DataType.build("boolean"),
            "fingerprint": exp.DataType.build(blob_type),
        }

        self._auto_restatement_columns_to_types = {
            "snapshot_name": exp.DataType.build(index_type),
            "snapshot_version": exp.DataType.build(index_type),
            "next_auto_restatement_ts": exp.DataType.build("bigint"),
        }

        self._snapshot_cache = SnapshotCache(cache_dir)

    def push_snapshots(self, snapshots: t.Iterable[Snapshot], overwrite: bool = False) -> None:
        """Pushes snapshots to the state store.

        Args:
            snapshots: The snapshots to push.
            overwrite: Whether to overwrite existing snapshots.
        """
        if overwrite:
            snapshots = tuple(snapshots)
            self.delete_snapshots(snapshots)

        snapshots_to_store = []

        for snapshot in snapshots:
            if isinstance(snapshot.node, SeedModel):
                seed_model = t.cast(SeedModel, snapshot.node)
                snapshot = snapshot.copy(update={"node": seed_model.to_dehydrated()})
            snapshots_to_store.append(snapshot)

        self.engine_adapter.insert_append(
            self.snapshots_table,
            _snapshots_to_df(snapshots_to_store),
            target_columns_to_types=self._snapshot_columns_to_types,
            track_rows_processed=False,
        )

        for snapshot in snapshots:
            self._snapshot_cache.put(snapshot)

    def unpause_snapshots(
        self,
        snapshots: t.Collection[SnapshotInfoLike],
        unpaused_dt: TimeLike,
    ) -> None:
        unrestorable_snapshots_by_forward_only: t.Dict[bool, t.List[SnapshotNameVersion]] = (
            defaultdict(list)
        )

        for snapshot in snapshots:
            # We need to mark all other snapshots that have forward-only opposite to the target snapshot as unrestorable
            unrestorable_snapshots_by_forward_only[not snapshot.is_forward_only].append(
                snapshot.name_version
            )

        updated_ts = now_timestamp()
        unpaused_ts = to_timestamp(unpaused_dt)

        # Pause all snapshots with target names first
        for where in snapshot_name_filter(
            [s.name for s in snapshots],
            batch_size=self.SNAPSHOT_BATCH_SIZE,
        ):
            self.engine_adapter.update_table(
                self.snapshots_table,
                {"unpaused_ts": None},
                where=where,
            )

        # Now unpause the target snapshots
        self._update_snapshots(
            [s.snapshot_id for s in snapshots],
            unpaused_ts=unpaused_ts,
            updated_ts=updated_ts,
        )

        # Mark unrestorable snapshots
        for forward_only, snapshot_name_versions in unrestorable_snapshots_by_forward_only.items():
            forward_only_exp = exp.column("forward_only").is_(exp.convert(forward_only))
            for where in snapshot_name_version_filter(
                self.engine_adapter,
                snapshot_name_versions,
                batch_size=self.SNAPSHOT_BATCH_SIZE,
                alias=None,
            ):
                self.engine_adapter.update_table(
                    self.snapshots_table,
                    {"unrestorable": True, "updated_ts": updated_ts},
                    where=forward_only_exp.and_(where),
                )

    def get_expired_snapshots(
        self,
        environments: t.Iterable[Environment],
        current_ts: int,
        ignore_ttl: bool,
        batch_range: ExpiredBatchRange,
    ) -> t.Optional[ExpiredSnapshotBatch]:
        expired_query = exp.select("name", "identifier", "version", "updated_ts").from_(
            self.snapshots_table
        )

        if not ignore_ttl:
            expired_query = expired_query.where(
                (exp.column("updated_ts") + exp.column("ttl_ms")) <= current_ts
            )

        expired_query = expired_query.where(batch_range.where_filter)

        promoted_snapshot_ids = {
            snapshot.snapshot_id
            for environment in environments
            for snapshot in (
                environment.snapshots
                if environment.finalized_ts is not None
                # If the environment is not finalized, check both the current snapshots and the previous finalized snapshots
                else [*environment.snapshots, *(environment.previous_finalized_snapshots or [])]
            )
        }

        if promoted_snapshot_ids:
            not_in_conditions = [
                exp.not_(condition)
                for condition in snapshot_id_filter(
                    self.engine_adapter,
                    promoted_snapshot_ids,
                    batch_size=self.SNAPSHOT_BATCH_SIZE,
                )
            ]
            expired_query = expired_query.where(exp.and_(*not_in_conditions))

        expired_query = expired_query.order_by(
            exp.column("updated_ts"), exp.column("name"), exp.column("identifier")
        )

        if isinstance(batch_range.end, LimitBoundary):
            expired_query = expired_query.limit(batch_range.end.batch_size)

        rows = fetchall(self.engine_adapter, expired_query)

        if not rows:
            return None

        expired_candidates = {
            SnapshotId(name=name, identifier=identifier): SnapshotNameVersion(
                name=name, version=version
            )
            for name, identifier, version, _ in rows
        }
        if not expired_candidates:
            return None

        def _is_snapshot_used(snapshot: SnapshotIdAndVersion) -> bool:
            return (
                snapshot.snapshot_id in promoted_snapshot_ids
                or snapshot.snapshot_id not in expired_candidates
            )

        # Extract cursor values from last row for pagination
        last_row = rows[-1]
        last_row_boundary = RowBoundary(
            updated_ts=last_row[3],
            name=last_row[0],
            identifier=last_row[1],
        )
        # The returned batch_range represents the actual range of rows in this batch
        result_batch_range = ExpiredBatchRange(
            start=batch_range.start,
            end=last_row_boundary,
        )

        unique_expired_versions = unique(expired_candidates.values())
        expired_snapshot_ids: t.Set[SnapshotId] = set()
        cleanup_tasks: t.List[SnapshotTableCleanupTask] = []

        snapshots = self._get_snapshots_with_same_version(unique_expired_versions)

        snapshots_by_version = defaultdict(set)
        snapshots_by_dev_version = defaultdict(set)
        for s in snapshots:
            snapshots_by_version[(s.name, s.version)].add(s.snapshot_id)
            snapshots_by_dev_version[(s.name, s.dev_version)].add(s.snapshot_id)

        expired_snapshots = [s for s in snapshots if not _is_snapshot_used(s)]
        all_expired_snapshot_ids = {s.snapshot_id for s in expired_snapshots}

        cleanup_targets: t.List[t.Tuple[SnapshotId, bool]] = []
        for snapshot in expired_snapshots:
            shared_version_snapshots = snapshots_by_version[(snapshot.name, snapshot.version)]
            shared_version_snapshots.discard(snapshot.snapshot_id)

            shared_dev_version_snapshots = snapshots_by_dev_version[
                (snapshot.name, snapshot.dev_version)
            ]
            shared_dev_version_snapshots.discard(snapshot.snapshot_id)

            if not shared_dev_version_snapshots:
                dev_table_only = bool(shared_version_snapshots)
                cleanup_targets.append((snapshot.snapshot_id, dev_table_only))

        snapshot_ids_to_cleanup = [snapshot_id for snapshot_id, _ in cleanup_targets]
        full_snapshots = self._get_snapshots(snapshot_ids_to_cleanup)
        for snapshot_id, dev_table_only in cleanup_targets:
            if snapshot_id in full_snapshots:
                cleanup_tasks.append(
                    SnapshotTableCleanupTask(
                        snapshot=full_snapshots[snapshot_id].table_info,
                        dev_table_only=dev_table_only,
                    )
                )
                expired_snapshot_ids.add(snapshot_id)
                all_expired_snapshot_ids.discard(snapshot_id)

        # Add any remaining expired snapshots that don't require cleanup
        if all_expired_snapshot_ids:
            expired_snapshot_ids.update(all_expired_snapshot_ids)

        if expired_snapshot_ids or cleanup_tasks:
            return ExpiredSnapshotBatch(
                expired_snapshot_ids=expired_snapshot_ids,
                cleanup_tasks=cleanup_tasks,
                batch_range=result_batch_range,
            )

        return None

    def delete_snapshots(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> None:
        """Deletes snapshots.

        Args:
            snapshot_ids: The snapshot IDs to delete.
        """
        if not snapshot_ids:
            return
        for where in snapshot_id_filter(
            self.engine_adapter, snapshot_ids, batch_size=self.SNAPSHOT_BATCH_SIZE
        ):
            self.engine_adapter.delete_from(self.snapshots_table, where=where)

    def touch_snapshots(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> None:
        """Touch snapshots to set their updated_ts to the current timestamp.

        Args:
            snapshot_ids: The snapshot IDs to touch.
        """
        self._update_snapshots(snapshot_ids)

    def get_snapshots(
        self,
        snapshot_ids: t.Iterable[SnapshotIdLike],
    ) -> t.Dict[SnapshotId, Snapshot]:
        """Fetches snapshots.

        Args:
            snapshot_ids: The snapshot IDs to fetch.

        Returns:
            A dictionary of snapshot IDs to snapshots.
        """
        return self._get_snapshots(snapshot_ids)

    def get_snapshots_by_names(
        self,
        snapshot_names: t.Iterable[str],
        current_ts: t.Optional[int] = None,
        exclude_expired: bool = True,
    ) -> t.Set[SnapshotIdAndVersion]:
        """Return the snapshot records for all versions of the specified snapshot names.

        Args:
            snapshot_names: Iterable of snapshot names to fetch all snapshot records for
            current_ts: Sets the current time for identifying which snapshots have expired so they can be excluded (only relevant if :exclude_expired=True)
            exclude_expired: Whether or not to return the snapshot id's of expired snapshots in the result

        Returns:
            A set containing all the matched snapshot records. To fetch full snapshots, pass it into StateSync.get_snapshots()
        """
        if not snapshot_names:
            return set()

        if exclude_expired:
            current_ts = current_ts or now_timestamp()
            unexpired_expr = (exp.column("updated_ts") + exp.column("ttl_ms")) > current_ts
        else:
            unexpired_expr = None

        return {
            SnapshotIdAndVersion(
                name=name,
                identifier=identifier,
                version=version,
                kind_name=kind_name or None,
                dev_version=dev_version,
                fingerprint=fingerprint,
            )
            for where in snapshot_name_filter(
                snapshot_names=snapshot_names,
                batch_size=self.SNAPSHOT_BATCH_SIZE,
            )
            for name, identifier, version, kind_name, dev_version, fingerprint in fetchall(
                self.engine_adapter,
                exp.select(
                    "name", "identifier", "version", "kind_name", "dev_version", "fingerprint"
                )
                .from_(self.snapshots_table)
                .where(where)
                .and_(unexpired_expr),
            )
        }

    def snapshots_exist(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> t.Set[SnapshotId]:
        """Checks if snapshots exist.

        Args:
            snapshot_ids: The snapshot IDs to check.

        Returns:
            A set of snapshot IDs to check for existence.
        """
        return {
            SnapshotId(name=name, identifier=identifier)
            for where in snapshot_id_filter(
                self.engine_adapter, snapshot_ids, batch_size=self.SNAPSHOT_BATCH_SIZE
            )
            for name, identifier in fetchall(
                self.engine_adapter,
                exp.select("name", "identifier").from_(self.snapshots_table).where(where),
            )
        }

    def nodes_exist(self, names: t.Iterable[str], exclude_external: bool = False) -> t.Set[str]:
        """Checks if nodes with given names exist.

        Args:
            names: The node names to check.
            exclude_external: Whether to exclude external nodes.

        Returns:
            A set of node names that exist.
        """
        names = set(names)

        if not names:
            return names

        query = (
            exp.select("name")
            .from_(self.snapshots_table)
            .where(exp.column("name").isin(*names))
            .distinct()
        )
        if exclude_external:
            query = query.where(exp.column("kind_name").neq(ModelKindName.EXTERNAL.value))
        return {name for (name,) in fetchall(self.engine_adapter, query)}

    def update_auto_restatements(
        self, next_auto_restatement_ts: t.Dict[SnapshotNameVersion, t.Optional[int]]
    ) -> None:
        """Updates the auto restatement timestamps.

        Args:
            next_auto_restatement_ts: A dictionary of snapshot name version to the next auto restatement timestamp.
        """
        next_auto_restatement_ts_deleted = []
        next_auto_restatement_ts_filtered = {}
        for k, v in next_auto_restatement_ts.items():
            if v is None:
                next_auto_restatement_ts_deleted.append(k)
            else:
                next_auto_restatement_ts_filtered[k] = v

        for where in snapshot_name_version_filter(
            self.engine_adapter,
            next_auto_restatement_ts_deleted,
            column_prefix="snapshot",
            alias=None,
            batch_size=self.SNAPSHOT_BATCH_SIZE,
        ):
            self.engine_adapter.delete_from(self.auto_restatements_table, where=where)

        if not next_auto_restatement_ts_filtered:
            return

        self.engine_adapter.merge(
            self.auto_restatements_table,
            _auto_restatements_to_df(next_auto_restatement_ts_filtered),
            target_columns_to_types=self._auto_restatement_columns_to_types,
            unique_key=(exp.column("snapshot_name"), exp.column("snapshot_version")),
        )

    def count(self) -> int:
        """Counts the number of snapshots in the state."""
        result = fetchone(self.engine_adapter, exp.select("COUNT(*)").from_(self.snapshots_table))
        return result[0] if result else 0

    def clear_cache(self) -> None:
        """Clears the snapshot cache."""
        self._snapshot_cache.clear()

    def _update_snapshots(
        self,
        snapshots: t.Iterable[SnapshotIdLike],
        **kwargs: t.Any,
    ) -> None:
        properties = kwargs
        if "updated_ts" not in properties:
            properties["updated_ts"] = now_timestamp()

        for where in snapshot_id_filter(
            self.engine_adapter, snapshots, batch_size=self.SNAPSHOT_BATCH_SIZE
        ):
            self.engine_adapter.update_table(
                self.snapshots_table,
                properties,
                where=where,
            )

    def _push_snapshots(self, snapshots: t.Iterable[Snapshot]) -> None:
        snapshots_to_store = []
        for snapshot in snapshots:
            if isinstance(snapshot.node, SeedModel):
                seed_model = t.cast(SeedModel, snapshot.node)
                snapshot = snapshot.copy(update={"node": seed_model.to_dehydrated()})
            snapshots_to_store.append(snapshot)

        self.engine_adapter.insert_append(
            self.snapshots_table,
            _snapshots_to_df(snapshots_to_store),
            target_columns_to_types=self._snapshot_columns_to_types,
            track_rows_processed=False,
        )

    def _get_snapshots(
        self,
        snapshot_ids: t.Iterable[SnapshotIdLike],
        lock_for_update: bool = False,
    ) -> t.Dict[SnapshotId, Snapshot]:
        """Fetches specified snapshots or all snapshots.

        Args:
            snapshot_ids: The collection of snapshot like objects to fetch.
            lock_for_update: Lock the snapshot rows for future update

        Returns:
            A dictionary of snapshot ids to snapshots for ones that could be found.
        """
        duplicates: t.Dict[SnapshotId, Snapshot] = {}

        def _loader(snapshot_ids_to_load: t.Set[SnapshotId]) -> t.Collection[Snapshot]:
            fetched_snapshots: t.Dict[SnapshotId, Snapshot] = {}
            for query in self._get_snapshots_expressions(snapshot_ids_to_load, lock_for_update):
                for (
                    serialized_snapshot,
                    _,
                    _,
                    _,
                    updated_ts,
                    unpaused_ts,
                    unrestorable,
                    forward_only,
                    next_auto_restatement_ts,
                ) in fetchall(self.engine_adapter, query):
                    snapshot = parse_snapshot(
                        serialized_snapshot=serialized_snapshot,
                        updated_ts=updated_ts,
                        unpaused_ts=unpaused_ts,
                        unrestorable=unrestorable,
                        forward_only=forward_only,
                        next_auto_restatement_ts=next_auto_restatement_ts,
                    )
                    snapshot_id = snapshot.snapshot_id
                    if snapshot_id in fetched_snapshots:
                        other = duplicates.get(snapshot_id, fetched_snapshots[snapshot_id])
                        duplicates[snapshot_id] = (
                            snapshot if snapshot.updated_ts > other.updated_ts else other
                        )
                        fetched_snapshots[snapshot_id] = duplicates[snapshot_id]
                    else:
                        fetched_snapshots[snapshot_id] = snapshot
            return fetched_snapshots.values()

        snapshots, cached_snapshots = self._snapshot_cache.get_or_load(
            {s.snapshot_id for s in snapshot_ids}, _loader
        )

        if cached_snapshots:
            cached_snapshots_in_state: t.Set[SnapshotId] = set()
            for where in snapshot_id_filter(
                self.engine_adapter, cached_snapshots, batch_size=self.SNAPSHOT_BATCH_SIZE
            ):
                query = (
                    exp.select(
                        "name",
                        "identifier",
                        "updated_ts",
                        "unpaused_ts",
                        "unrestorable",
                        "forward_only",
                        "next_auto_restatement_ts",
                    )
                    .from_(exp.to_table(self.snapshots_table).as_("snapshots"))
                    .join(
                        exp.to_table(self.auto_restatements_table).as_("auto_restatements"),
                        on=exp.and_(
                            exp.column("name", table="snapshots").eq(
                                exp.column("snapshot_name", table="auto_restatements")
                            ),
                            exp.column("version", table="snapshots").eq(
                                exp.column("snapshot_version", table="auto_restatements")
                            ),
                        ),
                        join_type="left",
                        copy=False,
                    )
                    .where(where)
                )
                if lock_for_update:
                    query = query.lock(copy=False)
                for (
                    name,
                    identifier,
                    updated_ts,
                    unpaused_ts,
                    unrestorable,
                    forward_only,
                    next_auto_restatement_ts,
                ) in fetchall(self.engine_adapter, query):
                    snapshot_id = SnapshotId(name=name, identifier=identifier)
                    snapshot = snapshots[snapshot_id]
                    snapshot.updated_ts = updated_ts
                    snapshot.unpaused_ts = unpaused_ts
                    snapshot.unrestorable = unrestorable
                    snapshot.forward_only = forward_only
                    snapshot.next_auto_restatement_ts = next_auto_restatement_ts
                    cached_snapshots_in_state.add(snapshot_id)

            missing_cached_snapshots = cached_snapshots - cached_snapshots_in_state
            for missing_cached_snapshot_id in missing_cached_snapshots:
                snapshots.pop(missing_cached_snapshot_id, None)

        if duplicates:
            self.push_snapshots(duplicates.values(), overwrite=True)
            logger.error("Found duplicate snapshots in the state store.")

        return snapshots

    def _get_snapshots_expressions(
        self,
        snapshot_ids: t.Iterable[SnapshotIdLike],
        lock_for_update: bool = False,
    ) -> t.Iterator[exp.Expr]:
        for where in snapshot_id_filter(
            self.engine_adapter,
            snapshot_ids,
            alias="snapshots",
            batch_size=self.SNAPSHOT_BATCH_SIZE,
        ):
            query = (
                exp.select(
                    "snapshots.snapshot",
                    "snapshots.name",
                    "snapshots.identifier",
                    "snapshots.version",
                    "snapshots.updated_ts",
                    "snapshots.unpaused_ts",
                    "snapshots.unrestorable",
                    "snapshots.forward_only",
                    "auto_restatements.next_auto_restatement_ts",
                )
                .from_(exp.to_table(self.snapshots_table).as_("snapshots"))
                .join(
                    exp.to_table(self.auto_restatements_table).as_("auto_restatements"),
                    on=exp.and_(
                        exp.column("name", table="snapshots").eq(
                            exp.column("snapshot_name", table="auto_restatements")
                        ),
                        exp.column("version", table="snapshots").eq(
                            exp.column("snapshot_version", table="auto_restatements")
                        ),
                    ),
                    join_type="left",
                    copy=False,
                )
                .where(where)
            )
            if lock_for_update:
                query = query.lock(copy=False)
            yield query

    def _get_snapshots_with_same_version(
        self,
        snapshots: t.Collection[SnapshotNameVersionLike],
        lock_for_update: bool = False,
    ) -> t.List[SnapshotIdAndVersion]:
        """Fetches all snapshots that share the same version as the snapshots.

        The output includes the snapshots with the specified identifiers.

        Args:
            snapshots: The collection of target name / version pairs.
            lock_for_update: Lock the snapshot rows for future update

        Returns:
            The list of Snapshot objects.
        """
        if not snapshots:
            return []

        snapshot_rows = []

        for where in snapshot_name_version_filter(
            self.engine_adapter, snapshots, batch_size=self.SNAPSHOT_BATCH_SIZE
        ):
            query = (
                exp.select(
                    "name",
                    "identifier",
                    "version",
                    "kind_name",
                    "dev_version",
                    "fingerprint",
                )
                .from_(exp.to_table(self.snapshots_table).as_("snapshots"))
                .where(where)
            )
            if lock_for_update:
                query = query.lock(copy=False)

            snapshot_rows.extend(fetchall(self.engine_adapter, query))

        return [
            SnapshotIdAndVersion(
                name=name,
                identifier=identifier,
                version=version,
                kind_name=kind_name or None,
                dev_version=dev_version,
                fingerprint=SnapshotFingerprint.parse_raw(fingerprint),
            )
            for name, identifier, version, kind_name, dev_version, fingerprint in snapshot_rows
        ]


def parse_snapshot(
    serialized_snapshot: str,
    updated_ts: int,
    unpaused_ts: t.Optional[int],
    unrestorable: bool,
    forward_only: bool,
    next_auto_restatement_ts: t.Optional[int],
) -> Snapshot:
    return Snapshot(
        **{
            **json.loads(serialized_snapshot),
            "updated_ts": updated_ts,
            "unpaused_ts": unpaused_ts,
            "unrestorable": unrestorable,
            "forward_only": forward_only,
            "next_auto_restatement_ts": next_auto_restatement_ts,
        }
    )


def _snapshot_to_json(snapshot: Snapshot) -> str:
    return snapshot.json(
        exclude={
            "intervals",
            "dev_intervals",
            "pending_restatement_intervals",
            "updated_ts",
            "unpaused_ts",
            "unrestorable",
            "forward_only",
            "next_auto_restatement_ts",
        }
    )


def _snapshots_to_df(snapshots: t.Iterable[Snapshot]) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "name": snapshot.name,
                "identifier": snapshot.identifier,
                "version": snapshot.version,
                "snapshot": _snapshot_to_json(snapshot),
                "kind_name": snapshot.model_kind_name.value if snapshot.model_kind_name else None,
                "updated_ts": snapshot.updated_ts,
                "unpaused_ts": snapshot.unpaused_ts,
                "ttl_ms": snapshot.ttl_ms,
                "unrestorable": snapshot.unrestorable,
                "forward_only": snapshot.forward_only,
                "dev_version": snapshot.dev_version,
                "fingerprint": snapshot.fingerprint.json(),
            }
            for snapshot in snapshots
        ]
    )


def _auto_restatements_to_df(auto_restatements: t.Dict[SnapshotNameVersion, int]) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "snapshot_name": name_version.name,
                "snapshot_version": name_version.version,
                "next_auto_restatement_ts": ts,
            }
            for name_version, ts in auto_restatements.items()
        ]
    )
