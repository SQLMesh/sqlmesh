from __future__ import annotations

import typing as t

from sqlmesh.core.model import SeedModel
from sqlmesh.core.snapshot import (
    Snapshot,
    SnapshotId,
    SnapshotIdLike,
    SnapshotIdAndVersionLike,
    SnapshotInfoLike,
)
from sqlmesh.core.snapshot.definition import Interval, SnapshotIntervals
from sqlmesh.core.state_sync.base import DelegatingStateSync, StateSync
from sqlmesh.core.state_sync.common import ExpiredBatchRange
from sqlmesh.utils.date import TimeLike, now_timestamp


class CachingStateSync(DelegatingStateSync):
    """In memory cache for snapshots that implements the state sync api.

    Args:
        state_sync: The base state sync.
        ttl: The number of seconds a snapshot should be cached.
    """

    def __init__(self, state_sync: StateSync, ttl: int = 120):
        super().__init__(state_sync)
        # The cache can contain a snapshot or False or None.
        # False means that the snapshot does not exist in the state sync but has been requested before
        # None means that the snapshot has not been requested.
        self.snapshot_cache: t.Dict[
            SnapshotId, t.Tuple[t.Optional[Snapshot | t.Literal[False]], int]
        ] = {}

        self.ttl = ttl

    def _from_cache(
        self, snapshot_id: SnapshotId, now: int
    ) -> t.Optional[Snapshot | t.Literal[False]]:
        snapshot: t.Optional[Snapshot | t.Literal[False]] = None
        snapshot_expiration = self.snapshot_cache.get(snapshot_id)

        if snapshot_expiration and snapshot_expiration[1] >= now:
            snapshot = snapshot_expiration[0]

        return snapshot

    def get_snapshots(
        self, snapshot_ids: t.Iterable[SnapshotIdLike]
    ) -> t.Dict[SnapshotId, Snapshot]:
        existing = {}
        missing = set()
        now = now_timestamp()
        expire_at = now + self.ttl * 1000

        for s in snapshot_ids:
            snapshot_id = s.snapshot_id
            snapshot = self._from_cache(snapshot_id, now)

            if snapshot is None:
                self.snapshot_cache[snapshot_id] = (False, expire_at)
                missing.add(snapshot_id)
            elif snapshot:
                existing[snapshot_id] = snapshot

        if missing:
            existing.update(self.state_sync.get_snapshots(missing))

        for snapshot_id, snapshot in existing.items():
            cached = self._from_cache(snapshot_id, now)
            if cached and (not isinstance(cached.node, SeedModel) or cached.node.is_hydrated):
                continue
            self.snapshot_cache[snapshot_id] = (snapshot, expire_at)

        return existing

    def snapshots_exist(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> t.Set[SnapshotId]:
        existing = set()
        missing = set()
        now = now_timestamp()

        for s in snapshot_ids:
            snapshot_id = s.snapshot_id
            snapshot = self._from_cache(snapshot_id, now)
            if snapshot:
                existing.add(snapshot_id)
            elif snapshot is None:
                missing.add(snapshot_id)

        if missing:
            existing.update(self.state_sync.snapshots_exist(missing))

        return existing

    def push_snapshots(self, snapshots: t.Iterable[Snapshot]) -> None:
        snapshots = tuple(snapshots)

        for snapshot in snapshots:
            self.snapshot_cache.pop(snapshot.snapshot_id, None)

        self.state_sync.push_snapshots(snapshots)

    def delete_snapshots(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> None:
        snapshot_ids = tuple(snapshot_ids)

        for s in snapshot_ids:
            self.snapshot_cache.pop(s.snapshot_id, None)
        self.state_sync.delete_snapshots(snapshot_ids)

    def delete_expired_snapshots(
        self,
        batch_range: ExpiredBatchRange,
        ignore_ttl: bool = False,
        current_ts: t.Optional[int] = None,
        target_snapshot_ids: t.Optional[t.Collection[SnapshotIdLike]] = None,
    ) -> None:
        self.snapshot_cache.clear()
        self.state_sync.delete_expired_snapshots(
            batch_range=batch_range,
            ignore_ttl=ignore_ttl,
            current_ts=current_ts,
            target_snapshot_ids=target_snapshot_ids,
        )

    def add_snapshots_intervals(self, snapshots_intervals: t.Sequence[SnapshotIntervals]) -> None:
        for snapshot_intervals in snapshots_intervals:
            if snapshot_intervals.snapshot_id:
                self.snapshot_cache.pop(snapshot_intervals.snapshot_id, None)
            else:
                # Evict all snapshots that share the same name
                self.snapshot_cache = {
                    snapshot_id: value
                    for snapshot_id, value in self.snapshot_cache.items()
                    if snapshot_id.name != snapshot_intervals.name
                }
        self.state_sync.add_snapshots_intervals(snapshots_intervals)

    def remove_intervals(
        self,
        snapshot_intervals: t.Sequence[t.Tuple[SnapshotIdAndVersionLike, Interval]],
        remove_shared_versions: bool = False,
    ) -> None:
        for s, _ in snapshot_intervals:
            self.snapshot_cache.pop(s.snapshot_id, None)
        self.state_sync.remove_intervals(snapshot_intervals, remove_shared_versions)

    def unpause_snapshots(
        self, snapshots: t.Collection[SnapshotInfoLike], unpaused_dt: TimeLike
    ) -> None:
        self.snapshot_cache.clear()
        self.state_sync.unpause_snapshots(snapshots, unpaused_dt)

    def clear_cache(self) -> None:
        self.snapshot_cache.clear()
