from __future__ import annotations

import logging
import typing as t

from pathlib import Path
from sqlmesh.core.model.cache import (
    OptimizedQueryCache,
    optimized_query_cache_pool,
    load_optimized_query,
)
from sqlmesh.core import constants as c
from sqlmesh.core.snapshot.definition import Snapshot, SnapshotId
from sqlmesh.utils.cache import FileCache


logger = logging.getLogger(__name__)


class SnapshotCache:
    def __init__(self, path: Path):
        self._snapshot_cache: FileCache[Snapshot] = FileCache(path, prefix="snapshot")
        self._optimized_query_cache = OptimizedQueryCache(path)

    def get_or_load(
        self,
        snapshot_ids: t.Set[SnapshotId],
        loader: t.Callable[[t.Set[SnapshotId]], t.Collection[Snapshot]],
        *,
        load_optimized_queries: bool = True,
    ) -> t.Tuple[t.Dict[SnapshotId, Snapshot], t.Set[SnapshotId]]:
        """Fetches the target snapshots from cache or loads them using the provided loader on cache miss.

        Args:
            snapshot_ids: Target snapshot IDs to fetch.
            loader: The loader to load snapshot records that are missing in the cache.
            load_optimized_queries: Whether to prepare optimized queries for model snapshots.

        Returns:
            A tuple where the first value represents the fetched snapshots, and the second value is a set of
            snapshot IDs for which records were retrieved from the cache.

        """
        snapshots = {}
        cache_hits: t.Set[SnapshotId] = set()

        for s_id in snapshot_ids:
            snapshot = self._snapshot_cache.get(self._entry_name(s_id))
            if snapshot:
                snapshot.intervals = []
                snapshot.dev_intervals = []
                snapshots[s_id] = snapshot
                cache_hits.add(s_id)

        snapshot_ids_to_load = snapshot_ids - snapshots.keys()
        if snapshot_ids_to_load:
            loaded_snapshots = loader(snapshot_ids_to_load)
            for snapshot in loaded_snapshots:
                snapshots[snapshot.snapshot_id] = snapshot

        if load_optimized_queries:
            with optimized_query_cache_pool(self._optimized_query_cache) as executor:
                for key, entry_name in executor.map(
                    load_optimized_query,
                    (
                        (snapshot.model, s_id)
                        for s_id, snapshot in snapshots.items()
                        if snapshot.is_model
                    ),
                ):
                    if entry_name:
                        self._optimized_query_cache.with_optimized_query(
                            snapshots[key].model, entry_name
                        )

        for snapshot in snapshots.values():
            self._update_node_hash_cache(snapshot)

            if load_optimized_queries and snapshot.is_model and c.MAX_FORK_WORKERS == 1:
                try:
                    self._optimized_query_cache.with_optimized_query(snapshot.model)
                except Exception:
                    logger.exception(
                        "Failed to cache optimized query for snapshot %s", snapshot.snapshot_id
                    )

            self.put(snapshot)

        return snapshots, cache_hits

    def put(self, snapshot: Snapshot) -> None:
        entry_name = self._entry_name(snapshot.snapshot_id)

        if self._snapshot_cache.exists(entry_name):
            return

        try:
            if snapshot.is_model:
                # make sure we preload full_depends_on
                snapshot.model.full_depends_on
            self._snapshot_cache.put(entry_name, value=snapshot)
        except Exception:
            logger.exception("Failed to cache snapshot %s", snapshot.snapshot_id)

    def clear(self) -> None:
        self._snapshot_cache.clear()

    @staticmethod
    def _entry_name(snapshot_id: SnapshotId) -> str:
        return f"{snapshot_id.name}_{snapshot_id.identifier}"

    @staticmethod
    def _update_node_hash_cache(snapshot: Snapshot) -> None:
        snapshot.node._data_hash = snapshot.fingerprint.data_hash
        snapshot.node._metadata_hash = snapshot.fingerprint.metadata_hash
