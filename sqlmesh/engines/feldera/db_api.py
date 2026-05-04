from __future__ import annotations

import logging
import threading
import typing as t
from enum import Enum
import re

from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError

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
    stripped = _strip_leading_comments(sql)
    if not stripped:
        return SqlIntent.NO_OP

    try:
        expression = parse_one(stripped)
    except (ParseError, ValueError):
        expression = None

    if isinstance(expression, (exp.Create, exp.Drop, exp.Alter)):
        return SqlIntent.PIPELINE_DDL
    if isinstance(expression, exp.Insert):
        return SqlIntent.DATA_INGRESS

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
        self._dropped_objects: t.Set[str] = set()
        self._pipeline = None
        self._dirty = False
        self._hydrated = False
        self._hydrated_object_keys: t.Set[str] = set()

    def register_ddl(self, sql: str) -> None:
        with self._lock:
            upper = sql.strip().upper()
            object_key = _extract_name(sql)
            self._hydrated_object_keys.discard(object_key)
            if "CREATE TABLE" in upper or "CREATE MATERIALIZED TABLE" in upper:
                self._dropped_objects.discard(object_key)
                self._views.pop(object_key, None)
                self._tables[object_key] = sql
                self._dirty = True
            elif "CREATE VIEW" in upper or "CREATE MATERIALIZED VIEW" in upper:
                self._dropped_objects.discard(object_key)
                self._tables.pop(object_key, None)
                self._views[object_key] = sql
                self._dirty = True
            elif "DROP TABLE" in upper:
                self._tables.pop(object_key, None)
                self._dropped_objects.add(object_key)
                self._dirty = True
            elif "DROP VIEW" in upper:
                self._views.pop(object_key, None)
                self._dropped_objects.add(object_key)
                self._dirty = True

    def has_pending_changes(self) -> bool:
        with self._lock:
            return self._dirty

    def pending_tables(self) -> t.Set[str]:
        with self._lock:
            return set(self._tables)

    def pending_views(self) -> t.Set[str]:
        with self._lock:
            return set(self._views)

    def pending_drops(self) -> t.Set[str]:
        with self._lock:
            return set(self._dropped_objects)

    def current_pipeline(self) -> t.Any:
        with self._lock:
            return self._pipeline

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
        from feldera.enums import CompilationProfile
        from feldera.pipeline import Pipeline
        from feldera.pipeline_builder import PipelineBuilder
        from feldera.rest.errors import FelderaAPIError
        from feldera.rest.pipeline import Pipeline as InnerPipeline
        from feldera.runtime_config import RuntimeConfig

        try:
            from feldera.pipeline import PipelineStatus
        except ImportError:
            from feldera.rest.pipeline import PipelineStatus

        with self._lock:
            self._hydrate_existing_program(client, pipeline_name)
            if not self._dirty:
                return self._pipeline

            sql = self.assemble_program()
            if not sql.strip():
                self._dirty = False
                self._dropped_objects.clear()
                return self._pipeline

            runtime_config = RuntimeConfig.default()
            runtime_config.workers = workers

            profile = compilation_profile
            if isinstance(profile, str):
                profile = CompilationProfile(profile)

            try:
                pipeline = self._compile_program(
                    client,
                    pipeline_name,
                    sql,
                    profile,
                    runtime_config,
                    timeout,
                    Pipeline,
                    PipelineBuilder,
                    PipelineStatus,
                    InnerPipeline,
                    FelderaAPIError,
                )
            except RuntimeError as ex:
                if "not found" not in str(ex).lower() or not self._hydrated_object_keys:
                    raise

                for object_key in list(self._hydrated_object_keys):
                    self._tables.pop(object_key, None)
                    self._views.pop(object_key, None)
                self._hydrated_object_keys.clear()

                sql = self.assemble_program()
                pipeline = self._compile_program(
                    client,
                    pipeline_name,
                    sql,
                    profile,
                    runtime_config,
                    timeout,
                    Pipeline,
                    PipelineBuilder,
                    PipelineStatus,
                    InnerPipeline,
                    FelderaAPIError,
                )

            pipeline.start()
            pipeline.wait_for_status(PipelineStatus.RUNNING, timeout=timeout)
            self._pipeline = pipeline
            self._dirty = False
            self._dropped_objects.clear()
            return pipeline

    def _hydrate_existing_program(self, client: t.Any, pipeline_name: str) -> None:
        if self._hydrated:
            return

        self._hydrated = True

        try:
            from feldera.pipeline import Pipeline

            pipeline = Pipeline.get(pipeline_name, client)
            program_code = getattr(getattr(pipeline, "_inner", None), "program_code", "") or ""
        except Exception:
            return

        for expression in parse(program_code):
            if expression is None:
                continue

            sql = expression.sql()
            object_key = _extract_name(sql)

            if isinstance(expression, exp.Create):
                kind = str(expression.args.get("kind") or "").upper()
                if "VIEW" in kind:
                    self._tables.pop(object_key, None)
                    self._views[object_key] = sql
                elif "TABLE" in kind:
                    self._views.pop(object_key, None)
                    self._tables[object_key] = sql
                self._hydrated_object_keys.add(object_key)

    def _compile_program(
        self,
        client: t.Any,
        pipeline_name: str,
        sql: str,
        profile: t.Any,
        runtime_config: t.Any,
        timeout: int,
        Pipeline: t.Any,
        PipelineBuilder: t.Any,
        PipelineStatus: t.Any,
        InnerPipeline: t.Any,
        FelderaAPIError: t.Any,
    ) -> t.Any:
        try:
            existing_pipeline = Pipeline.get(pipeline_name, client)
        except FelderaAPIError:
            existing_pipeline = None

        if existing_pipeline is None:
            return PipelineBuilder(
                client,
                name=pipeline_name,
                sql=sql,
                compilation_profile=profile,
                runtime_config=runtime_config,
            ).create_or_replace(wait=True)

        existing_pipeline.stop(force=True)
        existing_pipeline.wait_for_status(PipelineStatus.STOPPED, timeout=timeout)
        existing_pipeline.dismiss_error()

        inner_pipeline = InnerPipeline(
            name=pipeline_name,
            sql=sql,
            udf_rust="",
            udf_toml="",
            description="",
            program_config={
                "profile": profile.value,
                "runtime_version": None,
            },
            runtime_config=runtime_config.to_dict(),
        )
        inner_pipeline = client.create_or_update_pipeline(inner_pipeline, wait=True)
        pipeline = Pipeline(client)
        pipeline._inner = inner_pipeline
        return pipeline


def _extract_name(sql: str) -> str:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped)
    except (ParseError, ValueError):
        return stripped[:80].lower()

    target = expression.this if isinstance(expression, (exp.Create, exp.Drop)) else None
    if isinstance(target, exp.Schema):
        target = target.this

    if isinstance(target, exp.Table):
        return target.name.lower()

    return stripped[:80].lower()


def _normalize_ddl(sql: str) -> str:
    return re.sub(
        r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\b",
        "CREATE TABLE",
        sql,
        flags=re.IGNORECASE,
    )


def _strip_leading_comments(sql: str) -> str:
    stripped = sql.lstrip()
    while stripped.startswith("/*"):
        comment_end = stripped.find("*/")
        if comment_end == -1:
            return stripped
        stripped = stripped[comment_end + 2 :].lstrip()
    return stripped


def _strip_table_qualifiers(sql: str) -> str:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped)
    except (ParseError, ValueError):
        return stripped

    expression = expression.transform(_unqualify_table)
    return expression.sql()


def _unqualify_table(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Table):
        node = node.copy()
        node.set("db", None)
        node.set("catalog", None)
    return node


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

        sql = _strip_table_qualifiers(sql)
        sql = _normalize_ddl(sql)

        intent = _classify(sql)
        logger.debug("Feldera execute (intent=%s): %.200s", intent.value, sql)

        if intent == SqlIntent.NO_OP:
            self._rows = []
            self._columns = []
            return

        if intent == SqlIntent.PIPELINE_DDL:
            self._state.register_ddl(sql)
            self._rows = []
            self._columns = []
            return

        if self._state.has_pending_changes():
            self._state.deploy(
                self._client,
                self._pipeline_name,
                self._workers,
                self._compilation_profile,
                self._timeout,
            )

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

        pipeline = self._state.current_pipeline()
        if pipeline is not None:
            return pipeline

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

    _state_lock = threading.Lock()
    _shared_states: t.Dict[t.Tuple[str, str], PipelineStateManager] = {}

    def __init__(
        self,
        client: t.Any,
        host: str,
        pipeline_name: str,
        workers: int = 4,
        compilation_profile: str = "dev",
        timeout: int = 300,
    ) -> None:
        self._client = client
        self._host = host
        self._pipeline_name = pipeline_name
        self._workers = workers
        self._compilation_profile = compilation_profile
        self._timeout = timeout
        state_key = (host, pipeline_name)
        with self._state_lock:
            self._state = self._shared_states.setdefault(state_key, PipelineStateManager())

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
        if self._state.has_pending_changes():
            self._state.deploy(
                self._client,
                self._pipeline_name,
                self._workers,
                self._compilation_profile,
                self._timeout,
            )
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
        host,
        pipeline_name,
        workers,
        compilation_profile,
        timeout,
    )
