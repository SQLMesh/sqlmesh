# Query execution context

SQLMesh can add structured metadata to warehouse statements so a query-history
tool can connect a statement to the plan or run, model, and evaluation that issued
it. The metadata is a versioned JSON object in a `QUERY_CONTEXT` SQL comment.

Enable `query_execution_context` on a connection and set a provider on its
engine adapter. The provider returns a `QueryExecutionContext`. The flag defaults
to `false`. For YAML configuration, set the option under the gateway's
`connection`:

```yaml
gateways:
  local:
    connection:
      type: duckdb
      query_execution_context: true
```

The flag enables context comments but does not infer model context. Without a
provider, SQLMesh adds parent metadata only when a plan or run correlation ID is
available. SQLMesh calls the provider after rendering each statement and before
executing it. The call runs in the current worker's context, so concurrent model
evaluations can return different IDs for a shared adapter.

To build a provider from native observer events, see the
[query execution context example](../examples/query_execution_context.md).

## Configure a provider

Set the provider on warehouse adapters before loading a Python `Context`. When
`Context` creates its snapshot evaluator, it clones those adapters and preserves
the provider and flag.

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
a model batch and restores the previous context when that event finishes. It also
forwards the same event, facts, and error to the observer returned by the console.

Adapters created with `with_settings` inherit the flag and provider. Pass
`query_execution_context_provider=None` to clear the inherited provider. Setting
the flag to `false` disables provider calls and comment output.

When `state_connection` is omitted, the built-in scheduler uses the warehouse
connection for its state adapter and disables query-context comments on that
adapter. To add context to state SQL, configure `state_connection` explicitly
and set a provider on its adapter.

## Read the payload

The version-one payload uses the SQLMesh model's canonical name and opaque
snapshot identifier. Operation intervals use UTC half-open boundaries. Fields
that are unavailable are omitted.

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

The provider assigns an operation ID to the statements for one model evaluation,
including nested audits. A later evaluation gets a new ID. The adapter preserves
the provider's ID. Consumers can compare it with the execution observer's record.

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
