# SPDX-License-Identifier: Apache-2.0

import pytest
from sqlglot import parse_one
from sqlmesh.core.model import SqlModel
from sqlmesh.core.model.kind import FullKind
from sqlmesh.core.snapshot import Snapshot, SnapshotChangeCategory, SnapshotEvaluator
from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync
from sqlmesh.core.state_sync.common import ExpiredBatchRange
from sqlmesh.core.engine_adapter import create_engine_adapter
from sqlmesh.utils.date import now_timestamp


def make(query, description):
    return Snapshot.from_node(
        SqlModel(name="s.a", kind=FullKind(), query=parse_one(query), description=description),
        nodes={},
        ttl="in 10 seconds",
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("compact", [False, True])
def test_shared_dev_survives_other_production_cleanup(tmp_path, reverse, compact):
    import duckdb

    old = make("select 1 as a", "old")
    old.categorize_as(SnapshotChangeCategory.BREAKING)
    a = make("select 2 as a", "a")
    a.categorize_as(SnapshotChangeCategory.BREAKING)
    b = make("select 2 as a", "b")
    b.previous_versions = (old.data_version,)
    b.categorize_as(SnapshotChangeCategory.NON_BREAKING, forward_only=True)
    expired, live = (b, a) if reverse else (a, b)
    now = now_timestamp()
    expired.updated_ts = now - 20000
    live.updated_ts = now
    adapter = create_engine_adapter(lambda: duckdb.connect(), "duckdb")
    try:
        state = EngineAdapterStateSync(adapter, None, cache_dir=tmp_path)
        state.migrate(skip_backup=True)
        state.push_snapshots([expired, live])
        state.add_interval(expired, "2026-09-01", "2026-09-02", is_dev=True)
        state.add_interval(expired, "2026-09-01", "2026-09-02")
        if compact:
            state.interval_state.compact_intervals()
        adapter.create_schema("sqlmesh__s")
        for table in {
            expired.table_name(True),
            live.table_name(True),
            live.table_name(False),
        }:
            adapter.execute(f"CREATE TABLE {table} AS SELECT 42 AS marker")
        batch = state.get_expired_snapshots(
            batch_range=ExpiredBatchRange.all_batch_range(), current_ts=now
        )
        assert batch is not None
        SnapshotEvaluator(adapter, ddl_concurrent_tasks=1).cleanup(batch.cleanup_tasks)
        assert adapter.table_exists(live.table_name(False)), "shared development table was dropped"
        assert adapter.table_exists(live.table_name(True)), "live production table was dropped"
        assert not adapter.table_exists(expired.table_name(True)), (
            "orphan production table was leaked"
        )
        state.delete_expired_snapshots(
            batch_range=ExpiredBatchRange.all_batch_range(), current_ts=now
        )
        restored = state.get_snapshots([live])[live.snapshot_id]
        assert restored.dev_intervals, "shared development coverage was deleted"
        assert not restored.intervals
        state.interval_state.compact_intervals()
        assert state.get_snapshots([live])[live.snapshot_id].dev_intervals
    finally:
        adapter.close()


@pytest.mark.parametrize("forward_survives", [False, True])
def test_cleanup_preserves_other_environment(tmp_path, forward_survives):
    from sqlmesh.core.context import Context
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        DuckDBConnectionConfig,
    )

    (tmp_path / "models").mkdir()
    path = tmp_path / "models" / "a.sql"

    def write(value, description):
        path.write_text(
            f"MODEL (name s.a, kind FULL, start '2026-09-01', description '{description}'); SELECT {value} AS a"
        )

    write(1, "baseline")
    ctx = Context(
        paths=[tmp_path],
        config=Config(
            gateways={"local": GatewayConfig(connection=DuckDBConnectionConfig())},
            model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        ),
    )
    try:
        ctx.plan("prod", skip_tests=True, no_prompts=True, auto_apply=True)
        common = dict(
            skip_tests=True, no_prompts=True, auto_apply=True, start="2026-09-18", end="2026-09-19"
        )
        write(2, "breaking")
        ctx.load()
        p1 = ctx.plan("branch_a", **common)
        write(2, "forward")
        ctx.load()
        p2 = ctx.plan("branch_b", forward_only=True, **common)
        a = next(iter(p1.snapshots.values()))
        b = next(iter(p2.snapshots.values()))
        assert a.dev_version == b.dev_version and a.version != b.version
        expired, live = (a, b) if forward_survives else (b, a)
        state = ctx.state_sync
        state.invalidate_environment("branch_a" if forward_survives else "branch_b")
        state.delete_expired_environments()
        adapter = ctx.engine_adapter
        shared = live.table_name(False)
        # A FULL model may use its deployable table in dev; materialize a preview
        # as the evaluator does for a non-deployable snapshot.
        adapter.execute(f"CREATE TABLE IF NOT EXISTS {shared} AS SELECT 2 AS a")
        before = adapter.fetchall(f"SELECT * FROM {shared}")
        batch = state.get_expired_snapshots(
            batch_range=ExpiredBatchRange.all_batch_range(), ignore_ttl=True
        )
        assert batch is not None
        SnapshotEvaluator(adapter, ddl_concurrent_tasks=1).cleanup(batch.cleanup_tasks)
        assert adapter.table_exists(shared), "cleanup removed the other branch preview"
        assert adapter.fetchall(f"SELECT * FROM {shared}") == before
    finally:
        ctx.close()
