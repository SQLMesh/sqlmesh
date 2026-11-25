# SPDX-License-Identifier: Apache-2.0
import os
import sys

os.environ["MAX_FORK_WORKERS"] = "1"

from datetime import datetime, timezone
from pathlib import Path
import uuid

import duckdb  # noqa: TID253
import pytest
import time_machine
from sqlglot import exp
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    PostgresConnectionConfig,
)
from sqlmesh.core.config.model import ModelDefaultsConfig
from sqlmesh.core.context import Context

ROOT = Path(__file__).resolve().parents[2]
PG_HOST = os.environ.get("SQLMESH_REWRITE_TEST_HOST")
PG_PORT = int(os.environ.get("SQLMESH_REWRITE_TEST_PORT", "5432"))
PG_USER = os.environ.get("SQLMESH_REWRITE_TEST_USER", "postgres")
PG_PASSWORD = os.environ.get("SQLMESH_REWRITE_TEST_PASSWORD", "")


def day(n):
    return datetime(2026, 11, n, tzinfo=timezone.utc)


def state_config(database="postgres"):
    return PostgresConnectionConfig(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=database,
        concurrent_tasks=4,
    )


def config_for(root, database, parallel=False):
    return Config(
        gateways={
            "local": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    database=str(Path(root) / "warehouse.db"),
                    concurrent_tasks=4 if parallel else 1,
                ),
                state_connection=state_config(database),
                state_schema="sqlmesh",
            )
        },
        default_gateway="local",
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        disable_anonymized_analytics=True,
    )


@pytest.fixture
def context(tmp_path, request):
    import psycopg2

    if not PG_HOST:
        pytest.skip("Set SQLMESH_REWRITE_TEST_HOST to an isolated PostgreSQL cluster")
    database = "test_" + uuid.uuid4().hex
    connection = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres"
    )
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("CREATE DATABASE " + database)
    connection.close()
    (tmp_path / "models").mkdir()
    values = ",".join(f"('2026-11-{n:02}',{n})" for n in range(1, 9))
    (tmp_path / "models/upstream.sql").write_text(
        "MODEL (name repro.upstream, kind INCREMENTAL_BY_TIME_RANGE "
        "(time_column ds, batch_size 1, lookback 1), start '2026-11-01', cron '@daily', audits (not_null(columns := (id)))); "
        "SELECT CAST(ds AS DATE) ds, CAST(id AS INT) id "
        f"FROM (VALUES {values}) src(ds,id) WHERE CAST(ds AS DATE) BETWEEN @start_ds AND @end_ds;"
    )
    (tmp_path / "models/downstream.sql").write_text(
        "MODEL (name repro.downstream, kind INCREMENTAL_BY_TIME_RANGE "
        "(time_column ds, batch_size 2, lookback 5), start '2026-11-01', cron '@daily'); "
        "SELECT ds,id FROM repro.upstream WHERE ds BETWEEN @start_ds AND @end_ds;"
    )
    if getattr(request, "param", None) == "parallel":
        (tmp_path / "models/independent.sql").write_text(
            (tmp_path / "models/upstream.sql")
            .read_text()
            .replace("repro.upstream", "repro.independent")
        )
    if getattr(request, "param", None) == "transitive":
        downstream = tmp_path / "models/downstream.sql"
        downstream.write_text(
            downstream.read_text().replace("FROM repro.upstream", "FROM repro.middle")
        )
        (tmp_path / "models/middle.sql").write_text(
            "MODEL (name repro.middle, kind INCREMENTAL_BY_TIME_RANGE (time_column ds), start '2026-11-01', cron '@daily'); "
            "SELECT ds,id FROM repro.upstream WHERE ds BETWEEN @start_ds AND @end_ds;"
        )
    if getattr(request, "param", None) == "no-restatement":
        model = tmp_path / "models/upstream.sql"
        model.write_text(
            model.read_text().replace("lookback 1)", "lookback 1, disable_restatement TRUE)")
        )
    config = config_for(tmp_path, database, getattr(request, "param", None) == "parallel")
    ctx = Context(paths=[str(tmp_path)], config=config)
    ctx._prototype_path = tmp_path
    import json

    (tmp_path / "state-id.json").write_text(
        json.dumps(
            {
                "test": request.node.name,
                "database": database,
                "warehouse": str(tmp_path / "warehouse.db"),
                "schema": "sqlmesh",
            }
        )
    )
    with time_machine.travel(day(6), tick=False):
        ctx.apply(ctx.plan_builder("prod", execution_time=day(6), skip_tests=True).build())
    saved = ctx.snapshot_evaluator
    clone = saved.set_correlation_id(ctx.engine_adapter.correlation_id)
    from sqlmesh.core.snapshot import SnapshotEvaluator

    scheduler_evaluator = ctx.scheduler(
        environment="prod",
        snapshot_evaluator=SnapshotEvaluator(
            ctx.engine_adapters, selected_gateway=ctx.selected_gateway
        ),
    ).snapshot_evaluator
    ctx._retained_evaluators = {
        "original": saved,
        "clone": clone,
        "scheduler": scheduler_evaluator,
        "scheduler-clone": scheduler_evaluator.set_correlation_id(
            ctx.engine_adapter.correlation_id
        ),
    }
    ctx.state_sync.initialize_rewrite_protection()
    try:
        yield ctx
    finally:
        ctx.close()


def run(ctx, cutoff=7, **kwargs):
    with time_machine.travel(day(cutoff), tick=False):
        return ctx.run(
            "prod",
            start=day(1),
            end=day(cutoff),
            execution_time=day(cutoff),
            skip_janitor=True,
            **kwargs,
        )


def ids(ctx, model):
    snapshot = ctx.get_snapshot("repro." + model, raise_if_missing=True)
    return [
        r[0]
        for r in ctx.engine_adapter.fetchall(f"SELECT id FROM {snapshot.table_name()} ORDER BY id")
    ]


def fail_insert(ctx, monkeypatch, error, committed=False):
    upstream = ctx.get_snapshot("repro.upstream", raise_if_missing=True)
    adapter_type = type(ctx.engine_adapter)
    original = adapter_type._insert_append_query
    target = exp.to_table(upstream.table_name()).name

    def insert(self, table_name, query, *args, **kwargs):
        if exp.to_table(table_name).name == target and any(r[1] == 5 for r in self.fetchall(query)):
            assert 5 not in ids(ctx, "upstream")
            if committed:
                original(self, table_name, query, *args, **kwargs)
            if error == "terminal":
                self.execute(
                    f"INSERT INTO {upstream.table_name()} SELECT DATE '2026-11-05', CAST('bad' AS INT)"
                )
            raise RuntimeError("Lost or synthetic warehouse response")
        return original(self, table_name, query, *args, **kwargs)

    monkeypatch.setattr(adapter_type, "_insert_append_query", insert)


def test_confirmed_failure_repairs_same_cutoff(context, monkeypatch):
    ctx = context
    with monkeypatch.context() as patch:
        fail_insert(ctx, patch, "terminal")
        assert str(run(ctx)).endswith("FAILURE")
    assert ids(ctx, "upstream") == [1, 2, 3, 4, 6]
    assert ids(ctx, "downstream") == [1, 2, 3, 4, 5]
    fresh = ctx.config.get_state_connection(ctx.gateway).create_engine_adapter()
    from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync

    sync = EngineAdapterStateSync(fresh, schema="sqlmesh")
    snapshots = sync.get_snapshots(sync.get_environment("prod").snapshots)
    sync.refresh_snapshot_intervals(snapshots.values())
    upstream = next(s for s in snapshots.values() if s.name.endswith('"upstream"'))
    assert upstream.intervals == [(int(day(1).timestamp() * 1000), int(day(7).timestamp() * 1000))]
    fresh.close()
    assert str(run(ctx)).endswith("SUCCESS")
    for name in ["upstream", "downstream"]:
        assert ids(ctx, name) == list(range(1, 7))
    run(ctx)
    run(ctx, 8)
    for name in ["upstream", "downstream"]:
        assert ids(ctx, name) == list(range(1, 8))
    assert not ctx.state_sync.rewrite_state().list_unresolved()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_failure_blocks(context, monkeypatch, committed):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    ctx = context
    with monkeypatch.context() as patch:
        fail_insert(ctx, patch, "uncertain", committed)
        assert str(run(ctx)).endswith("FAILURE")
    for kwargs in [
        {},
        {"select_models": ["repro.downstream"], "no_auto_upstream": True},
    ]:
        with pytest.raises(RewriteBlockedError):
            run(ctx, **kwargs)
    assert ids(ctx, "downstream") == [1, 2, 3, 4, 5]


def test_selected_only_cannot_skip_terminal_repair(context, monkeypatch):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    with monkeypatch.context() as patch:
        fail_insert(context, patch, "terminal")
        run(context)
    with pytest.raises(RewriteBlockedError):
        run(context, select_models=["repro.downstream"], no_auto_upstream=True)
    assert ids(context, "downstream") == [1, 2, 3, 4, 5]


def test_before_delete_failure_keeps_data(context, monkeypatch):
    target = context.get_snapshot("repro.upstream", raise_if_missing=True).table_name()
    adapter_type = type(context.engine_adapter)
    original = adapter_type.insert_overwrite_by_time_partition

    def overwrite(self, table_name, *args, **kwargs):
        from sqlmesh.utils.date import to_timestamp

        if exp.to_table(table_name).name == exp.to_table(target).name and to_timestamp(
            kwargs["start"]
        ) == int(day(5).timestamp() * 1000):
            self.execute("SELECT missing_column FROM (SELECT 1) t")
        return original(self, table_name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(adapter_type, "insert_overwrite_by_time_partition", overwrite)
        assert str(run(context)).endswith("FAILURE")
    assert ids(context, "upstream") == list(range(1, 7))
    run(context)
    assert ids(context, "downstream") == list(range(1, 7))


def test_intent_persistence_failure_prevents_delete(context, monkeypatch):
    from sqlmesh.core.state_sync.db.rewrite import RewriteState

    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise RuntimeError("State intent was not persisted")

        patch.setattr(RewriteState, "begin", fail)
        assert str(run(context)).endswith("FAILURE")
    assert ids(context, "upstream") == list(range(1, 6))
    run(context)
    assert ids(context, "downstream") == list(range(1, 7))


def test_coverage_failure_is_uncertain(context, monkeypatch):
    from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    original = EngineAdapterStateSync.add_interval

    def fail(self, snapshot, start, end, **kwargs):
        if snapshot.name.endswith('"upstream"'):
            original(self, snapshot, start, end, **kwargs)
            raise duckdb.Error("State completion failed after warehouse success")
        return original(self, snapshot, start, end, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(EngineAdapterStateSync, "add_interval", fail)
        assert str(run(context)).endswith("FAILURE")
    assert ids(context, "upstream") == list(range(1, 7))
    with pytest.raises(RewriteBlockedError):
        run(context)
    assert ids(context, "downstream") == list(range(1, 6))


def test_audit_failure_requires_repair(context, monkeypatch):
    adapter_type = type(context.engine_adapter)
    original = adapter_type._insert_append_query
    upstream = context.get_snapshot("repro.upstream", raise_if_missing=True)

    def insert(self, table_name, query, *args, **kwargs):
        result = original(self, table_name, query, *args, **kwargs)
        if exp.to_table(table_name).name == exp.to_table(upstream.table_name()).name:
            self.execute(f"UPDATE {upstream.table_name()} SET id=NULL WHERE id=5")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(adapter_type, "_insert_append_query", insert)
        assert str(run(context)).endswith("FAILURE")
    assert ids(context, "downstream") == list(range(1, 6))
    run(context)
    assert ids(context, "downstream") == list(range(1, 7))


def test_direct_scheduler_run_repairs_without_auto_restatement(context, monkeypatch):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError
    from sqlmesh.core.snapshot import DeployabilityIndex

    with monkeypatch.context() as patch:
        fail_insert(context, patch, "terminal")
        run(context)
    scheduler = context.scheduler(environment="prod")
    upstream = context.get_snapshot("repro.upstream", raise_if_missing=True)
    with pytest.raises(RewriteBlockedError):
        scheduler.evaluate(upstream, day(5), day(6), day(7), DeployabilityIndex.all_deployable(), 0)
    with pytest.raises(RewriteBlockedError):
        context.snapshot_evaluator.evaluate(
            upstream, start=day(5), end=day(6), execution_time=day(7), snapshots={}
        )
    with pytest.raises(RewriteBlockedError):
        context.snapshot_evaluator.set_correlation_id(
            context.engine_adapter.correlation_id
        ).evaluate(upstream, start=day(5), end=day(6), execution_time=day(7), snapshots={})
    with time_machine.travel(day(7), tick=False):
        assert str(
            scheduler.run(
                "prod",
                start=day(1),
                end=day(7),
                execution_time=day(7),
                auto_restatement_enabled=False,
            )
        ).endswith("SUCCESS")
    assert ids(context, "downstream") == list(range(1, 7))


def test_plan_and_state_lifecycle_refused(context):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    plan = context.plan_builder(
        "prod",
        execution_time=day(7),
        restate_models=["repro.upstream"],
        skip_tests=True,
    ).build()
    with pytest.raises(RewriteBlockedError):
        context.apply(plan)
    for name in ["export", "rollback", "remove_state"]:
        with pytest.raises(RewriteBlockedError):
            getattr(context.state_sync, name)()
    with pytest.raises(RewriteBlockedError):
        context.state_sync.delete_snapshots([])
    with pytest.raises(RewriteBlockedError):
        context.run("prod")  # Default janitor is outside the scratch prototype scope.


def contend(database, token, start, ready, results):
    from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    adapter = state_config(database).create_engine_adapter()
    state = EngineAdapterStateSync(adapter, schema="sqlmesh").rewrite_state()
    ready.put(True)
    assert start.wait(15)
    try:
        state.claim(token)
        results.put((token, True))
    except RewriteBlockedError:
        results.put((token, False))
    finally:
        adapter.close()


def test_native_postgres_claim_races(context):
    import multiprocessing as mp

    database = context.config.get_state_connection(context.gateway).database
    spawn = mp.get_context("spawn")
    for _ in range(4):
        ready, results, start = spawn.Queue(), spawn.Queue(), spawn.Event()
        children = [
            spawn.Process(target=contend, args=(database, uuid.uuid4().hex, start, ready, results))
            for _ in range(4)
        ]
        for child in children:
            child.start()
        for child in children:
            assert ready.get(timeout=15)
        start.set()
        outcomes = [results.get(timeout=15) for child in children]
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        assert sum(won for token, won in outcomes) == 1
        winner = next(token for token, won in outcomes if won)
        state = context.state_sync.rewrite_state()
        state.claim(winner)  # Lost claim response can be retried by exact token.
        state.release(winner)  # These children submitted no warehouse work.


def test_native_completion_rollback_identity_and_retry(context, monkeypatch):
    from dataclasses import replace
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError, RewriteStatus

    state = context.state_sync.rewrite_state()
    snapshot = context.get_snapshot("repro.upstream", raise_if_missing=True)
    owner = uuid.uuid4().hex
    state.claim(owner)
    a = state.begin(owner, snapshot, day(5), day(6), uuid.uuid4().hex)
    assert state.begin(owner, snapshot, day(5), day(6), a.attempt_id) == a
    table = state.sync.interval_state.intervals_table.sql(dialect="postgres")
    count = lambda: state.adapter.fetchone(f"SELECT COUNT(*) FROM {table}")[0]
    initial = count()
    original = type(state.adapter).update_table

    def fail(self, table_name, properties, *args, **kwargs):
        if (
            exp.to_table(table_name) == state.attempts_table
            and properties.get("status") == "resolved"
        ):
            original(self, table_name, properties, *args, **kwargs)
            raise RuntimeError("Completion failed after coverage and resolution, before commit")
        return original(self, table_name, properties, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(state.adapter), "update_table", fail)
        with pytest.raises(RuntimeError):
            state.finish(a, snapshot)
    assert count() == initial
    assert state.list_unresolved() == [a]
    state.finish(a, snapshot)
    assert count() == initial + 1
    state.finish(a, snapshot)
    assert count() == initial + 1
    with pytest.raises(RewriteBlockedError):
        state.finish(replace(a, attempt_id=uuid.uuid4().hex), snapshot)
    state.release(owner)
    new_owner = uuid.uuid4().hex
    state.claim(new_owner)
    b = state.begin(new_owner, snapshot, day(5), day(6))
    with pytest.raises(RewriteBlockedError):
        state.finish(a, snapshot)
    assert state.list_unresolved() == [b]
    state.terminal(b)
    state.release(new_owner)
    # Only a terminal attempt can be adopted automatically at admission.
    final_owner = uuid.uuid4().hex
    state.claim(final_owner)
    assert state.list_unresolved() == [replace(b, owner=final_owner, status=RewriteStatus.TERMINAL)]
    with pytest.raises(RewriteBlockedError):
        state.begin(new_owner, snapshot, day(5), day(6), b.attempt_id)
    state.release(final_owner)


def die_after_delete(root, database):
    from unittest.mock import patch

    ctx = Context(paths=[root], config=config_for(root, database))
    target = ctx.get_snapshot("repro.upstream", raise_if_missing=True).table_name()
    original = type(ctx.engine_adapter)._insert_append_query

    def insert(self, table_name, query, *args, **kwargs):
        if exp.to_table(table_name).name == exp.to_table(target).name and any(
            r[1] == 5 for r in self.fetchall(query)
        ):
            assert 5 not in ids(ctx, "upstream")
            os._exit(23)
        return original(self, table_name, query, *args, **kwargs)

    with patch.object(type(ctx.engine_adapter), "_insert_append_query", insert):
        run(ctx)
    os._exit(24)


def test_actual_process_death_remains_blocked(context):
    import multiprocessing as mp
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    root = str(context._prototype_path)
    database = context.config.get_state_connection(context.gateway).database
    context.close()
    child = mp.get_context("spawn").Process(target=die_after_delete, args=(root, database))
    child.start()
    child.join(20)
    assert child.exitcode == 23
    fresh = Context(paths=[root], config=config_for(root, database))
    try:
        assert ids(fresh, "upstream") == [1, 2, 3, 4]
        with time_machine.travel(day(25), tick=False):
            with pytest.raises(RewriteBlockedError):
                run(fresh, 9)
        assert ids(fresh, "downstream") == list(range(1, 6))
        assert len(fresh.state_sync.rewrite_state().list_unresolved()) == 1
    finally:
        fresh.close()


@pytest.mark.parametrize("context", ["parallel"], indirect=True)
def test_distinct_targets_parallel_same_target_serial(context, monkeypatch):
    import threading
    import time

    target_lock = threading.Lock()
    active = set()
    overlapping_targets = []
    barrier = threading.Barrier(2, timeout=10)
    seen = set()
    adapter_type = type(context.engine_adapter)
    original = adapter_type._insert_append_query

    def insert(self, table_name, query, *args, **kwargs):
        target = exp.to_table(table_name).name
        with target_lock:
            assert target not in active
            active.add(target)
            overlapping_targets.append(len(active))
            first_root = ("upstream" in target or "independent" in target) and target not in seen
            seen.add(target)
        try:
            if first_root:
                barrier.wait()
            time.sleep(0.02)
            return original(self, table_name, query, *args, **kwargs)
        finally:
            with target_lock:
                active.remove(target)

    scheduler = context.scheduler(environment="prod")
    scheduler.max_workers = 4
    errors = []
    merged_run = scheduler.run_merged_intervals

    def capture(**kwargs):
        result = merged_run(**kwargs)
        errors.extend(repr(e.__cause__) for e in result[0])
        return result

    monkeypatch.setattr(scheduler, "run_merged_intervals", capture)
    with monkeypatch.context() as patch:
        patch.setattr(adapter_type, "_insert_append_query", insert)
        with time_machine.travel(day(7), tick=False):
            assert str(
                scheduler.run("prod", start=day(1), end=day(7), execution_time=day(7))
            ).endswith("SUCCESS"), errors
    assert max(overlapping_targets) >= 2
    for name in ["upstream", "downstream", "independent"]:
        assert ids(context, name) == list(range(1, 7))


def test_overlapping_scheduler_refused_before_write(context, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    entered, resume = threading.Event(), threading.Event()
    original = type(context.engine_adapter)._insert_append_query

    def insert(self, table_name, query, *args, **kwargs):
        if "upstream" in exp.to_table(table_name).name and any(
            r[1] == 5 for r in self.fetchall(query)
        ):
            entered.set()
            assert resume.wait(10)
        return original(self, table_name, query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(context.engine_adapter), "_insert_append_query", insert)
        with time_machine.travel(day(7), tick=False), ThreadPoolExecutor(1) as pool:
            admitted = context.scheduler(environment="prod")
            running = pool.submit(
                admitted.run,
                "prod",
                start=day(1),
                end=day(7),
                execution_time=day(7),
            )
            try:
                assert entered.wait(10)
                competing = context.scheduler(environment="prod")
                with pytest.raises(RewriteBlockedError):
                    competing.run("prod", start=day(1), end=day(7), execution_time=day(7))
                from sqlmesh.core.snapshot import DeployabilityIndex

                upstream = context.get_snapshot("repro.upstream", raise_if_missing=True)
                with pytest.raises(RewriteBlockedError):
                    admitted.evaluate(
                        upstream,
                        day(5),
                        day(6),
                        day(7),
                        DeployabilityIndex.all_deployable(),
                        0,
                    )
            finally:
                resume.set()
            assert str(running.result(10)).endswith("SUCCESS")
    assert ids(context, "downstream") == list(range(1, 7))


def test_lost_begin_response_blocks_without_delete(context, monkeypatch):
    from sqlmesh.core.state_sync.db.rewrite import RewriteState
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    original = RewriteState.begin

    def begin(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("Intent committed, acknowledgment lost")

    with monkeypatch.context() as patch:
        patch.setattr(RewriteState, "begin", begin)
        assert str(run(context)).endswith("FAILURE")
    assert ids(context, "upstream") == list(range(1, 6))
    with pytest.raises(RewriteBlockedError):
        run(context)


def test_lost_committed_completion_response_reconciles(context, monkeypatch):
    from sqlmesh.core.state_sync.db.rewrite import RewriteState

    original = RewriteState.finish

    def finish(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("Completion committed, acknowledgment lost")

    with monkeypatch.context() as patch:
        patch.setattr(RewriteState, "finish", finish)
        assert str(run(context)).endswith("FAILURE")
    assert not context.state_sync.rewrite_state().list_unresolved()
    assert ids(context, "upstream") == list(range(1, 7))
    assert str(run(context)).endswith("SUCCESS")
    assert ids(context, "downstream") == list(range(1, 7))


def test_overlap_does_not_clear_other_attempt(context):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    state = context.state_sync.rewrite_state()
    snapshot = context.get_snapshot("repro.upstream", raise_if_missing=True)
    owner = uuid.uuid4().hex
    state.claim(owner)
    a = state.begin(owner, snapshot, day(5), day(6))
    with pytest.raises(RewriteBlockedError):
        state.claim(uuid.uuid4().hex)
    with pytest.raises(RewriteBlockedError):
        state.begin(owner, snapshot, day(5), day(6))
    b = state.begin(owner, snapshot, day(6), day(7))
    state.finish(b, snapshot)
    context.state_sync.compact_intervals()
    assert state.list_unresolved() == [a]
    state.release(owner)
    with pytest.raises(RewriteBlockedError):
        state.claim(uuid.uuid4().hex)  # Active ownership is never adopted.


def test_initialization_constraints_and_backend_refusal(context, tmp_path):
    from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    state = context.state_sync.rewrite_state()
    context.state_sync.initialize_rewrite_protection()
    context.state_sync.initialize_rewrite_protection()
    table = state.owner_table.sql(dialect="postgres")
    import psycopg2

    with pytest.raises(psycopg2.IntegrityError), state.adapter.transaction():
        state.adapter.execute(f"INSERT INTO {table}(id,state_schema) VALUES(2,'sqlmesh')")
    with pytest.raises(psycopg2.IntegrityError), state.adapter.transaction():
        state.adapter.execute(f"INSERT INTO {table}(id,state_schema) VALUES(1,'sqlmesh')")
    assert state.adapter.fetchone(f"SELECT COUNT(*) FROM {table}")[0] == 1
    adapter = DuckDBConnectionConfig(
        database=str(tmp_path / "unsupported-state.db")
    ).create_engine_adapter()
    try:
        unsupported = EngineAdapterStateSync(adapter, schema="sqlmesh")
        with pytest.raises(RewriteBlockedError):
            unsupported.initialize_rewrite_protection()
    finally:
        adapter.close()


@pytest.mark.parametrize("failure", ["terminal", "uncertain"])
def test_native_cli_parsing_and_exit(context, monkeypatch, failure):
    import json
    import subprocess

    root = context._prototype_path
    database = context.config.get_state_connection(context.gateway).database
    with monkeypatch.context() as patch:
        fail_insert(context, patch, failure)
        assert str(run(context)).endswith("FAILURE")
    raw_config = {
        "gateways": {
            "local": {
                "connection": {
                    "type": "duckdb",
                    "database": str(root / "warehouse.db"),
                    "concurrent_tasks": 1,
                },
                "state_connection": {
                    "type": "postgres",
                    "host": PG_HOST,
                    "port": PG_PORT,
                    "user": PG_USER,
                    "password": PG_PASSWORD,
                    "database": database,
                    "concurrent_tasks": 4,
                },
                "state_schema": "sqlmesh",
            }
        },
        "default_gateway": "local",
        "model_defaults": {"dialect": "duckdb"},
        "disable_anonymized_analytics": True,
    }
    (root / "config.yaml").write_text(json.dumps(raw_config))
    context.close()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sqlmesh.cli.main import cli; cli()",
            "-p",
            str(root),
            "run",
            "prod",
            "--skip-janitor",
            "--start",
            "2026-11-01T00:00:00+00:00",
            "--end",
            "2026-11-07T00:00:00+00:00",
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    (root / "cli-result.json").write_text(
        json.dumps(
            {
                "argv": result.args,
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            indent=2,
        )
    )
    assert result.returncode == (0 if failure == "terminal" else 1), result.stderr
    fresh = Context(paths=[str(root)], config=config_for(root, database))
    try:
        assert ids(fresh, "downstream") == (
            list(range(1, 7)) if failure == "terminal" else list(range(1, 6))
        )
        if failure == "uncertain":
            output = result.stdout + result.stderr
            assert "unresolved" in output and "upstream" in output
    finally:
        fresh.close()


def test_warehouse_route_change_is_refused(context, tmp_path):
    import shutil
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    run(context)
    changed = tmp_path / "changed"
    changed.mkdir()
    shutil.copytree(context._prototype_path / "models", changed / "models")
    database = context.config.get_state_connection(context.gateway).database
    fresh = Context(paths=[str(changed)], config=config_for(changed, database))
    try:
        with pytest.raises(RewriteBlockedError, match="routing changed"):
            run(fresh)
    finally:
        fresh.close()


def test_lost_claim_response_retains_admission(context, monkeypatch):
    from sqlmesh.core.state_sync.db.rewrite import RewriteState
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    original = RewriteState.claim

    def claim(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("Claim committed, acknowledgment lost")

    with monkeypatch.context() as patch:
        patch.setattr(RewriteState, "claim", claim)
        with pytest.raises(RuntimeError):
            run(context)
    with pytest.raises(RewriteBlockedError):
        run(context)
    assert ids(context, "upstream") == list(range(1, 6))


@pytest.mark.parametrize("context", ["no-restatement"], indirect=True)
def test_repair_independent_of_disable_restatement(context, monkeypatch):
    with monkeypatch.context() as patch:
        fail_insert(context, patch, "terminal")
        assert str(run(context)).endswith("FAILURE")
    assert str(run(context)).endswith("SUCCESS")
    assert ids(context, "downstream") == list(range(1, 7))


def test_direct_merged_scheduler_refused(context):
    from sqlmesh.core.snapshot import DeployabilityIndex
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    scheduler = context.scheduler(environment="prod")
    snapshot = context.get_snapshot("repro.upstream", raise_if_missing=True)
    with pytest.raises(RewriteBlockedError):
        scheduler.run_merged_intervals(
            merged_intervals={snapshot: []},
            deployability_index=DeployabilityIndex.all_deployable(),
            environment_naming_info=context.state_sync.get_environment("prod").naming_info,
        )


def test_outer_state_transaction_refused_before_write(context, monkeypatch):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    state = context.state_sync.rewrite_state()
    with pytest.raises(RuntimeError, match="Caller rollback"):
        with state.adapter.transaction():
            with monkeypatch.context() as patch:
                fail_insert(context, patch, "uncertain")
                with pytest.raises(RewriteBlockedError):
                    run(context)
            assert ids(context, "upstream") == list(range(1, 6))
            assert ids(context, "downstream") == list(range(1, 6))
            raise RuntimeError("Caller rollback")
    assert state.list_unresolved() == []
    assert str(run(context, select_models=["repro.downstream"], no_auto_upstream=True)).endswith(
        "SUCCESS"
    )
    # A refused execution cannot erase intent for a DELETE that never happened.
    assert ids(context, "downstream") == list(range(1, 6))
    assert str(run(context, 8)).endswith("SUCCESS")
    assert ids(context, "upstream") == ids(context, "downstream") == list(range(1, 8))


def test_durable_mutations_refuse_caller_transaction(context):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    state = context.state_sync.rewrite_state()
    snapshot = context.get_snapshot("repro.upstream", raise_if_missing=True)
    owner = uuid.uuid4().hex
    with state.adapter.transaction():
        with pytest.raises(RewriteBlockedError):
            context.state_sync.initialize_rewrite_protection()
        with pytest.raises(RewriteBlockedError):
            state.claim(owner)
    state.claim(owner)
    a = state.begin(owner, snapshot, day(5), day(6))
    table = state.sync.interval_state.intervals_table.sql(dialect="postgres")
    initial = state.adapter.fetchone(f"SELECT COUNT(*) FROM {table}")[0]
    mutations = [
        lambda: state.claim(owner),
        lambda: state.check_owner(owner),
        lambda: state.check_attempt(a),
        lambda: state.begin(owner, snapshot, day(6), day(7)),
        lambda: state.terminal(a),
        lambda: state.finish(a, snapshot),
        lambda: state.release(owner),
        lambda: state.bind_warehouse(owner, str(context._prototype_path / "warehouse.db")),
    ]
    for mutate in mutations:
        with state.adapter.transaction(), pytest.raises(RewriteBlockedError):
            mutate()
    assert state.list_unresolved() == [a]
    assert state.adapter.fetchone(f"SELECT COUNT(*) FROM {table}")[0] == initial
    state.finish(a, snapshot)
    assert state.list_unresolved() == []
    assert state.adapter.fetchone(f"SELECT COUNT(*) FROM {table}")[0] == initial + 1
    state.release(owner)


@pytest.mark.parametrize("retained", ["original", "clone", "scheduler", "scheduler-clone"])
def test_retained_evaluator_observes_initialization(context, monkeypatch, retained):
    from sqlmesh.core.state_sync.rewrite import RewriteBlockedError

    evaluator = context._retained_evaluators[retained]
    snapshot = context.get_snapshot("repro.upstream", raise_if_missing=True)
    with monkeypatch.context() as patch:
        fail_insert(context, patch, "uncertain")
        with pytest.raises(RewriteBlockedError):
            evaluator.evaluate(
                snapshot, start=day(5), end=day(6), execution_time=day(7), snapshots={}
            )
    with pytest.raises(RewriteBlockedError):
        evaluator.cleanup([])
    with pytest.raises(RewriteBlockedError):
        evaluator.create([snapshot], {snapshot.snapshot_id: snapshot})
    assert ids(context, "upstream") == list(range(1, 6))
    assert context.state_sync.rewrite_state().list_unresolved() == []
    assert str(run(context, select_models=["repro.downstream"], no_auto_upstream=True)).endswith(
        "SUCCESS"
    )
    assert ids(context, "downstream") == list(range(1, 6))


@pytest.mark.parametrize("context", ["transitive"], indirect=True)
def test_transitive_terminal_repairs_precede_downstream(context):
    from sqlmesh.core.state_sync.db.facade import EngineAdapterStateSync

    state = context.state_sync.rewrite_state()
    owner = uuid.uuid4().hex
    state.claim(owner)
    parent = context.get_snapshot("repro.upstream", raise_if_missing=True)
    child = context.get_snapshot("repro.downstream", raise_if_missing=True)
    attempts = []
    receipts = []
    for snapshot, start, end in [(child, day(1), day(2)), (parent, day(5), day(6))]:
        a = state.begin(owner, snapshot, start, end)
        context.engine_adapter.execute(
            f"DELETE FROM {snapshot.table_name()} WHERE ds=DATE '{start.date()}'"
        )
        state.terminal(a)
        attempts.append(a.attempt_id)
        receipts.append(a.__dict__)
    import json

    (context._prototype_path / "terminal-attempts.json").write_text(json.dumps(receipts, indent=2))
    state.release(owner)
    # Reopen native state so ordering uses persisted terminal records.
    fresh = state_config(
        context.config.get_state_connection(context.gateway).database
    ).create_engine_adapter()
    try:
        sync = EngineAdapterStateSync(fresh, schema="sqlmesh")
        assert {a.attempt_id for a in sync.rewrite_state().list_unresolved()} == set(attempts)
    finally:
        fresh.close()
    from sqlmesh.utils import CompletionStatus

    for expected in [CompletionStatus.SUCCESS, CompletionStatus.NOTHING_TO_DO]:
        assert run(context) == expected
        assert state.list_unresolved() == []
        for model in ["upstream", "middle", "downstream"]:
            assert ids(context, model) == list(range(1, 7))
