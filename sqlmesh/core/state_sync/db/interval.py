from __future__ import annotations

import typing as t
import logging

from sqlglot import exp

from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.state_sync.db.utils import (
    snapshot_name_version_filter,
    snapshot_id_filter,
    create_batches,
    fetchall,
)
from sqlmesh.core.snapshot import (
    SnapshotIntervals,
    SnapshotIdLike,
    SnapshotIdAndVersionLike,
    SnapshotNameVersionLike,
    SnapshotTableCleanupTask,
    SnapshotNameVersion,
    Snapshot,
)
from sqlmesh.core.snapshot.definition import Interval
from sqlmesh.utils.migration import index_text_type
from sqlmesh.utils import random_id
from sqlmesh.utils.date import now_timestamp

if t.TYPE_CHECKING:
    import pandas as pd


logger = logging.getLogger(__name__)


class IntervalState:
    INTERVAL_BATCH_SIZE = 1000
    SNAPSHOT_BATCH_SIZE = 1000

    def __init__(
        self,
        engine_adapter: EngineAdapter,
        schema: t.Optional[str] = None,
        table_name: t.Optional[str] = None,
    ):
        self.engine_adapter = engine_adapter
        self.intervals_table = exp.table_(table_name or "_intervals", db=schema)

        index_type = index_text_type(engine_adapter.dialect)
        self._interval_columns_to_types = {
            "id": exp.DataType.build(index_type),
            "created_ts": exp.DataType.build("bigint"),
            "name": exp.DataType.build(index_type),
            "identifier": exp.DataType.build("text"),
            "version": exp.DataType.build(index_type),
            "dev_version": exp.DataType.build(index_type),
            "start_ts": exp.DataType.build("bigint"),
            "end_ts": exp.DataType.build("bigint"),
            "is_dev": exp.DataType.build("boolean"),
            "is_removed": exp.DataType.build("boolean"),
            "is_compacted": exp.DataType.build("boolean"),
            "is_pending_restatement": exp.DataType.build("boolean"),
            "last_altered_ts": exp.DataType.build("bigint"),
        }

    def add_snapshots_intervals(self, snapshots_intervals: t.Sequence[SnapshotIntervals]) -> None:
        if snapshots_intervals:
            self._push_snapshot_intervals(snapshots_intervals)

    def remove_intervals(
        self,
        snapshot_intervals: t.Sequence[t.Tuple[SnapshotIdAndVersionLike, Interval]],
        remove_shared_versions: bool = False,
    ) -> None:
        intervals_to_remove: t.Sequence[
            t.Tuple[t.Union[SnapshotIdAndVersionLike, SnapshotIntervals], Interval]
        ] = snapshot_intervals
        if remove_shared_versions:
            name_version_mapping = {s.name_version: interval for s, interval in snapshot_intervals}
            all_snapshots = []
            for where in snapshot_name_version_filter(
                self.engine_adapter,
                name_version_mapping,
                alias=None,
                batch_size=self.SNAPSHOT_BATCH_SIZE,
            ):
                all_snapshots.extend(
                    [
                        SnapshotIntervals(
                            name=r[0],
                            identifier=r[1],
                            version=r[2],
                            dev_version=r[3],
                            intervals=[],
                            dev_intervals=[],
                        )
                        for r in fetchall(
                            self.engine_adapter,
                            exp.select("name", "identifier", "version", "dev_version")
                            .from_(self.intervals_table)
                            .where(where)
                            .distinct(),
                        )
                    ]
                )
            intervals_to_remove = [
                (snapshot, name_version_mapping[snapshot.name_version])
                for snapshot in all_snapshots
            ]

        if not intervals_to_remove:
            return

        if logger.isEnabledFor(logging.INFO):
            snapshot_ids = ", ".join(str(s.snapshot_id) for s, _ in intervals_to_remove)
            logger.info("Removing interval for snapshots: %s", snapshot_ids)

        self.engine_adapter.insert_append(
            self.intervals_table,
            _intervals_to_df(intervals_to_remove, is_dev=False, is_removed=True),
            target_columns_to_types=self._interval_columns_to_types,
            track_rows_processed=False,
        )

    def get_snapshot_intervals(
        self, snapshots: t.Collection[SnapshotNameVersionLike]
    ) -> t.List[SnapshotIntervals]:
        return self._get_snapshot_intervals(snapshots)[1]

    def compact_intervals(self) -> None:
        interval_ids, snapshot_intervals = self._get_snapshot_intervals(uncompacted_only=True)

        logger.info(
            "Compacting %s intervals for %s snapshots", len(interval_ids), len(snapshot_intervals)
        )

        self._push_snapshot_intervals(snapshot_intervals, is_compacted=True)

        if interval_ids:
            for interval_id_batch in create_batches(
                list(interval_ids), batch_size=self.INTERVAL_BATCH_SIZE
            ):
                self.engine_adapter.delete_from(
                    self.intervals_table, exp.column("id").isin(*interval_id_batch)
                )

    def refresh_snapshot_intervals(self, snapshots: t.Collection[Snapshot]) -> t.List[Snapshot]:
        if not snapshots:
            return []

        _, intervals = self._get_snapshot_intervals([s for s in snapshots if s.version])
        for s in snapshots:
            s.intervals = []
            s.dev_intervals = []
        return Snapshot.hydrate_with_intervals_by_version(snapshots, intervals)

    def max_interval_end_per_model(
        self, snapshots: t.Collection[SnapshotNameVersionLike]
    ) -> t.Dict[str, int]:
        if not snapshots:
            return {}

        table_alias = "intervals"
        name_col = exp.column("name", table=table_alias)
        version_col = exp.column("version", table=table_alias)

        result: t.Dict[str, int] = {}

        for where in snapshot_name_version_filter(
            self.engine_adapter, snapshots, alias=table_alias, batch_size=self.SNAPSHOT_BATCH_SIZE
        ):
            query = (
                exp.select(
                    name_col,
                    exp.func("MAX", exp.column("end_ts", table=table_alias)).as_("max_end_ts"),
                )
                .from_(exp.to_table(self.intervals_table).as_(table_alias))
                .where(where, copy=False)
                .where(
                    exp.and_(
                        exp.to_column("is_dev").not_(),
                        exp.to_column("is_removed").not_(),
                        exp.to_column("is_pending_restatement").not_(),
                    ),
                    copy=False,
                )
                .group_by(name_col, version_col, copy=False)
            )

            for name, max_end in fetchall(self.engine_adapter, query):
                result[name] = max_end

        return result

    def cleanup_intervals(
        self,
        cleanup_targets: t.List[SnapshotTableCleanupTask],
        expired_snapshot_ids: t.Collection[SnapshotIdLike],
    ) -> None:
        # Cleanup can only happen for compacted intervals
        self.compact_intervals()
        # Delete intervals for non-dev tables that are no longer used
        self._delete_intervals_by_version(cleanup_targets)
        # Delete dev intervals for dev tables that are no longer used
        self._delete_intervals_by_dev_version(cleanup_targets)
        # Nullify the snapshot identifiers of interval records for snapshots that have been deleted
        self._update_intervals_for_deleted_snapshots(expired_snapshot_ids)

    def _push_snapshot_intervals(
        self,
        snapshots: t.Iterable[t.Union[Snapshot, SnapshotIntervals]],
        is_compacted: bool = False,
    ) -> None:
        import pandas as pd

        new_intervals = []
        for snapshot in snapshots:
            logger.info("Pushing intervals for snapshot %s", snapshot.snapshot_id)
            for start_ts, end_ts in snapshot.intervals:
                new_intervals.append(
                    _interval_to_df(
                        snapshot,
                        start_ts,
                        end_ts,
                        is_dev=False,
                        is_compacted=is_compacted,
                        last_altered_ts=snapshot.last_altered_ts,
                    )
                )
            for start_ts, end_ts in snapshot.dev_intervals:
                new_intervals.append(
                    _interval_to_df(
                        snapshot,
                        start_ts,
                        end_ts,
                        is_dev=True,
                        is_compacted=is_compacted,
                        last_altered_ts=snapshot.dev_last_altered_ts,
                    )
                )

        # Make sure that all pending restatement intervals are recorded last
        for snapshot in snapshots:
            for start_ts, end_ts in snapshot.pending_restatement_intervals:
                new_intervals.append(
                    _interval_to_df(
                        snapshot,
                        start_ts,
                        end_ts,
                        is_dev=False,
                        is_compacted=is_compacted,
                        is_pending_restatement=True,
                        last_altered_ts=snapshot.last_altered_ts,
                    )
                )

        if new_intervals:
            self.engine_adapter.insert_append(
                self.intervals_table,
                pd.DataFrame(new_intervals),
                target_columns_to_types=self._interval_columns_to_types,
                track_rows_processed=False,
            )

    def _get_snapshot_intervals(
        self,
        snapshots: t.Optional[t.Collection[SnapshotNameVersionLike]] = None,
        uncompacted_only: bool = False,
    ) -> t.Tuple[t.Set[str], t.List[SnapshotIntervals]]:
        if not snapshots and snapshots is not None:
            return (set(), [])

        query = self._get_snapshot_intervals_query(uncompacted_only)

        interval_ids: t.Set[str] = set()
        intervals: t.Dict[
            t.Tuple[str, str, t.Optional[str], t.Optional[str]], SnapshotIntervals
        ] = {}

        for where in (
            snapshot_name_version_filter(
                self.engine_adapter,
                snapshots,
                alias="intervals",
                batch_size=self.SNAPSHOT_BATCH_SIZE,
            )
            if snapshots
            else [None]
        ):
            rows = fetchall(self.engine_adapter, query.where(where))
            for (
                interval_id,
                name,
                identifier,
                version,
                dev_version,
                start,
                end,
                is_dev,
                is_removed,
                is_pending_restatement,
                last_altered_ts,
            ) in rows:
                interval_ids.add(interval_id)
                merge_key = (name, version, dev_version, identifier)
                # Pending restatement intervals are merged by name and version
                pending_restatement_interval_merge_key = (name, version, None, None)

                if merge_key not in intervals:
                    intervals[merge_key] = SnapshotIntervals(
                        name=name,
                        identifier=identifier,
                        version=version,
                        dev_version=dev_version,
                    )

                if pending_restatement_interval_merge_key not in intervals:
                    intervals[pending_restatement_interval_merge_key] = SnapshotIntervals(
                        name=name,
                        identifier=None,
                        version=version,
                        dev_version=None,
                    )

                if is_removed:
                    if is_dev:
                        intervals[merge_key].remove_dev_interval(start, end)
                    else:
                        intervals[merge_key].remove_interval(start, end)
                elif is_pending_restatement:
                    intervals[
                        pending_restatement_interval_merge_key
                    ].add_pending_restatement_interval(start, end)
                else:
                    if is_dev:
                        intervals[merge_key].add_dev_interval(start, end)
                        intervals[merge_key].update_dev_last_altered_ts(last_altered_ts)
                    else:
                        intervals[merge_key].add_interval(start, end)
                        intervals[merge_key].update_last_altered_ts(last_altered_ts)
                        # Remove all pending restatement intervals recorded before the current interval has been added
                        intervals[
                            pending_restatement_interval_merge_key
                        ].remove_pending_restatement_interval(start, end)

        return interval_ids, [i for i in intervals.values() if not i.is_empty()]

    def _get_snapshot_intervals_query(self, uncompacted_only: bool) -> exp.Select:
        query = (
            exp.select(
                "id",
                exp.column("name", table="intervals"),
                exp.column("identifier", table="intervals"),
                exp.column("version", table="intervals"),
                exp.column("dev_version", table="intervals"),
                "start_ts",
                "end_ts",
                "is_dev",
                "is_removed",
                "is_pending_restatement",
                "last_altered_ts",
            )
            .from_(exp.to_table(self.intervals_table).as_("intervals"))
            .order_by(
                exp.column("name", table="intervals"),
                exp.column("version", table="intervals"),
                "created_ts",
                "is_removed",
                "is_pending_restatement",
            )
        )
        if uncompacted_only:
            query.join(
                exp.select("name", "version")
                .from_(exp.to_table(self.intervals_table).as_("intervals"))
                .where(exp.column("is_compacted").not_())
                .distinct()
                .subquery(alias="uncompacted"),
                on=exp.and_(
                    exp.column("name", table="intervals").eq(
                        exp.column("name", table="uncompacted")
                    ),
                    exp.column("version", table="intervals").eq(
                        exp.column("version", table="uncompacted")
                    ),
                ),
                copy=False,
            )
        return query

    def _update_intervals_for_deleted_snapshots(
        self, snapshot_ids: t.Collection[SnapshotIdLike]
    ) -> None:
        """Nullifies the snapshot identifiers of dev interval records and snapshot identifiers and dev versions of
        non-dev interval records for snapshots that have been deleted so that they can be compacted efficiently.
        """
        if not snapshot_ids:
            return

        for where in snapshot_id_filter(
            self.engine_adapter, snapshot_ids, alias=None, batch_size=self.SNAPSHOT_BATCH_SIZE
        ):
            # Nullify the identifier for dev intervals
            # Set is_compacted to False so that it's compacted during the next compaction
            self.engine_adapter.update_table(
                self.intervals_table,
                {"identifier": None, "is_compacted": False},
                where=where.and_(exp.column("is_dev")),
            )
            # Nullify both identifier and dev version for non-dev intervals
            # Set is_compacted to False so that it's compacted during the next compaction
            self.engine_adapter.update_table(
                self.intervals_table,
                {"identifier": None, "dev_version": None, "is_compacted": False},
                where=where.and_(exp.column("is_dev").not_()),
            )

    def _delete_intervals_by_dev_version(self, targets: t.List[SnapshotTableCleanupTask]) -> None:
        """Deletes dev intervals for snapshot dev versions that are no longer used."""
        dev_keys_to_delete = [
            SnapshotNameVersion(name=t.snapshot.name, version=t.snapshot.dev_version)
            for t in targets
            if t.dev_table_only
        ]
        if not dev_keys_to_delete:
            return

        for where in snapshot_name_version_filter(
            self.engine_adapter,
            dev_keys_to_delete,
            version_column_name="dev_version",
            alias=None,
            batch_size=self.SNAPSHOT_BATCH_SIZE,
        ):
            self.engine_adapter.delete_from(self.intervals_table, where.and_(exp.column("is_dev")))

    def _delete_intervals_by_version(self, targets: t.List[SnapshotTableCleanupTask]) -> None:
        """Deletes intervals for snapshot versions that are no longer used."""
        non_dev_keys_to_delete = [t.snapshot for t in targets if not t.dev_table_only]
        if not non_dev_keys_to_delete:
            return

        for where in snapshot_name_version_filter(
            self.engine_adapter,
            non_dev_keys_to_delete,
            alias=None,
            batch_size=self.SNAPSHOT_BATCH_SIZE,
        ):
            self.engine_adapter.delete_from(self.intervals_table, where)


def _intervals_to_df(
    snapshot_intervals: t.Sequence[
        t.Tuple[t.Union[SnapshotIdAndVersionLike, SnapshotIntervals], Interval]
    ],
    is_dev: bool,
    is_removed: bool,
) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame(
        [
            _interval_to_df(
                s,
                *interval,
                is_dev=is_dev,
                is_removed=is_removed,
            )
            for s, interval in snapshot_intervals
        ]
    )


def _interval_to_df(
    snapshot: t.Union[SnapshotIdAndVersionLike, SnapshotIntervals],
    start_ts: int,
    end_ts: int,
    is_dev: bool = False,
    is_removed: bool = False,
    is_compacted: bool = False,
    is_pending_restatement: bool = False,
    last_altered_ts: t.Optional[int] = None,
) -> t.Dict[str, t.Any]:
    return {
        "id": random_id(),
        "created_ts": now_timestamp(),
        "name": snapshot.name,
        "identifier": snapshot.identifier if not is_pending_restatement else None,
        "version": snapshot.version,
        "dev_version": snapshot.dev_version if not is_pending_restatement else None,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "is_dev": is_dev,
        "is_removed": is_removed,
        "is_compacted": is_compacted,
        "is_pending_restatement": is_pending_restatement,
        "last_altered_ts": last_altered_ts,
    }
