# SPDX-License-Identifier: Apache-2.0
"""Run a local plan and scheduler with observer-owned query context."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import tempfile
import typing as t
from pathlib import Path

from examples.query_execution_context.observer import QueryContextObserver
from sqlmesh.core.config import Config, ModelDefaultsConfig
from sqlmesh.core.config.connection import DuckDBConnectionConfig
from sqlmesh.core.config.gateway import GatewayConfig
from sqlmesh.core.console import NoopConsole
from sqlmesh.core.context import Context
from sqlmesh.core.execution_observation import observer_scope


class _QueryLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.statements: t.List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("Executing SQL: "):
            self.statements.append(message.removeprefix("Executing SQL: "))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sqlmesh-query-context-") as directory:
        root = Path(directory)
        shutil.copytree(Path(__file__).parent / "models", root / "models")
        context = Context(
            paths=root,
            config=Config(
                gateways={
                    "local": GatewayConfig(
                        connection=DuckDBConnectionConfig(
                            database=str(root / "warehouse.duckdb"),
                            concurrent_tasks=2,
                            query_execution_context=True,
                        )
                    )
                },
                default_gateway="local",
                model_defaults=ModelDefaultsConfig(dialect="duckdb"),
            ),
            load=False,
        )
        context.console = NoopConsole()
        ids: t.Set[str] = set()

        def record(
            event: t.Literal["start", "finish"],
            facts: t.Mapping[str, t.Any],
            error: t.Optional[BaseException],
        ) -> None:
            if event == "finish" and facts["kind"] == "model":
                value = consumer.current_query_context()
                assert value is not None and value.evaluation is not None
                ids.add(value.evaluation.evaluation_id)

        consumer = QueryContextObserver(record)
        for adapter in context.engine_adapters.values():
            adapter.query_execution_context_provider = consumer.current_query_context
        context.load()
        query_log = _QueryLog()
        logger = logging.getLogger("sqlmesh.core.engine_adapter.base")
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(query_log)
        try:
            with observer_scope(consumer):
                plan = context.plan(
                    "dev", start="2024-01-01", end="2024-01-02", no_prompts=True, skip_tests=True
                )
                context.apply(plan)
                assert context.run("dev", start="2024-01-03", end="2024-01-03").is_success
            assert consumer.current_query_context() is None
            assert len(ids) == 6
            phases: t.Dict[str, t.Set[str]] = {}
            for sql in query_log.statements:
                match = re.search(r"/\* QUERY_CONTEXT: (.*?) \*/", sql)
                if match is None:
                    continue
                payload = json.loads(match[1])
                operation = payload.get("operation")
                if operation is not None:
                    phases.setdefault(operation["id"], set()).add(payload["query"]["phase"])
            assert set(phases) == ids
            assert all({"materialize", "audit"} <= value for value in phases.values())
            rows = context.engine_adapter.fetchall(
                f"SELECT ds, id FROM {context.resolve_table('e2e.events')} ORDER BY ds"
            )
            assert rows == [(dt.date(2024, 1, day), 1) for day in (1, 2, 3)]
            print("Verified six model-batch IDs and three resulting rows.")
        finally:
            logger.removeHandler(query_log)
            logger.setLevel(previous_level)
            context.close()


if __name__ == "__main__":
    main()
