# Query execution context

SQLMesh can add structured metadata to warehouse statements so a query-history
tool can connect a statement to the plan or run, model, and evaluation that issued
it. The metadata is a versioned JSON object in a `QUERY_CONTEXT` SQL comment.

Enable `query_execution_context` on a connection and give its engine adapter a
provider that returns a `QueryExecutionContext`. The flag defaults to `false`.
For YAML configuration, set the option under the gateway's `connection`:

```yaml
gateways:
  local:
    connection:
      type: duckdb
      query_execution_context: true
```

The configuration flag enables transport. It does not create a model-context
provider. Without a provider, SQLMesh adds parent metadata only when a plan or run
correlation ID is available. SQLMesh calls the provider after rendering each
statement and before executing it. The provider sees the current worker's context,
so concurrent model evaluations can supply different IDs to a shared adapter.

This provider API does not add automatic model tagging to a stock project.
For a provider that reads native observer events, see the
[query execution context example](../examples/query_execution_context.md).

## Configure a provider

For a Python `Context`, configure its warehouse adapters before loading the
project. The `Context` clones these adapters when it creates its snapshot
evaluator, so the provider and flag carry to those clones.

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

The provider returns the immutable context currently active in that worker, or
`None` when no context applies. An observer can set an evaluation ID when
SQLMesh reports a model batch, then restore its prior context when that
action ends. The sample observer composes with the observer returned by the
console. It preserves that observer's facts and events.

Use the adapter instance that the evaluator will use. Adapters created with
`with_settings` inherit the flag and provider. Passing
`query_execution_context_provider=None` to `with_settings` clears the inherited
provider. Setting the flag to `false` disables provider calls and comment output.

Connection settings also provide defaults to other adapters. Configure the
warehouse adapters where you want model context in query history. The built-in
scheduler can derive its state adapter from the warehouse settings, but it
disables query context on that fallback. To annotate state SQL, configure the
`state_connection` explicitly and provide a context provider for its adapter.

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

The operation ID groups statements for one model evaluation, including nested
audits. A later evaluation gets a new ID. The provider owns the ID; SQLMesh does
not create another operation ID at the adapter boundary. Consumers can compare
the value they supplied with records from their execution observer.

The adapter's own correlation ID sets the payload's `parent`. SQLMesh replaces
a different provider-supplied parent with the adapter's value. When the adapter
has no correlation ID, SQLMesh clears a parent from the provided context.

Consumers must accept absent fields and unknown future phase or role values.
The `workload` identity is the tuple `(system, type, name)`. Its `version` is
opaque and does not change that identity.

## SQL comment behavior

SQLMesh prefixes the context comment after rendering. When a plan or run marker
is present, SQLMesh keeps that legacy marker first. The submitted statement may
look like this:

```sql
/* SQLMESH_RUN: run-123 */ /* QUERY_CONTEXT: {"schema_version":1,"emitter":"sqlmesh","parent":{"system":"sqlmesh","type":"run","id":"run-123"},"query":{"phase":"unknown","role":"query"}} */ SELECT 1
```

The generic comment is bounded to 8,192 UTF-8 bytes. Each identifier is bounded
to 1,024 UTF-8 bytes before JSON escaping. SQLMesh escapes metadata so it cannot
end the comment. In JSON, SQLMesh also escapes slash characters so an identifier
cannot form a nested comment delimiter. If an identifier is invalid or either
limit is exceeded, SQLMesh
omits the complete `QUERY_CONTEXT` comment and logs a warning. It never truncates
the JSON or fails the query for an omitted annotation.

The submitted SQL changes, while rendered model definitions and model fingerprints
do not. If a downstream query-history system hashes raw SQL text, it must account
for the additional comment.

An adapter that does not support generic SQL comments keeps its SQL unchanged
and logs one warning per adapter when the option is enabled. It does not call the
provider. Athena does not support this transport. Engines can override the adapter
hook to add native metadata transport in a separate implementation.

If the provider raises an ordinary exception or returns an invalid value, SQLMesh
omits the rich comment, logs a bounded warning without provider data, and executes
the statement with its legacy correlation marker. It tries the provider again
for the next statement. `KeyboardInterrupt`, `SystemExit`, and other cancellation
exceptions keep their normal behavior and propagate.
