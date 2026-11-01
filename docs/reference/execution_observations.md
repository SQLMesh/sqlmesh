# Execution observations

Execution observations expose native SQLMesh work to Python integrations through
an optional callback. They do not replace SQL generation, execution, retries,
transactions, or `Console` progress reporting.

## Console observer registration

`Console.get_execution_observer()` returns `None` by default. A `Console`
subclass can return an `Observer` callback from `sqlmesh.core.execution_observation`.
The callback receives a
phase, a read-only mapping of facts, and the original exception or `None`.
For general Python integration details, see the [Python API reference](./python.md).

The following console prints the kind, action, and finish error for each observed
operation. The example uses `examples/sushi` relative to the SQLMesh repository root:

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
context = Context(paths=["examples/sushi"])
```

A `Context` uses the `Console` that was current when the context was created.
Unless an explicit `observer_scope()` is active, SQLMesh resolves
`get_execution_observer()` once for each outer plan evaluation or scheduler run.
Nested calls reuse that observer. If the getter raises an ordinary `Exception`,
SQLMesh disables observation for that plan evaluation or scheduler run and
continues native work.

## Observer scopes

`observer_scope(observer)` from `sqlmesh.core.execution_observation` sets the
observer for the duration of the scope. A nested scope restores the previously
active observer when it exits. `observer_scope(None)` disables observation inside
that scope, including automatic Console registration.

## Callback lifecycle

SQLMesh calls `start` before observed work with `error=None`. When `start`
returns or raises an ordinary `Exception`, SQLMesh continues the work and calls
`finish` from a `finally` block afterward. The `finish` callback receives the
original workload exception object or `None`. Both callbacks run synchronously
on the thread that performs the observed work. Callbacks for work dispatched to
a worker run on that worker's thread.

Every invocation that reaches `finish` passes the same read-only mapping object
to `start` and `finish`. Distinct invocations use distinct mapping objects.
Consumers can pair the phases by mapping identity. SQLMesh may add facts while
the work runs, so values in the mapping at `finish` can differ from its values
at `start`. The mapping's supported lifetime ends when the matching `finish`
callback returns.

Ordinary observer `Exception` failures are suppressed and do not replace the
workload result or error. SQLMesh does not suppress observer-raised
`BaseException` subclasses that are not `Exception` subclasses, such as
`KeyboardInterrupt` and `SystemExit`. If `start` raises a `BaseException` that
is not an `Exception`, the work and `finish` callback do not run. If `finish`
raises a `BaseException` that is not an `Exception`, that failure can replace
the workload error.

If a worker raises a `BaseException`, the error can reach the caller after its
worker pool exits. If a DAG run records a terminal error, SQLMesh stops
scheduling nodes and waits for running workers to exit before it raises that
error. Parallel value operations also wait for their workers to finish before
raising a worker's `BaseException`. Blocking callbacks and process termination
are outside the observer failure guarantee. SQLMesh does not log observer
failures.

Nested workload calls produce nested observations. Callbacks for concurrent
workers can interleave, and SQLMesh provides no global callback order. Observers
must support concurrent calls and return promptly. SQLMesh allocates no
execution IDs, timestamps, durations, or parent IDs. Consumers supply those
values when needed.

While SQLMesh delivers a callback, it suppresses observations triggered by that
callback and ignores calls to `update_execution()`. The guard covers callback
delivery only. Normal model work can still contain observations for physical
operations, audits, and queries.

## Native facts and limits

Every facts mapping contains `kind` and `action`. Other fields depend on the
observed operation and can be absent when work exits early or fails.

| Kind | Action | Native facts |
| --- | --- | --- |
| `stage` | Plan-stage class name | `native_plan_id` |
| `model` | `evaluate`, `audit_only` | `snapshot`, `interval`, `batch_index`, `execution_time`, `audit_only` |
| `physical` | `create`, `materialize`, `schema_migration` | `snapshot`; `target` when known; `creating` for materialization; `existed` for schema migration when known |
| `virtual` | `promote`, `demote` | `snapshot`; `target` when known; `source_table` for promotion when known |
| `audit` | `audit` | `audit_name`; `audit_result` at finish if produced |
| `query` | `execute` | `engine` dialect string |

[`Snapshot`](../concepts/architecture/snapshots.md) and `AuditResult` values are
borrowed native objects. Consumers must
treat these objects and other nested values as read-only and must not retain them
beyond the matching `finish` callback. The mapping's read-only wrapper does not
freeze nested objects. Facts are not a JSON event schema. SQLMesh does not
interpret audit results as consumer statuses or inherit model facts into child
mappings. Consumers that need facts after `finish` copy and normalize those
values during the callback.

SQLMesh calls `finish` for an audit before it runs later audits or completes
write-audit-publish (WAP). A later audit or WAP publication can still fail. An audit
query failure can produce a `finish` callback without an `AuditResult`. The
[audits guide](../concepts/audits.md) describes audit behavior in plans and
runs.

For `physical/materialize`, `target` is the logical destination table from the
snapshot. During WAP, SQLMesh writes to a staging destination, but the `target`
fact remains the logical destination. The `finish` callback runs after the
materialization's transaction and session exit. It does not report the result
of later audits or WAP publication.

An observed operation can do no work, issue multiple statements, or trigger
internal driver retries. The observation does not predict how many statements
the operation will issue or report each retry.

The query observation surrounds the base adapter's existing SQL logging and
`_execute` call. It ends before a surrounding transaction commits and before
later fetch or result consumption. Some adapter operations do not call this
query hook. BigQuery session queries, BigQuery dataframe loads, and ClickHouse
dataframe inserts may not produce query observations. These hooks do not provide
a complete warehouse activity history or guarantee transaction success.

SQL logging retains its existing behavior, including VALUES redaction for
non-query expressions. The callback receives no SQL text, adapter objects, or
warehouse query IDs. The `start` callback runs before SQL logging. Consumers can
use that ordering to associate existing log entries with the invocation.

## Worker context

When an observer is active outside callback delivery, SQLMesh copies the current
Python context for each task in scheduler and snapshot DAG work, snapshot schema creation, and
data-object listing. That context carries Python's [context variables](https://docs.python.org/3/library/contextvars.html).
Each task gets its own context copy, including during serial execution. DAG
successors use the context captured for the run. They do not inherit changes
made by earlier workers. Reusing a DAG executor captures a fresh context for
each run.

Context copies are shallow. Mutable objects stored in `ContextVar` values remain
shared across copies. A parent identity stored in a consumer-owned `ContextVar`
is available to each worker. SQLMesh uses these copies in the scheduler and
snapshot DAG, schema creation, and data-object listing. Arbitrary consumer
threads and other concurrency helpers do not receive these copies.

With no observer registered, these operations do not copy contexts. The concurrency
helpers retain their existing `ContextVar` behavior for serial and worker
execution.
