from __future__ import annotations

import logging
import threading
import typing as t
from enum import Enum

logger = logging.getLogger(__name__)

if t.TYPE_CHECKING:
    import pandas as pd


class SqlIntent(Enum):
    ADHOC_QUERY = "adhoc_query"
    PIPELINE_DDL = "pipeline_ddl"
    DATA_INGRESS = "data_ingress"
    NO_OP = "no_op"


def _classify(sql: str) -> SqlIntent:
    """Route SQL to the correct Feldera endpoint."""
    stripped = sql.strip().lstrip("/*").strip()
    if not stripped:
        return SqlIntent.NO_OP
    upper = stripped.upper()
    if upper.startswith(("CREATE", "DROP", "ALTER")):
        return SqlIntent.PIPELINE_DDL
    if upper.startswith("INSERT"):
        return SqlIntent.DATA_INGRESS
    return SqlIntent.ADHOC_QUERY


class PipelineStateManager:
    """Accumulates DDL and deploys it as a single Feldera pipeline program."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: t.Dict[str, str] = {}
        self._views: t.Dict[str, str] = {}
        self._pipeline = None

    def register_ddl(self, sql: str) -> None:
        with self._lock:
            upper = sql.strip().upper()
            if "CREATE TABLE" in upper or "CREATE MATERIALIZED TABLE" in upper:
                self._tables[_extract_name(sql)] = sql
            elif "CREATE VIEW" in upper or "CREATE MATERIALIZED VIEW" in upper:
                self._views[_extract_name(sql)] = sql
            elif "DROP TABLE" in upper:
                self._tables.pop(_extract_name(sql), None)
            elif "DROP VIEW" in upper:
                self._views.pop(_extract_name(sql), None)

    def assemble_program(self) -> str:
        with self._lock:
            statements = [
                *(ddl.rstrip(";") + ";" for ddl in self._tables.values()),
                *(ddl.rstrip(";") + ";" for ddl in self._views.values()),
            ]
            return "\n\n".join(statements)

    def deploy(
        self,
        client: t.Any,
        pipeline_name: str,
        workers: int = 4,
        compilation_profile: str = "dev",
        timeout: int = 300,
    ) -> t.Any:
        from feldera.pipeline_builder import PipelineBuilder
        from feldera.rest.pipeline import PipelineStatus
        from feldera.runtime_config import RuntimeConfig

        with self._lock:
            sql = self.assemble_program()
            if not sql.strip():
                return self._pipeline

            runtime_config = RuntimeConfig.default()
            runtime_config.workers = workers

            pipeline = PipelineBuilder(
                client,
                name=pipeline_name,
                sql=sql,
                compilation_profile=compilation_profile,
                runtime_config=runtime_config,
            ).create_or_replace(wait=True)
            pipeline.start()
            pipeline.wait_for_status(PipelineStatus.RUNNING, timeout=timeout)
            self._pipeline = pipeline
            return pipeline


def _extract_name(sql: str) -> str:
    """Very basic name extraction until sqlglot-based parsing is added."""
    import re

    match = re.search(
        r"(?:CREATE|DROP)\s+(?:MATERIALIZED\s+)?(?:TABLE|VIEW)\s+"
        r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(\w+)",
        sql,
        re.IGNORECASE,
    )
    return match.group(1).lower() if match else sql[:40]


class FelderaCursor:
    """DB-API 2.0 cursor backed by Feldera's REST API."""

    def __init__(
        self,
        client: t.Any,
        pipeline_name: str,
        state_manager: PipelineStateManager,
        workers: int = 4,
        compilation_profile: str = "dev",
        timeout: int = 300,
    ) -> None:
        self._client = client
        self._pipeline_name = pipeline_name
        self._state = state_manager
        self._workers = workers
        self._compilation_profile = compilation_profile
        self._timeout = timeout
        self._rows: t.List[t.Dict[str, t.Any]] = []
        self._columns: t.List[str] = []
        self.rowcount = -1
        self.description: t.Optional[t.List] = None

    def execute(self, sql: str, parameters: t.Optional[t.Any] = None) -> None:
        if parameters is not None:
            raise NotImplementedError(
                "Feldera DB-API does not support query parameters"
            )

        intent = _classify(sql)
        logger.debug("Feldera execute (intent=%s): %.200s", intent.value, sql)

        if intent == SqlIntent.NO_OP:
            self._rows = []
            self._columns = []
            return

        if intent == SqlIntent.PIPELINE_DDL:
            self._state.register_ddl(sql)
            self._state.deploy(
                self._client,
                self._pipeline_name,
                self._workers,
                self._compilation_profile,
                self._timeout,
            )
            self._rows = []
            self._columns = []
            return

        if intent == SqlIntent.DATA_INGRESS:
            self._get_pipeline().execute(sql)
            self._rows = []
            self._columns = []
            return

        rows = list(self._get_pipeline().query(sql))
        self._rows = rows
        self._columns = list(rows[0].keys()) if rows else []
        self.rowcount = len(rows)
        self.description = [
            (column, None, None, None, None, None, None)
            for column in self._columns
        ]

    def _get_pipeline(self) -> t.Any:
        from feldera.pipeline import Pipeline

        return Pipeline.get(self._pipeline_name, self._client)

    def fetchone(self) -> t.Optional[t.Tuple]:
        return tuple(self._rows.pop(0).values()) if self._rows else None

    def fetchmany(self, size: int = 1) -> t.List[t.Tuple]:
        batch, self._rows = self._rows[:size], self._rows[size:]
        return [tuple(row.values()) for row in batch]

    def fetchall(self) -> t.List[t.Tuple]:
        rows, self._rows = self._rows, []
        return [tuple(row.values()) for row in rows]

    def fetchdf(self) -> pd.DataFrame:
        import pandas as pd

        rows, self._rows = self._rows, []
        return pd.DataFrame(rows)

    def close(self) -> None:
        self._rows = []


class FelderaConnection:
    """DB-API 2.0 connection wrapper around FelderaClient."""

    def __init__(
        self,
        client: t.Any,
        pipeline_name: str,
        workers: int = 4,
        compilation_profile: str = "dev",
        timeout: int = 300,
    ) -> None:
        self._client = client
        self._pipeline_name = pipeline_name
        self._workers = workers
        self._compilation_profile = compilation_profile
        self._timeout = timeout
        self._state = PipelineStateManager()

    def cursor(self) -> FelderaCursor:
        return FelderaCursor(
            self._client,
            self._pipeline_name,
            self._state,
            self._workers,
            self._compilation_profile,
            self._timeout,
        )

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def connect(
    host: str,
    pipeline_name: str,
    api_key: t.Optional[str] = None,
    timeout: int = 300,
    workers: int = 4,
    compilation_profile: str = "dev",
) -> FelderaConnection:
    from feldera.rest.feldera_client import FelderaClient

    client = FelderaClient(url=host, api_key=api_key, timeout=float(timeout))
    return FelderaConnection(
        client,
        pipeline_name,
        workers,
        compilation_profile,
        timeout,
    )
