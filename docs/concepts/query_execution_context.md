# Query execution context

SQLMesh can add structured metadata to warehouse statements so a query-history
tool can connect a statement to the plan or run, model, and evaluation that issued
it. The metadata is a versioned JSON object in a `QUERY_CONTEXT` SQL comment.

Set `query_execution_context` to `true` on a connection to enable bounded query
context comments on supported adapters. The flag defaults to `false`. With this
flag enabled, `Context.apply()` and built-in scheduler runs add model context to
SQL on supported adapters without a custom provider. For YAML configuration,
set the option under the gateway's `connection`:

```yaml
gateways:
  local:
    connection:
      type: duckdb
      query_execution_context: true
```

## Automatic model context

For each model batch, SQLMesh creates a context with a new operation ID, the
model's canonical name, an opaque snapshot ID, and the batch interval and index
when available. Intervals use UTC and an exclusive end.

The context stays active for SQL inside the batch, including nested audits.
Automatic context uses the broad phases `materialize`, `audit`, and `ddl`. The
`role` field does not identify the engine operation for each SQL statement.

Outside a model batch, SQLMesh can add parent metadata when a plan or run
correlation ID is available. Use a custom provider when you need caller-defined
context for other statements.

## Read the context from an observer

SQLMesh establishes the model context before it calls an observer's `start`
callback. It restores the context after the `finish` callback. Both callbacks can
read the active immutable value with `get_query_execution_context()`:

```python
from sqlmesh.core.query_execution import get_query_execution_context


def observe(event, facts, error):
    context = get_query_execution_context()
    if context is not None and context.evaluation is not None:
        print(context.evaluation.evaluation_id)
```

For console observer registration and callback behavior, see
[Execution observations](../reference/execution_observations.md).

SQLMesh forwards each observer event to the configured console observer once. If
the console has no observer, model context still works. Nested observer scopes
restore the outer observer and context on exit. Use `observer_scope(None)` to
disable the built-in observer for its block.

An observer callback can read the active context with
`get_query_execution_context()`. SQL issued inside the callback does not use that
automatic context. A custom adapter provider remains authoritative. Without one,
an active plan or run correlation ID can still produce parent-only metadata.

## Configure a custom provider

Use a custom provider when you need caller-defined operation IDs or context
outside a model batch. Set it on warehouse adapters before loading a Python
`Context`. When `Context` creates its snapshot evaluator, it clones those
adapters and preserves the provider and flag.

SQLMesh calls the provider after rendering each statement and before executing
it. The provider runs in the current worker's context, so concurrent workers can
return different values for a shared adapter.

```python
from sqlmesh.core.context import Context
from sqlmesh.core.execution_observation import observer_scope
from examples.query_execution_context.observer import QueryContextObserver

context = Context(paths=".", load=False)
observer = QueryContextObserver(context.console.get_execution_observer())

for adapter in context.engine_adapters.values():
    adapter.query_execution_context = True
    adapter.query_execution_context_provider = observer.current_query_context

context.load()
with observer_scope(observer):
    plan = context.plan("dev", no_prompts=True)
    context.apply(plan)
    context.run("dev")
```

`QueryContextObserver` returns the immutable context active in its worker, or
`None` when no context applies. It assigns an evaluation ID when SQLMesh reports
a model batch and restores the previous context when that event finishes. It
forwards the same event, facts, and error to the observer returned by the console.

A custom provider replaces the built-in provider for its adapter. If it returns
`None`, SQLMesh does not use automatic model context for that statement. SQLMesh
can still add parent-only metadata when the adapter has a correlation ID.

Adapters created with `with_settings` inherit the flag and provider. Pass
`query_execution_context_provider=None` to clear the inherited provider. Setting
the flag to `false` disables provider calls and comment output.

When `state_connection` is omitted, the built-in scheduler uses the warehouse
connection for its state adapter and disables query context on that adapter. An
explicitly configured `state_connection` uses its own `query_execution_context`
setting. The automatic observer activates only if a supported warehouse adapter
has `query_execution_context` enabled and no custom provider. An enabled state
adapter without a custom provider can use that context during a model batch.
Setting the flag only on `state_connection` does not activate the observer. Use a
custom provider for state SQL outside a model batch.

## Read the payload

The version-one payload uses the SQLMesh model's canonical name and opaque
snapshot identifier. Operation intervals use UTC half-open boundaries, and the
payload includes `batch_index` when available. Fields that are unavailable are
omitted.

```json
{
  "schema_version": 1,
  "emitter": "sqlmesh",
  "parent": {"system": "sqlmesh", "type": "run", "id": "run-123"},
  "workload": {
    "system": "sqlmesh",
    "type": "model",
    "name": "analytics.orders",
    "version": "snapshot-456"
  },
  "operation": {
    "id": "evaluation-789",
    "type": "evaluate",
    "interval_start": "2026-10-01T00:00:00+00:00",
    "interval_end": "2026-10-02T00:00:00+00:00",
    "batch_index": 0
  },
  "query": {"phase": "materialize", "role": "query"}
}
```

Each model batch gets one operation ID, which nested audits reuse. The built-in
provider creates this ID. A custom provider supplies its own. The adapter preserves
the provider's ID when it adds the context comment.

The adapter's correlation ID sets `parent` and replaces any parent supplied by
the provider. If the adapter has no correlation ID, SQLMesh clears the provider's
parent.

Consumers must accept absent fields and unknown future phase or role values.
The `workload` identity is the tuple `(system, type, name)`. Its `version` is
opaque and does not change that identity.

## SQL comment behavior

SQLMesh adds the context comment after rendering. If a plan or run marker is
present, SQLMesh keeps that marker first. The submitted statement may look like
this:

```sql
/* SQLMESH_RUN: run-123 */ /* QUERY_CONTEXT: {"schema_version":1,"emitter":"sqlmesh","parent":{"system":"sqlmesh","type":"run","id":"run-123"},"query":{"phase":"unknown","role":"query"}} */ SELECT 1
```

The comment is at most 8,192 UTF-8 bytes. Each identifier is at most 1,024 bytes
before JSON escaping. SQLMesh JSON-escapes metadata and encodes `/` as `\u002f`,
so values cannot end or nest the SQL comment.

If an identifier cannot be encoded or a size limit is exceeded, SQLMesh omits the
complete `QUERY_CONTEXT` comment and logs a warning. It does not truncate the JSON
or fail the query because context was omitted.

SQLMesh adds the comment after rendering. Model definitions and fingerprints
exclude it. Query-history systems that hash submitted SQL must account for the
added comment.

Adapters that do not support generic SQL comments leave SQL unchanged and skip
the provider. SQLMesh logs one warning per adapter when the option is enabled.
Athena does not support this transport. Engines can override the adapter hook to
add native metadata.

If the provider raises an ordinary exception, returns an invalid value, or
produces a context that cannot be serialized, SQLMesh omits the context comment
and logs a bounded warning without provider data. It continues with the
correlation marker if one is available. SQLMesh calls the provider again for the
next statement. On transaction-capable adapters, a provider or driver
`BaseException` rolls back any transaction SQLMesh opened for the statement and
propagates the original exception.
