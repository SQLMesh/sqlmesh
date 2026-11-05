# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import typing as t
from pathlib import Path

from examples.query_execution_context.observer import QueryContextObserver
from sqlmesh.core.config import Config, ModelDefaultsConfig
from sqlmesh.core.config.connection import DuckDBConnectionConfig
from sqlmesh.core.config.gateway import GatewayConfig
from sqlmesh.core.context import Context
from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.execution_observation import observer_scope
from sqlmesh.core.state_sync import CachingStateSync, EngineAdapterStateSync


def _payload(sql: str) -> t.Optional[t.Dict[str, t.Any]]:
    match = re.search(r"/\* QUERY_CONTEXT: (.*?) \*/", sql)
    return json.loads(match[1]) if match else None


def _new_context(root: Path, enabled: bool) -> Context:
    models = Path(__file__).parents[3] / "examples/query_execution_context/models"
    shutil.copytree(models, root / "models")
    config = Config(
        gateways={
            "local": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    database=str(root / "warehouse.duckdb"),
                    concurrent_tasks=2,
                    query_execution_context=enabled,
                )
            )
        },
        default_gateway="local",
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
    )
    return Context(paths=root, config=config, load=False)


def _batch_ids(
    sqls: t.List[str], parent_type: str, phase: str
) -> t.Dict[t.Tuple[str, str, int], str]:
    batches: t.Dict[t.Tuple[str, str, int], str] = {}
    for sql in sqls:
        payload = _payload(sql)
        if not payload or not payload.get("operation") or payload["query"]["phase"] != phase:
            continue
        assert payload["schema_version"] == 1
        assert payload["emitter"] == "sqlmesh"
        assert payload["parent"]["type"] == parent_type
        assert payload["parent"]["system"] == "sqlmesh"
        assert sql.startswith(f"/* SQLMESH_{parent_type.upper()}:")
        assert payload["workload"]["system"] == "sqlmesh"
        assert payload["workload"]["type"] == "model"
        assert payload["workload"]["version"]
        operation = payload["operation"]
        assert operation["type"] == "evaluate"
        key = (
            payload["workload"]["name"],
            operation["interval_start"],
            operation["batch_index"],
        )
        if key in batches:
            assert batches[key] == operation["id"]
        batches[key] = operation["id"]
    return batches


def test_provider_connects_native_events_to_warehouse_history(tmp_path, monkeypatch):
    submitted = []
    original_execute = EngineAdapter._execute

    def capture(adapter, sql, *args, **kwargs):
        submitted.append((adapter, sql))
        return original_execute(adapter, sql, *args, **kwargs)

    monkeypatch.setattr(EngineAdapter, "_execute", capture)
    results = []
    for enabled in (False, True):
        context = _new_context(tmp_path / str(enabled), enabled)
        events = []

        def observer(event, facts, error):
            current = consumer.current_query_context()
            if facts["kind"] == "model":
                events.append(
                    (event, facts["snapshot"].name, current.evaluation.evaluation_id, error)
                )

        consumer = QueryContextObserver(observer)
        for adapter in context.engine_adapters.values():
            adapter.query_execution_context_provider = consumer.current_query_context
        context.load()
        try:
            hashes = {m.name: (m.data_hash, m.metadata_hash) for m in context.models.values()}
            submitted.clear()
            with observer_scope(consumer):
                plan = context.plan(
                    "dev", start="2024-01-01", end="2024-01-02", no_prompts=True, skip_tests=True
                )
                context.apply(plan)
                assert consumer.current_query_context() is None
                plan_sql = [sql for _, sql in submitted]
                submitted.clear()
                assert context.run("dev", start="2024-01-03", end="2024-01-03").is_success
                assert consumer.current_query_context() is None
                run_sql = [sql for _, sql in submitted]
                state = context.state_sync
                assert isinstance(state, CachingStateSync)
                assert isinstance(state.state_sync, EngineAdapterStateSync)
                state_adapter = state.state_sync.engine_adapter
                assert any(adapter is state_adapter for adapter, _ in submitted)
                assert all(
                    _payload(sql) is None for adapter, sql in submitted if adapter is state_adapter
                )
            rows = context.engine_adapter.fetchall(
                f"SELECT ds, id FROM {context.resolve_table('e2e.events')} ORDER BY ds"
            )
            assert rows == [(dt.date(2024, 1, day), 1) for day in (1, 2, 3)]
            results.append((hashes, rows))
            if enabled:
                materialize = _batch_ids(plan_sql, "plan", "materialize")
                assert _batch_ids(plan_sql, "plan", "audit") == materialize
                assert set(materialize) == {
                    ('"warehouse"."e2e"."events"', "2024-01-01T00:00:00+00:00", 0),
                    ('"warehouse"."e2e"."events"', "2024-01-02T00:00:00+00:00", 1),
                    ('"warehouse"."e2e"."aux_events"', "2024-01-01T00:00:00+00:00", 0),
                    ('"warehouse"."e2e"."aux_events"', "2024-01-02T00:00:00+00:00", 1),
                }
                later = _batch_ids(run_sql, "run", "materialize")
                assert _batch_ids(run_sql, "run", "audit") == later
                assert set(later) == {
                    ('"warehouse"."e2e"."events"', "2024-01-03T00:00:00+00:00", 0),
                    ('"warehouse"."e2e"."aux_events"', "2024-01-03T00:00:00+00:00", 0),
                }
                all_ids = set(materialize.values()) | set(later.values())
                assert len(all_ids) == 6
                assert {operation_id for _, _, operation_id, _ in events} == all_ids
                assert [event for event, _, _, _ in events].count("start") == 6
                assert [event for event, _, _, _ in events].count("finish") == 6
                assert all(error is None for _, _, _, error in events)
            else:
                assert all(_payload(sql) is None for sql in plan_sql + run_sql)
                assert any(sql.startswith("/* SQLMESH_PLAN:") for sql in plan_sql)
        finally:
            context.close()
    assert results[0] == results[1]
