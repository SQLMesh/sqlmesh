# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timezone

import pytest

from sqlmesh.core.query_execution import (
    MAX_QUERY_EXECUTION_CONTEXT_BYTES,
    MAX_QUERY_EXECUTION_IDENTIFIER_BYTES,
    ModelExecutionContext,
    QueryExecutionContext,
    QueryExecutionOperation,
    QueryExecutionPhase,
    QueryExecutionRole,
    get_query_execution_context,
    query_execution_scope,
    query_phase_scope,
    serialize_query_execution_context,
)
from sqlmesh.utils import CorrelationId


def _context(
    *, model_name: str = "analytics.orders", evaluation_id: str = "eval-1"
) -> QueryExecutionContext:
    return QueryExecutionContext(
        parent=CorrelationId.from_run_id("run-7"),
        evaluation=ModelExecutionContext(
            evaluation_id=evaluation_id,
            model_name=model_name,
            snapshot_id="snapshot-3",
            interval_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            interval_end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            batch_index=2,
        ),
        phase=QueryExecutionPhase.MATERIALIZE,
        role=QueryExecutionRole.INSERT,
    )


def test_serializer_emits_version_one_context_with_utc_interval():
    context = _context()

    actual = serialize_query_execution_context(context)

    assert actual == (
        '/* QUERY_CONTEXT: {"schema_version":1,"emitter":"sqlmesh",'
        '"parent":{"system":"sqlmesh","type":"run","id":"run-7"},'
        '"workload":{"system":"sqlmesh","type":"model","name":"analytics.orders",'
        '"version":"snapshot-3"},"operation":{"id":"eval-1","type":"evaluate",'
        '"interval_start":"2026-01-01T00:00:00+00:00",'
        '"interval_end":"2026-01-02T00:00:00+00:00","batch_index":2},'
        '"query":{"phase":"materialize","role":"insert"}} */ '
    )


def test_serializer_omits_unavailable_context_fields():
    actual = serialize_query_execution_context(
        QueryExecutionContext(parent=CorrelationId.from_plan_id("plan-1"))
    )

    assert actual == (
        '/* QUERY_CONTEXT: {"schema_version":1,"emitter":"sqlmesh",'
        '"parent":{"system":"sqlmesh","type":"plan","id":"plan-1"},'
        '"query":{"phase":"unknown","role":"query"}} */ '
    )


@pytest.mark.parametrize("name", ['analytics."a/*b"', 'analytics."a*/b"'])
def test_serializer_escapes_sql_comment_delimiters_and_round_trips(name: str):
    import json

    comment = serialize_query_execution_context(_context(model_name=name))
    assert comment is not None
    payload = comment.removeprefix("/* QUERY_CONTEXT: ").removesuffix(" */ ")

    assert "/*" not in payload
    assert "*/" not in payload
    assert json.loads(payload)["workload"]["name"] == name


def test_scope_restores_nested_and_exceptional_contexts():
    outer = _context()
    inner = _context(evaluation_id="eval-2")
    with query_execution_scope(outer):
        assert get_query_execution_context() is outer
        with query_phase_scope(QueryExecutionPhase.AUDIT, QueryExecutionRole.AUDIT):
            audit = get_query_execution_context()
            assert audit is not None
            assert audit.evaluation == outer.evaluation
            assert audit.phase is QueryExecutionPhase.AUDIT
        assert get_query_execution_context() is outer
        with pytest.raises(RuntimeError, match="failed"):
            with query_execution_scope(inner):
                raise RuntimeError("failed")
        assert get_query_execution_context() is outer

    assert get_query_execution_context() is None


def test_serializer_accepts_identifier_at_utf8_byte_limit():
    context = _context(model_name="m" * MAX_QUERY_EXECUTION_IDENTIFIER_BYTES)

    comment = serialize_query_execution_context(context)

    assert comment is not None
    assert len(comment.encode("utf-8")) <= MAX_QUERY_EXECUTION_CONTEXT_BYTES


def test_serializer_omits_identifier_over_utf8_byte_limit(caplog):
    context = _context(model_name="m" * (MAX_QUERY_EXECUTION_IDENTIFIER_BYTES + 1))

    assert serialize_query_execution_context(context) is None

    assert "identifier is 1025 UTF-8 bytes" in caplog.text
    assert "analytics" not in caplog.text


def test_serializer_omits_unencodable_identifier(caplog):
    context = _context(model_name="invalid-\ud800")

    assert serialize_query_execution_context(context) is None

    assert "identifier cannot be encoded as UTF-8" in caplog.text
    assert "invalid-" not in caplog.text


def test_serializer_omits_complete_comment_over_byte_limit(caplog):
    value = "😀" * (MAX_QUERY_EXECUTION_IDENTIFIER_BYTES // 4)
    context = QueryExecutionContext(
        evaluation=ModelExecutionContext(
            evaluation_id=value,
            model_name=value,
            snapshot_id=value,
            operation_type=QueryExecutionOperation.EVALUATE,
        )
    )

    assert serialize_query_execution_context(context) is None

    assert "comment is" in caplog.text
    assert "8192 UTF-8 bytes" in caplog.text
    assert "😀" not in caplog.text


def test_context_rejects_wrong_types_and_empty_identifiers():
    with pytest.raises(ValueError, match="must not be empty"):
        ModelExecutionContext("", "model", "snapshot")
    with pytest.raises(TypeError, match="must be an integer"):
        ModelExecutionContext("eval", "model", "snapshot", batch_index=True)
    with pytest.raises(ValueError, match="fixed at 1"):
        QueryExecutionContext(schema_version=2)
