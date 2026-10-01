# Execution observations

Execution observations let Python integrations observe native SQLMesh work through
an optional callback. They do not replace SQL generation, execution, retries,
transactions, or Console progress reporting.

## Register an observer

Override `Console.get_execution_observer()` to return a callback accepting
`phase`, `facts`, and `error`. Its default return value is `None`.
Install the console before constructing the SQLMesh `Context`:

```python
from typing import Any, Literal, Mapping, Optional

from sqlmesh.core.console import TerminalConsole, set_console
from sqlmesh.core.context import Context
from sqlmesh.core.execution_observation import Observer


def observe(
    phase: Literal["start", "finish"],
    facts: Mapping[str, Any],
    error: Optional[BaseException],
) -> None:
    if phase == "finish":
        print(f"{facts['kind']}/{facts['action']}: error={error!r}")


class ObservedConsole(TerminalConsole):
    def get_execution_observer(self) -> Optional[Observer]:
        return observe


set_console(ObservedConsole())
context = Context(paths=["path/to/project"])
```

Built-in plan-stage evaluation and scheduler dispatch resolve the getter at the
outer observation boundary. Nested dispatch reuses the active registration.
An ordinary `Exception` from the getter disables observation at that boundary;
native work continues.

For an explicitly bounded registration, use `observer_scope(observer)` from
`sqlmesh.core.execution_observation`. Nested scopes restore the preceding
registration on exit. `observer_scope(None)` explicitly disables observation
inside the scope, including automatic Console registration.

## Callback lifecycle

`start` runs before the observed work, with `error=None`. Once the start callback
returns, `finish` runs from `finally` after that work, with its original exception
object or `None`. Both callbacks run synchronously on the invoking thread.

SQLMesh passes the **same read-only mapping object** to start and finish for one
invocation. Distinct invocations have distinct mappings. Consumers may retain the
mapping until its matching finish callback to pair the calls by object identity;
release it afterward. SQLMesh may add facts discovered during execution, so this
mapping is a live view, not an immutable snapshot of the start facts.

Nested workload calls produce nested observations. Callbacks for concurrent
workers can interleave, and there is no global callback ordering. Observers must
support concurrent calls and return promptly. SQLMesh does not allocate execution
IDs, timestamps, durations, or parent IDs; consumers can maintain that information.

Ordinary observer `Exception` failures are suppressed and do not replace the
workload result or error. This guarantee does **not** cover observer-raised
`BaseException` subclasses such as `KeyboardInterrupt` or `SystemExit`: a failure
in start can prevent work and finish, and a failure in finish can replace a
workload error. Blocking callbacks and process termination are also outside the
guarantee. SQLMesh does not recursively log observer failures.

While a callback is being delivered, observations triggered by that callback are
suppressed, and internal metadata updates are ignored. This guard applies only
during callback delivery: a normal model operation can still contain observed
physical operations, audits, and queries.

## Native facts and boundaries

Every mapping contains `kind` and `action`. Other fields depend on the boundary
and may be absent, particularly when work exits early or fails.

| Kind | Action | Native facts |
| --- | --- | --- |
| `stage` | Native plan-stage name | `native_plan_id` |
| `model` | `evaluate`, `audit_only` | `snapshot`, `interval`, `batch_index`, `execution_time`, `audit_only` |
| `physical` | `create`, `materialize`, `schema_migration` | `snapshot`; `target` when known; `creating` for materialization; `existed` for schema migration when known |
| `virtual` | `promote`, `demote` | `snapshot`; `target` when known; `source_table` for promotion when known |
| `audit` | `audit` | `audit_name`; `audit_result` at finish if produced |
| `query` | `execute` | `engine` dialect string |

`Snapshot` and `AuditResult` values are borrowed native objects. Treat them and
other nested values as read-only, and do not retain them beyond finish. The
mapping's read-only wrapper does not freeze these objects. Normalize any values
needed for later processing during the callback; facts are not a JSON event
schema. SQLMesh does not interpret audit results as consumer statuses or inherit
model facts into child mappings.

Audit observations finish before later audits or WAP publication can fail. An
audit query failure can produce a finish callback without an `AuditResult`.
Physical materialization includes the surrounding transaction/session exit.
An invocation can perform no work, multiple statements, or internal driver
retries; observations are not an expected-work manifest or a retry inventory.

The query boundary surrounds the base adapter's existing SQL logging and
`_execute` call. It does not include a surrounding transaction's commit or later
fetch/result consumption. Adapter paths that bypass this boundary, including
BigQuery session queries and dataframe loads and ClickHouse dataframe inserts,
do not necessarily produce query observations. These hooks therefore do not
provide a complete warehouse activity history or a transaction-success guarantee.

SQL logs retain their existing behavior, including VALUES redaction for
non-query expressions. The callback does not receive SQL text, adapter objects,
or warehouse query IDs. Starting the observation before SQL logging lets a
consumer associate existing logs with the invocation.

## Worker context

When observation is active, the instrumented scheduler and snapshot DAG dispatch
paths copy the admitting context at execution time. Each node runs in its own
copy, including serial DAG execution. Consumer-owned `ContextVar` values can
therefore carry parent identities into workers; successors inherit the admission
context rather than the preceding worker's changes. Reusing a DAG executor
captures a fresh context for each run.

Context copies are shallow. Mutable objects stored in ContextVars can still be
shared; consumers should use immutable values or replace values instead of
mutating shared state. This behavior applies to the instrumented DAG paths, not
to arbitrary consumer threads or every SQLMesh concurrency helper.

With no observer registered, these call sites do not opt into context copying
and retain their existing serial and worker execution behavior.
