# Query execution context example

This example runs two incremental models with daily batches and `not_null`
audits in a temporary DuckDB project. It applies a plan to `dev`, then runs a
later interval.

Run it from the SQLMesh repository root with the development environment:

```bash
.venv/bin/python examples/query_execution_context/run_local.py
```

The script registers `QueryContextObserver.current_query_context` as the provider
on each warehouse adapter. `QueryContextObserver` tracks the active context from
SQLMesh's native observation events. A callback records the model operation IDs
used by the assertions.

The plan covers four model batches, and the later run covers two. The script
checks that model events, materialization SQL, and audits use matching operation
IDs. It also checks the model rows and confirms that the query context is cleared
after the run.

The example uses a temporary DuckDB database and makes no external connections.
See [query execution context](../concepts/query_execution_context.md) for the
payload, provider contract, and transport limits.
