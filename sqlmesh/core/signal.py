from __future__ import annotations

import typing as t
from sqlmesh.utils import UniqueKeyDict, registry_decorator
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.errors import MissingSourceError

if t.TYPE_CHECKING:
    from sqlmesh.core.context import ExecutionContext
    from sqlmesh.core.snapshot.definition import Snapshot
    from sqlmesh.utils.date import DatetimeRanges


class signal(registry_decorator):
    """Specifies a function which intervals are ready from a list of scheduled intervals.

    When SQLMesh wishes to execute a batch of intervals, say between `a` and `d`, then
    the `batch` parameter will contain each individual interval within this batch,
    i.e.: `[a,b),[b,c),[c,d)`.

    This function may return `True` to indicate that the whole batch is ready,
    `False` to indicate none of the batch's intervals are ready, or a list of
    intervals (a batch) to indicate exactly which ones are ready.

    When returning a batch, the function is expected to return a subset of
    the `batch` parameter, e.g.: `[a,b),[b,c)`. Note that it may return
    gaps, e.g.: `[a,b),[c,d)`, but it may not alter the bounds of any of the
    intervals.

    The interface allows an implementation to check batches of intervals without
    having to actually compute individual intervals itself.

    Args:
        batch: the list of intervals that are missing and scheduled to run.

    Returns:
        Either `True` to indicate all intervals are ready, `False` to indicate none are
        ready or a list of intervals to indicate exactly which ones are ready.
    """


SignalRegistry = UniqueKeyDict[str, signal]


@signal()
def freshness(
    batch: DatetimeRanges,
    snapshot: Snapshot,
    context: ExecutionContext,
) -> t.Union[bool, DatetimeRanges]:
    """
    Implements model freshness as a signal, i.e it considers this model to be fresh if:
    - Any upstream SQLMesh model has available intervals to compute i.e is fresh
    - Any upstream external model has been altered since the last time the model was evaluated
    """
    adapter = context.engine_adapter
    if context.is_restatement or not adapter.SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS:
        return True

    from sqlmesh.core.snapshot.definition import DeployabilityIndex

    deployability_index = context.deployability_index or DeployabilityIndex.all_deployable()

    last_altered_ts = (
        snapshot.last_altered_ts
        if deployability_index.is_deployable(snapshot)
        else snapshot.dev_last_altered_ts
    )

    ready_intervals: t.Union[bool, DatetimeRanges]
    if not last_altered_ts:
        ready_intervals = True
    else:
        parent_snapshots = {context.snapshots[p.name] for p in snapshot.parents}
        upstream_parent_snapshots = {p for p in parent_snapshots if not p.is_external}
        external_parents = snapshot.node.depends_on - {p.name for p in upstream_parent_snapshots}

        if context.parent_intervals:
            # At least one upstream sqlmesh model has intervals to compute (i.e is fresh),
            # so the current model is considered fresh too
            ready_intervals = True
        elif external_parents:
            external_last_altered_timestamps = adapter.get_table_last_modified_ts(
                list(external_parents)
            )

            if len(external_last_altered_timestamps) != len(external_parents):
                raise MissingSourceError(
                    f"Expected {len(external_parents)} sources to be present, but got {len(external_last_altered_timestamps)}."
                )

            # Finding new data means that the upstream depedencies have been altered
            # since the last time the model was evaluated
            ready_intervals = any(
                external_last_altered_ts > last_altered_ts
                for external_last_altered_ts in external_last_altered_timestamps
            )
        else:
            ready_intervals = False

    if context.restated_interval and ready_intervals is not True:
        r_start, r_end = context.restated_interval
        restated_batch = [
            i for i in batch if to_timestamp(i[0]) < r_end and r_start < to_timestamp(i[1])
        ]
        if restated_batch:
            if isinstance(ready_intervals, list):
                combined_set = set(ready_intervals) | set(restated_batch)
                return [i for i in batch if i in combined_set]
            return restated_batch

    return ready_intervals
