# Local query execution context example

This example runs two incremental models with daily batches and `not_null`
audits in a temporary DuckDB database. It applies a plan to `dev`, then runs a
later interval.

From the SQLMesh repository root, run:

```bash
.venv/bin/python examples/query_execution_context/run_local.py
```

The script registers the query-context provider on each warehouse adapter before
it loads the project. `QueryContextObserver` builds model and batch metadata from
SQLMesh's native observation events. Its provider returns the active immutable
context in each worker. The existing console observer still receives its events.

The example checks four batches during the plan and two during the later run. It
confirms that the observer events, materialization SQL, and audits share each
batch's operation ID. It also verifies the resulting rows and scope cleanup. The
temporary project and DuckDB database are removed when the script exits.

The example does not connect to an external database. For configuration and
payload details, see [Query execution context](../../docs/concepts/query_execution_context.md).
