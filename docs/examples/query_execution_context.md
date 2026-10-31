# Query execution context example

This example runs two incremental models with daily batches and `not_null`
audits in a temporary DuckDB project. It applies a plan to `dev`, then runs a
later interval.

Run it from the SQLMesh repository root with the development environment:

```bash
.venv/bin/python examples/query_execution_context/run_local.py
```

The script attaches `QueryContextObserver.current_query_context` to the warehouse
adapter providers. The observer builds one context for each model batch and
delegates events to the observer supplied by the console. The run checks that four
initial batches and two later batches use the same IDs in the model events,
materialization SQL, and audits. It also checks that the resulting model rows are
correct and that the query-context scope is restored after the run.

The example uses a temporary DuckDB database and makes no external connections.
See [query execution context](../concepts/query_execution_context.md) for the
payload, provider contract, and transport limits.
