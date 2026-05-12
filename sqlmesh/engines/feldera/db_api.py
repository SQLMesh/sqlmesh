from __future__ import annotations

import logging
import threading
import typing as t
from enum import Enum
import re

from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

QUERY_MIRROR_PREFIX = "__sqlmesh_query__"
FELDERA_DIALECT = "feldera"

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
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
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


def _is_virtual_layer_ddl(sql: str) -> bool:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        return False

    if not isinstance(expression, (exp.Create, exp.Drop)):
        return False

    target = expression.this
    if isinstance(target, exp.Schema):
        target = target.this

    if not isinstance(target, exp.Table) or not target.db:
        return False

    target_db = target.db.lower()
    if target_db.startswith("sqlmesh__"):
        return False

    referenced_snapshot_tables = [
        table
        for table in expression.find_all(exp.Table)
        if table is not target and table.db and table.db.lower().startswith("sqlmesh__")
    ]
    return bool(referenced_snapshot_tables)


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
            object_key = _extract_name(sql)
            expression = None

            try:
                expression = parse_one(_strip_leading_comments(sql), dialect=FELDERA_DIALECT)
            except (ParseError, ValueError):
                pass

            self._hydrated_object_keys.discard(object_key)

            if isinstance(expression, exp.Create):
                kind = str(expression.args.get("kind") or "").upper()
                if "TABLE" in kind:
                    sql = _rewrite_table_ctas_sql(sql)
                    self._dropped_objects.discard(object_key)
                    self._views.pop(object_key, None)
                    self._tables[object_key] = sql
                    self._dirty = True
                elif "VIEW" in kind:
                    self._dropped_objects.discard(object_key)
                    self._tables.pop(object_key, None)
                    self._views[object_key] = sql
                    self._dirty = True
                return

            if isinstance(expression, exp.Drop):
                kind = str(expression.args.get("kind") or "").upper()
                if "TABLE" in kind:
                    self._tables.pop(object_key, None)
                    self._dropped_objects.add(object_key)
                    self._dirty = True
                elif "VIEW" in kind:
                    self._views.pop(object_key, None)
                    self._dropped_objects.add(object_key)
                    self._dirty = True
                return

            upper = sql.strip().upper()
            if "CREATE TABLE" in upper or "CREATE MATERIALIZED TABLE" in upper:
                sql = _rewrite_table_ctas_sql(sql)
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

    def is_materialized_view(self, object_name: str) -> bool:
        with self._lock:
            return _is_materialized_view_sql(self._views.get(object_name.lower(), ""))

    def pending_drops(self) -> t.Set[str]:
        with self._lock:
            return set(self._dropped_objects)

    def current_pipeline(self) -> t.Any:
        with self._lock:
            return self._pipeline

    def queryable_relation_names(self) -> t.Set[str]:
        with self._lock:
            return {
                *self._tables,
                *(name for name, sql in self._views.items() if not _is_materialized_view_sql(sql)),
            }

    def assemble_program(self) -> str:
        with self._lock:
            statements = [
                *(
                    statement
                    for ddl in self._tables.values()
                    for statement in _ddl_statements_with_query_mirror(ddl)
                ),
                *(
                    statement
                    for ddl in self._views.values()
                    for statement in _ddl_statements_with_query_mirror(ddl)
                ),
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

            while True:
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
                        InnerPipeline,
                        FelderaAPIError,
                    )
                    break
                except RuntimeError as ex:
                    if not self._evict_hydrated_objects(str(ex)):
                        raise

                    sql = self.assemble_program()
                    if not sql.strip():
                        self._dirty = False
                        self._dropped_objects.clear()
                        return self._pipeline

            pipeline.start(timeout_s=timeout)
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

        for expression in parse(program_code, dialect=FELDERA_DIALECT):
            if expression is None:
                continue

            sql = expression.sql(dialect=FELDERA_DIALECT)

            if isinstance(expression, exp.Create):
                target = expression.this
                if isinstance(target, exp.Schema):
                    target = target.this

                if isinstance(target, exp.Table) and _is_query_mirror_name(target.name):
                    continue

                object_key = _extract_name(sql)
                kind = str(expression.args.get("kind") or "").upper()
                if "VIEW" in kind:
                    self._tables.pop(object_key, None)
                    self._views[object_key] = sql
                elif "TABLE" in kind:
                    sql = _rewrite_table_ctas_sql(sql)
                    self._views.pop(object_key, None)
                    self._tables[object_key] = sql
                self._hydrated_object_keys.add(object_key)

    def _evict_hydrated_objects(self, error_message: str) -> bool:
        if not self._hydrated_object_keys:
            return False

        normalized_error = error_message.lower()
        object_keys = [
            object_key
            for object_key in self._hydrated_object_keys
            if object_key in normalized_error
        ]

        if not object_keys:
            if "not found" not in normalized_error:
                return False
            object_keys = list(self._hydrated_object_keys)

        for object_key in object_keys:
            self._tables.pop(object_key, None)
            self._views.pop(object_key, None)
            self._hydrated_object_keys.discard(object_key)

        return True

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
        InnerPipeline: t.Any,
        FelderaAPIError: t.Any,
    ) -> t.Any:
        try:
            existing_pipeline = Pipeline.get(pipeline_name, client)
        except FelderaAPIError:
            existing_pipeline = None

        if existing_pipeline is None:
            try:
                return PipelineBuilder(
                    client,
                    name=pipeline_name,
                    sql=sql,
                    compilation_profile=profile,
                    runtime_config=runtime_config,
                ).create_or_replace(wait=True)
            except RuntimeError as ex:
                raise self._format_compile_error(client, pipeline_name, ex) from ex

        existing_pipeline.stop(force=True, timeout_s=timeout)
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
        try:
            inner_pipeline = client.create_or_update_pipeline(inner_pipeline, wait=True)
        except RuntimeError as ex:
            raise self._format_compile_error(client, pipeline_name, ex) from ex
        pipeline = Pipeline(client)
        pipeline._inner = inner_pipeline
        return pipeline

    def _format_compile_error(
        self, client: t.Any, pipeline_name: str, error: Exception
    ) -> RuntimeError:
        error_message = str(error)
        from feldera.enums import PipelineFieldSelector

        try:
            pipeline = client.get_pipeline(pipeline_name, PipelineFieldSelector.ALL)
        except Exception:
            return RuntimeError(error_message)

        program_error = getattr(pipeline, "program_error", None) or {}
        sql_compilation = program_error.get("sql_compilation") or {}
        sql_messages = sql_compilation.get("messages") or []

        if sql_messages:
            details = self._sql_compilation_error_message(pipeline_name, sql_messages)
            if details != error_message:
                return RuntimeError(details)

        rust_error = program_error.get("rust_compilation")
        system_error = program_error.get("system_error")
        if rust_error or system_error:
            message = (
                f"The program failed to compile: {getattr(pipeline, 'program_status', 'unknown')}\n"
            )
            if rust_error is not None:
                message += f"Rust Error: {rust_error}\n"
            if system_error is not None:
                message += f"System Error: {system_error}"
            return RuntimeError(message.rstrip())

        return RuntimeError(error_message)

    @staticmethod
    def _sql_compilation_error_message(
        pipeline_name: str, sql_errors: t.Sequence[t.Mapping[str, t.Any]]
    ) -> str:
        err_msg = f"Pipeline {pipeline_name} failed to compile:\n"
        for sql_error in sql_errors:
            err_msg += f"{sql_error['error_type']}\n{sql_error['message']}\n"
            err_msg += f"Code snippet:\n{sql_error['snippet']}"
        return err_msg


def _extract_name(sql: str) -> str:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
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


def _is_query_mirror_name(name: str) -> bool:
    return name.lower().startswith(QUERY_MIRROR_PREFIX)


def _query_mirror_name(name: str) -> str:
    return f"{QUERY_MIRROR_PREFIX}{name}"


def _query_mirror_table(table: exp.Table) -> exp.Table:
    mirror = table.copy()
    mirror.set("this", exp.to_identifier(_query_mirror_name(table.name), quoted=True))
    return mirror


def _ddl_statements_with_query_mirror(sql: str) -> t.List[str]:
    try:
        expressions = [
            expression
            for expression in parse(sql, dialect=FELDERA_DIALECT)
            if expression is not None
        ]
    except (ParseError, ValueError):
        expressions = []

    if not expressions:
        statement = sql.rstrip(";") + ";"
        mirror_sql = _query_mirror_sql(sql)
        return [statement, *([mirror_sql.rstrip(";") + ";"] if mirror_sql else [])]

    statements = []
    for expression in expressions:
        statement = expression.sql(dialect=FELDERA_DIALECT).rstrip(";") + ";"
        statements.append(statement)
        mirror_sql = _query_mirror_sql(statement)
        if mirror_sql:
            statements.append(mirror_sql.rstrip(";") + ";")

    return statements


def _query_mirror_sql(sql: str) -> t.Optional[str]:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        return None

    if not isinstance(expression, exp.Create):
        return None

    target = expression.this
    if isinstance(target, exp.Schema):
        target = target.this

    if not isinstance(target, exp.Table) or _is_query_mirror_name(target.name):
        return None
    if target.db and target.db.lower().startswith("sqlmesh__"):
        return None

    kind = str(expression.args.get("kind") or "").upper()
    if "TABLE" not in kind and "VIEW" not in kind:
        return None
    if "VIEW" in kind and _is_materialized_view_sql(sql):
        return None

    mirror_sql = exp.Create(
        this=_query_mirror_table(target),
        kind="MATERIALIZED VIEW",
        expression=exp.select("*").from_(target.copy()),
    ).sql(dialect=FELDERA_DIALECT)

    return _strip_table_qualifiers(mirror_sql)


def _rewrite_query_for_query_mirrors(sql: str, relation_names: t.Set[str]) -> str:
    if not relation_names:
        return sql

    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        return sql

    cte_names = {
        cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE) if cte.alias_or_name
    }

    def transform(node: exp.Expression) -> exp.Expression:
        if (
            isinstance(node, exp.Table)
            and not _is_query_mirror_name(node.name)
            and node.name.lower() in relation_names
            and node.name.lower() not in cte_names
        ):
            return _query_mirror_table(node)
        return node

    return expression.transform(transform, copy=True).sql(dialect=FELDERA_DIALECT)


def _execution_error(rows: t.Sequence[t.Mapping[str, t.Any]]) -> t.Optional[str]:
    for row in rows:
        for value in row.values():
            if isinstance(value, str) and value.startswith("Execution error:"):
                return value

    return None


def _is_materialized_view_sql(sql: str) -> bool:
    stripped = _strip_leading_comments(sql)
    upper = stripped.upper()

    if "CREATE MATERIALIZED VIEW" in upper:
        return True
    if "CREATE VIEW" in upper:
        return False

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        expression = None

    if isinstance(expression, exp.Create):
        kind = str(expression.args.get("kind") or "").upper()
        return "MATERIALIZED" in kind and "VIEW" in kind

    return False


def _rewrite_table_ctas_sql(sql: str) -> str:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        return sql

    if not isinstance(expression, exp.Create):
        return sql

    kind = str(expression.args.get("kind") or "").upper()
    query = expression.expression
    target = expression.this

    if "TABLE" not in kind or query is None or not isinstance(target, exp.Table):
        return sql

    columns_to_types = _select_columns_to_types(query)
    if not columns_to_types:
        return sql

    schema = exp.Schema(
        this=target.copy(),
        expressions=[
            exp.ColumnDef(this=exp.to_identifier(column, quoted=True), kind=data_type.copy())
            for column, data_type in columns_to_types.items()
        ],
    )
    create_exp = exp.Create(
        this=schema,
        kind=expression.args.get("kind") or "TABLE",
        replace=bool(expression.args.get("replace")),
        exists=bool(expression.args.get("exists")),
        properties=expression.args.get("properties"),
    )
    insert_exp = exp.insert(query.copy(), target.copy(), columns=list(columns_to_types))
    return f"{create_exp.sql(dialect=FELDERA_DIALECT)};\n{insert_exp.sql(dialect=FELDERA_DIALECT)}"


def _select_columns_to_types(query: exp.Expression) -> t.Optional[t.Dict[str, exp.DataType]]:
    if not isinstance(query, exp.Query):
        return None

    columns_to_types: t.Dict[str, exp.DataType] = {}
    unknown = exp.DataType.build("unknown")

    for select in query.selects:
        output_name = select.output_name
        data_type = (
            _projection_type(t.cast(exp.Expression, select)) or (select.type or unknown).copy()
        )

        if not output_name or output_name in columns_to_types or data_type == unknown:
            return None

        columns_to_types[output_name] = data_type

    return columns_to_types or None


def _projection_type(select: exp.Expression) -> t.Optional[exp.DataType]:
    expression = select
    if isinstance(select, exp.Alias):
        expression = select.this

    if isinstance(expression, exp.Cast) and isinstance(expression.args.get("to"), exp.DataType):
        return expression.args["to"].copy()

    return None


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
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        return stripped

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table):
            node = _canonicalize_snapshot_table(node)
            node = _unqualify_table(node)
        return node

    expression = expression.transform(transform, copy=True)
    return expression.sql(dialect=FELDERA_DIALECT)


def _snapshot_name_parts(name: str) -> t.Optional[t.Tuple[str, str]]:
    parts = name.split("__")
    if len(parts) < 3:
        return None

    if parts[-1].isdigit():
        schema_name = parts[0]
        model_name = "__".join(parts[1:-1])
        return (schema_name, model_name) if model_name else None

    if len(parts) >= 4 and parts[-2].isdigit():
        schema_name = parts[0]
        model_name = "__".join(parts[1:-2])
        return (schema_name, model_name) if model_name else None

    return None


def _canonicalize_snapshot_table(node: exp.Table) -> exp.Table:
    if _is_query_mirror_name(node.name):
        return node

    snapshot_parts = _snapshot_name_parts(node.name)
    if snapshot_parts is None:
        return node

    _, model_name = snapshot_parts
    canonicalized = node.copy()
    canonicalized.set("this", exp.to_identifier(model_name, quoted=True))
    canonicalized.set("db", None)
    canonicalized.set("catalog", None)
    return canonicalized


def _normalize_pipeline_ddl(sql: str) -> str:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        return stripped

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table):
            node = _canonicalize_snapshot_table(node)
            node = _unqualify_table(node)
        return node

    return expression.transform(transform, copy=True).sql(dialect=FELDERA_DIALECT)


def _insert_to_input_json_payload(
    sql: str,
) -> t.Optional[t.Tuple[str, t.List[t.Dict[str, t.Any]]]]:
    stripped = _strip_leading_comments(sql)

    try:
        expression = parse_one(stripped, dialect=FELDERA_DIALECT)
    except (ParseError, ValueError):
        return None

    if not isinstance(expression, exp.Insert):
        return None

    target = expression.this
    if isinstance(target, exp.Schema):
        table = target.this
        target_columns = [column.name for column in target.expressions]
    elif isinstance(target, exp.Table):
        table = target
        target_columns = []
    else:
        return None

    if not isinstance(table, exp.Table):
        return None

    table = _canonicalize_snapshot_table(table)

    query = expression.expression
    if not isinstance(query, exp.Query):
        return None

    values = _query_values_source(query)
    if values is None:
        return None

    alias = values.args.get("alias")
    if alias is None:
        return None

    source_columns = [column.name for column in alias.columns]
    if not source_columns:
        return None

    if not target_columns:
        target_columns = [select.output_name for select in query.selects]

    if len(target_columns) != len(query.selects):
        return None

    rows = []
    for row in values.expressions:
        if not isinstance(row, exp.Tuple):
            return None

        source_row = {
            column: _literal_value(value) for column, value in zip(source_columns, row.expressions)
        }
        payload_row: t.Dict[str, t.Any] = {}
        for target_column, select in zip(target_columns, query.selects):
            if not target_column:
                return None

            evaluated: t.Any = _evaluate_insert_value_expression(
                t.cast(exp.Expression, select), source_row
            )
            if evaluated is _UNSUPPORTED_INGEST_EXPRESSION:
                return None
            payload_row[target_column] = evaluated
        rows.append(payload_row)

    return table.name, rows


def _query_values_source(query: exp.Query) -> t.Optional[exp.Values]:
    from_expression = query.args.get("from_")
    if from_expression is None:
        return None

    source = from_expression.this
    if isinstance(source, exp.Values):
        return source
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Query):
        return _query_values_source(source.this)
    return None


_UNSUPPORTED_INGEST_EXPRESSION = object()


def _evaluate_insert_value_expression(
    expression: exp.Expression, source_row: t.Mapping[str, t.Any]
) -> t.Any:
    if isinstance(expression, exp.Alias):
        return _evaluate_insert_value_expression(expression.this, source_row)

    if isinstance(expression, exp.Cast):
        value = _evaluate_insert_value_expression(expression.this, source_row)
        if value is _UNSUPPORTED_INGEST_EXPRESSION:
            return value
        to_type = expression.args.get("to")
        if isinstance(to_type, exp.DataType):
            return _coerce_input_json_value(value, to_type)
        return value

    if isinstance(expression, exp.Column):
        return source_row.get(expression.name)

    if isinstance(expression, (exp.Literal, exp.Boolean, exp.Null)):
        return _literal_value(expression)

    if isinstance(expression, exp.Paren):
        return _evaluate_insert_value_expression(expression.this, source_row)

    if isinstance(expression, exp.Neg):
        value = _evaluate_insert_value_expression(expression.this, source_row)
        if isinstance(value, (int, float)):
            return -value
        return _UNSUPPORTED_INGEST_EXPRESSION

    return _UNSUPPORTED_INGEST_EXPRESSION


def _literal_value(expression: exp.Expression) -> t.Any:
    if isinstance(expression, (exp.Literal, exp.Boolean, exp.Null)):
        return expression.to_py()
    return _UNSUPPORTED_INGEST_EXPRESSION


def _coerce_input_json_value(value: t.Any, data_type: exp.DataType) -> t.Any:
    if value is None:
        return None

    dtype = data_type.this
    if dtype in {
        exp.DataType.Type.TINYINT,
        exp.DataType.Type.SMALLINT,
        exp.DataType.Type.INT,
        exp.DataType.Type.BIGINT,
    }:
        return int(value)

    if dtype in {
        exp.DataType.Type.FLOAT,
        exp.DataType.Type.DOUBLE,
        exp.DataType.Type.DECIMAL,
    }:
        return float(value)

    if dtype == exp.DataType.Type.BOOLEAN:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "t", "1"}:
                return True
            if normalized in {"false", "f", "0"}:
                return False
        return bool(value)

    if dtype in {
        exp.DataType.Type.CHAR,
        exp.DataType.Type.NCHAR,
        exp.DataType.Type.TEXT,
        exp.DataType.Type.VARCHAR,
        exp.DataType.Type.NVARCHAR,
        exp.DataType.Type.DATE,
        exp.DataType.Type.TIME,
        exp.DataType.Type.TIMESTAMP,
        exp.DataType.Type.TIMESTAMPTZ,
    }:
        return str(value)

    return value


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
        state_manager: t.Any,
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
            raise NotImplementedError("Feldera DB-API does not support query parameters")

        original_sql = sql
        normalized_sql = _normalize_ddl(sql)
        intent = _classify(normalized_sql)
        sql = (
            _normalize_pipeline_ddl(normalized_sql)
            if intent == SqlIntent.PIPELINE_DDL
            else _strip_table_qualifiers(normalized_sql)
        )
        logger.debug("Feldera execute (intent=%s): %.200s", intent.value, sql)

        if intent == SqlIntent.NO_OP:
            self._rows = []
            self._columns = []
            return

        if intent == SqlIntent.PIPELINE_DDL:
            if _is_virtual_layer_ddl(original_sql):
                self._rows = []
                self._columns = []
                return
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
            pipeline = self._get_pipeline()
            payload = _insert_to_input_json_payload(sql)
            if payload is not None:
                table_name, rows = payload
                pipeline.input_json(table_name, rows)
            else:
                pipeline.execute(sql)
            self._rows = []
            self._columns = []
            return

        query_sql = _rewrite_query_for_query_mirrors(sql, self._state.queryable_relation_names())
        rows = list(self._get_pipeline().query(query_sql))
        if error := _execution_error(rows):
            raise RuntimeError(error)
        self._rows = rows
        self._columns = list(rows[0].keys()) if rows else []
        self.rowcount = len(rows)
        self.description = [
            (column, None, None, None, None, None, None) for column in self._columns
        ]

    def _get_pipeline(self) -> t.Any:
        pipeline = self._state.current_pipeline()
        if pipeline is not None:
            return pipeline

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
            self._state: t.Any = self._shared_states.setdefault(state_key, PipelineStateManager())

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
            try:
                self._state.deploy(
                    self._client,
                    self._pipeline_name,
                    self._workers,
                    self._compilation_profile,
                    self._timeout,
                )
            except Exception as ex:
                logger.error(
                    "Feldera pending DDL failed during connection close for pipeline %s:\n%s",
                    self._pipeline_name,
                    ex,
                )
                raise
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
