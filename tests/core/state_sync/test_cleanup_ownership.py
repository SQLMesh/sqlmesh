# SPDX-License-Identifier: Apache-2.0
"""Independent cleanup ownership and persisted interval state."""

import json
from contextlib import contextmanager

import pytest
from sqlglot import exp, parse_one

from sqlmesh.core.janitor import delete_expired_snapshots
from sqlmesh.core.engine_adapter import create_engine_adapter
from sqlmesh.core.model import SqlModel
from sqlmesh.core.model.kind import FullKind
from sqlmesh.core.snapshot import Snapshot, SnapshotChangeCategory, SnapshotEvaluator
from sqlmesh.core.snapshot.definition import SnapshotIntervals
from sqlmesh.core.state_sync.common import ExpiredBatchRange
from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync
from sqlmesh.utils.date import to_timestamp


NOW = to_timestamp("2026-09-20")
WINDOW = [(to_timestamp("2026-09-01"), to_timestamp("2026-09-03"))]


def snapshot(label, prod, dev, expired=False, query="select 2 as a"):
    result = Snapshot.from_node(
        SqlModel(name="s.a", kind=FullKind(), query=parse_one(query), description=label),
        nodes={},
        ttl="in 10 seconds",
    )
    result.categorize_as(SnapshotChangeCategory.BREAKING)
    result.version = prod
    result.dev_version_ = dev
    result.updated_ts = NOW - 20000 if expired else NOW
    return result


@contextmanager
def store(tmp_path):
    import duckdb

    adapter = create_engine_adapter(lambda: duckdb.connect(str(tmp_path / "state.db")), "duckdb")
    state = EngineAdapterStateSync(adapter, None, cache_dir=tmp_path / "cache")
    state.migrate(skip_backup=True)
    adapter.create_schema("sqlmesh__s")
    try:
        yield adapter, state
    finally:
        adapter.close()


def materialize(adapter, snapshots):
    for table in {s.table_name(deployable) for s in snapshots for deployable in (False, True)}:
        adapter.execute(f"CREATE TABLE {table} AS SELECT 42 AS marker")


def expire(adapter, state, batch_size=100):
    batch = state.get_expired_snapshots(
        batch_range=ExpiredBatchRange.init_batch_range(batch_size), current_ts=NOW
    )
    assert batch is not None
    SnapshotEvaluator(adapter, ddl_concurrent_tasks=1).cleanup(batch.cleanup_tasks)
    state.delete_expired_snapshots(batch_range=batch.batch_range, current_ts=NOW)
    return batch


def reload(adapter, tmp_path, snapshots):
    fresh = EngineAdapterStateSync(adapter, None, cache_dir=tmp_path / "fresh")
    fresh.compact_intervals()
    return fresh.get_snapshots(snapshots)


@pytest.mark.parametrize("batch_size", [1, 2])
def test_retired_production_group_keeps_development_coverage(tmp_path, batch_size):
    # Two retired owners share production, but only one's dev table is still live.
    first = snapshot("first", "prod_old", "dev_shared", True)
    second = snapshot("second", "prod_old", "dev_other", True)
    second.updated_ts += 1
    live = snapshot("live", "prod_live", "dev_shared")
    with store(tmp_path) as (adapter, state):
        state.push_snapshots([first, second, live])
        materialize(adapter, [first, second, live])
        state.add_interval(first, "2026-09-01", "2026-09-02", is_dev=True)
        state.add_interval(first, "2026-09-01", "2026-09-02")
        for _ in range((2 + batch_size - 1) // batch_size):
            expire(adapter, state, batch_size)
        assert not state.snapshots_exist([first, second])
        assert adapter.table_exists(live.table_name(False))
        restored = reload(adapter, tmp_path, [live])[live.snapshot_id]
        assert restored.dev_intervals == WINDOW, (
            "last production-owner cleanup lost shared dev coverage"
        )
        assert restored.intervals == [], "unrelated production coverage transferred to survivor"
        assert not adapter.table_exists(first.table_name(True))
        assert not adapter.table_exists(second.table_name(False))


@pytest.mark.parametrize("compact", [False, True])
def test_legacy_derived_dev_version_still_owns_table(tmp_path, compact):
    # SnapshotIdAndVersion.dev_version supports absent stored values by deriving
    # from the fingerprint; migration v0094 preserves such null column values.
    expired = snapshot("expired", "prod_expired", None, True)
    live = snapshot("live", "prod_live", None)
    assert expired.dev_version == live.dev_version
    with store(tmp_path) as (adapter, state):
        state.push_snapshots([expired, live])
        materialize(adapter, [expired, live])
        state.add_interval(expired, "2026-09-01", "2026-09-02", is_dev=True)
        for s in (expired, live):
            where = exp.column("name").eq(s.name).and_(exp.column("identifier").eq(s.identifier))
            record = json.loads(
                adapter.fetchone(exp.select("snapshot").from_("_snapshots").where(where))[0]
            )
            record.pop("dev_version", None)
            adapter.update_table(
                "_snapshots", {"dev_version": None, "snapshot": json.dumps(record)}, where=where
            )
        if compact:
            state.compact_intervals()
        expire(adapter, state)
        assert adapter.table_exists(live.table_name(False)), (
            "legacy owner was omitted from ownership lookup"
        )
        restored = reload(adapter, tmp_path, [live])[live.snapshot_id]
        assert restored.dev_intervals == WINDOW
        assert not restored.intervals
        assert not adapter.table_exists(expired.table_name(True))


@pytest.mark.parametrize("batch_size", [1, 2, 10])
def test_crossed_ownership_batches_keep_only_live_resources(tmp_path, batch_size):
    a = snapshot("a", "p1", "d1", True)
    b = snapshot("b", "p1", "d2", True)
    c = snapshot("c", "p2", "d2", True)
    live_prod = snapshot("prod", "p2", "d3")
    live_dev = snapshot("dev", "p3", "d1")
    expired = [a, b, c]
    retained = [live_prod, live_dev]
    for n, s in enumerate(expired):
        s.updated_ts += n
    with store(tmp_path) as (adapter, state):
        state.push_snapshots([*expired, *retained])
        materialize(adapter, [*expired, *retained])
        for s in expired:
            state.add_interval(s, "2026-09-01", "2026-09-02", is_dev=True)
            state.add_interval(s, "2026-09-01", "2026-09-02")
        for _ in range((3 + batch_size - 1) // batch_size):
            expire(adapter, state, batch_size)
        assert not state.snapshots_exist(expired)
        restored = reload(adapter, tmp_path, retained)
        assert restored[live_dev.snapshot_id].dev_intervals == WINDOW
        assert restored[live_dev.snapshot_id].intervals == []
        assert restored[live_prod.snapshot_id].intervals == WINDOW
        assert restored[live_prod.snapshot_id].dev_intervals == []
        live_tables = {s.table_name(d) for s in retained for d in (False, True)}
        all_tables = {s.table_name(d) for s in [*expired, *retained] for d in (False, True)}
        for table in all_tables:
            assert adapter.table_exists(table) == (table in live_tables), table


def test_shared_dev_hydration_excludes_unrelated_pending_restatements():
    old = snapshot("old", "prod_old", "shared")
    live = snapshot("live", "prod_live", "shared")
    pending = (to_timestamp("2026-09-10"), to_timestamp("2026-09-11"))
    source = SnapshotIntervals(
        name=old.name,
        identifier=old.identifier,
        version=old.version,
        dev_version=old.dev_version,
        intervals=[pending],
        dev_intervals=[pending],
        pending_restatement_intervals=[pending],
    )

    Snapshot.hydrate_with_intervals_by_version([live], [source])

    assert live.dev_intervals == [pending]
    assert live.intervals == []
    assert live.pending_restatement_intervals == [], (
        "production restatement coverage transferred to a development-only peer"
    )


def test_serialized_batch_preserves_independent_interval_ownership(tmp_path):
    from sqlmesh.core.state_sync.common import ExpiredSnapshotBatch

    expired = snapshot("expired", "shared_prod", "shared_dev", True)
    live = snapshot("live", "shared_prod", "shared_dev")
    live.physical_schema_ = "other_store"
    with store(tmp_path) as (adapter, state):
        state.push_snapshots([expired, live])
        state.add_interval(expired, "2026-09-01", "2026-09-02")
        state.add_interval(expired, "2026-09-01", "2026-09-02", is_dev=True)
        batch = state.get_expired_snapshots(
            batch_range=ExpiredBatchRange.all_batch_range(), current_ts=NOW
        )
        assert batch is not None and len(batch.cleanup_tasks) == 1

        restored_batch = ExpiredSnapshotBatch.parse_raw(batch.json())

        state.interval_state.cleanup_intervals(
            restored_batch.cleanup_tasks, restored_batch.expired_snapshot_ids
        )
        state.compact_intervals()
        restored = state.get_snapshots([live])[live.snapshot_id]
        assert restored.intervals == WINDOW
        assert restored.dev_intervals == WINDOW


@pytest.mark.parametrize("live_peer", [False, True])
def test_distinct_physical_schemas_are_not_shared_owners(tmp_path, live_peer):
    old = snapshot("old", "p", "d", True)
    peer = snapshot("peer", "p", "d", not live_peer)
    peer.physical_schema_ = "another_store"
    with store(tmp_path) as (adapter, state):
        adapter.create_schema("another_store")
        state.push_snapshots([old, peer])
        state.add_interval(old, "2026-09-01", "2026-09-02")
        state.add_interval(old, "2026-09-01", "2026-09-02", is_dev=True)
        materialize(adapter, [old, peer])
        assert not delete_expired_snapshots(
            state, SnapshotEvaluator(adapter, ddl_concurrent_tasks=1), current_ts=NOW
        )
        assert not adapter.table_exists(old.table_name(True)), (
            "unrelated physical owner leaked production table"
        )
        assert not adapter.table_exists(old.table_name(False)), (
            "unrelated physical owner leaked development table"
        )
        assert adapter.table_exists(peer.table_name(True)) == live_peer
        if live_peer:
            restored = state.get_snapshots([peer])[peer.snapshot_id]
            assert restored.intervals and restored.dev_intervals, (
                "physical deletion erased live logical coverage"
            )
