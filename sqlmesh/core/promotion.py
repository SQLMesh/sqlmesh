# SPDX-License-Identifier: Apache-2.0

"""Helpers for validating promotion-time coverage changes."""

from __future__ import annotations

import typing as t
from dataclasses import dataclass
from enum import Enum

from sqlmesh.core.snapshot import Snapshot, SnapshotId, merge_intervals
from sqlmesh.core.snapshot.definition import Interval
from sqlmesh.utils.date import make_exclusive, now_timestamp, to_timestamp
from sqlmesh.utils.pydantic import PydanticModel


class PromotionFrontier(PydanticModel, frozen=True):
    """The incumbent snapshot frontier captured while a plan is built.

    ``end`` is an exclusive epoch-millisecond boundary.  Keeping the snapshot
    identity with the timestamp prevents a plan from applying its old frontier
    to a different incumbent snapshot after another plan has run.
    """

    snapshot_id: SnapshotId
    end: int


class TrailingGap(PydanticModel):
    """An accepted contiguous suffix of incumbent coverage absent from a candidate."""

    start: int
    end: int
    interval_count: int
    next_scheduler_boundary: int

    @property
    def interval(self) -> Interval:
        return (self.start, self.end)


class TrailingGapRejectionReason(str, Enum):
    """Stable policy categories for a rejected bounded trailing gap."""

    DISABLED = "disabled"
    FRONTIER_UNAVAILABLE = "frontier_unavailable"
    SCHEDULE_INELIGIBLE = "schedule_ineligible"
    PLANNED_COVERAGE_INCOMPLETE = "planned_coverage_incomplete"
    NON_TRAILING_GAP = "non_trailing_gap"
    BOUND_EXCEEDED = "bound_exceeded"
    DOWNSTREAM_COVERAGE = "downstream_coverage"


@dataclass(frozen=True)
class TrailingGapEvaluation:
    """Result of evaluating the bounded trailing-gap promotion policy."""

    gap: t.Optional[TrailingGap]
    reason: t.Optional[TrailingGapRejectionReason] = None


def evaluate_trailing_gap(
    target_snapshot: Snapshot,
    incumbent_snapshot: Snapshot,
    planned_frontier: t.Optional[PromotionFrontier],
    max_trailing_gap_intervals: int,
    *,
    start: int,
) -> TrailingGapEvaluation:
    """Evaluate whether an absent suffix is a bounded trailing gap.

    This subtracts the candidate's *stored* intervals from the incumbent's
    stored intervals. ``Snapshot.missing_intervals`` cannot provide a safe
    policy count because it expands work for lookback. The resulting suffix is
    counted against the model's scheduler cron boundaries.

    ``gap`` is populated only when the suffix is accepted. Otherwise ``reason``
    gives a stable category for the policy rejection. Callers should retain the
    normal strict no-gap failure when ``gap`` is absent.

    Args:
        target_snapshot: The candidate snapshot being promoted.
        incumbent_snapshot: The snapshot currently referenced by the target
            environment.
        planned_frontier: The incumbent identity and end captured when the
            plan's original backfill bounds were selected.
        max_trailing_gap_intervals: Maximum number of scheduler boundaries the
            accepted suffix may span.
        start: Inclusive start of the original no-gap obligation. The
            promotion facade passes this value so historical coverage is
            checked independently of lookback.
    """
    if max_trailing_gap_intervals < 1:
        return _rejected(TrailingGapRejectionReason.DISABLED)

    # The frontier is immutable plan metadata.  A matching timestamp on a
    # different incumbent snapshot is not sufficient evidence for acceptance.
    if planned_frontier is None or planned_frontier.snapshot_id != incumbent_snapshot.snapshot_id:
        return _rejected(TrailingGapRejectionReason.FRONTIER_UNAVAILABLE)

    if not _same_schedule(target_snapshot, incumbent_snapshot):
        return _rejected(TrailingGapRejectionReason.SCHEDULE_INELIGIBLE)

    incumbent_intervals = merge_intervals(incumbent_snapshot.intervals)
    target_intervals = merge_intervals(target_snapshot.intervals)
    if not incumbent_intervals:
        return _rejected(TrailingGapRejectionReason.NON_TRAILING_GAP)

    observed_end = incumbent_intervals[-1][1]
    planned_end = int(planned_frontier.end)
    if planned_end <= 0 or planned_end >= observed_end:
        return _rejected(TrailingGapRejectionReason.FRONTIER_UNAVAILABLE)

    required_start = to_timestamp(start)
    if required_start >= planned_end:
        return _rejected(TrailingGapRejectionReason.FRONTIER_UNAVAILABLE)

    # The candidate must be complete through the original plan obligation.
    # This is an actual coverage check and therefore does not include lookback
    # expansion.
    if not _covers(target_intervals, required_start, planned_end):
        return _rejected(TrailingGapRejectionReason.PLANNED_COVERAGE_INCOMPLETE)

    # The incumbent itself must provide a continuous source suffix.  A hole in
    # incumbent coverage cannot be safely attributed to scheduler drift.
    if not _covers(incumbent_intervals, planned_end, observed_end):
        return _rejected(TrailingGapRejectionReason.NON_TRAILING_GAP)

    missing = _subtract_coverage(
        incumbent_intervals,
        target_intervals,
        required_start,
        observed_end,
    )
    if len(missing) != 1:
        return _rejected(TrailingGapRejectionReason.NON_TRAILING_GAP)

    gap_start, gap_end = missing[0]
    if gap_start < planned_end or gap_end != observed_end:
        return _rejected(TrailingGapRejectionReason.NON_TRAILING_GAP)
    # A model end is an exclusive execution boundary. A suffix beyond it
    # cannot be repaired by the candidate's scheduler, so fail closed even if
    # the incumbent happens to contain that coverage.
    if target_snapshot.node.end:
        try:
            model_end = to_timestamp(make_exclusive(target_snapshot.node.end))
        except Exception:
            return _rejected(TrailingGapRejectionReason.SCHEDULE_INELIGIBLE)
        if gap_end > model_end:
            return _rejected(TrailingGapRejectionReason.SCHEDULE_INELIGIBLE)
    # Coverage after the gap, including coverage beyond the incumbent's
    # observed frontier, makes this an internal hole rather than a suffix.
    if any(interval_start >= gap_start for interval_start, _ in target_intervals):
        return _rejected(TrailingGapRejectionReason.NON_TRAILING_GAP)

    boundary_evaluation = _scheduler_boundaries(
        target_snapshot, gap_start, gap_end, max_trailing_gap_intervals
    )
    if boundary_evaluation.bound_exceeded:
        return _rejected(TrailingGapRejectionReason.BOUND_EXCEEDED)
    boundaries = boundary_evaluation.boundaries
    if boundaries is None or not boundaries:
        return _rejected(TrailingGapRejectionReason.SCHEDULE_INELIGIBLE)

    try:
        # The warning describes the next scheduler run from the current wall
        # clock. The observed frontier is historical state and may be stale.
        next_scheduler_boundary = to_timestamp(target_snapshot.node.cron_next(now_timestamp()))
    except Exception:
        return _rejected(TrailingGapRejectionReason.SCHEDULE_INELIGIBLE)

    if next_scheduler_boundary <= observed_end:
        return _rejected(TrailingGapRejectionReason.SCHEDULE_INELIGIBLE)

    return TrailingGapEvaluation(
        gap=TrailingGap(
            start=gap_start,
            end=gap_end,
            interval_count=len(boundaries),
            next_scheduler_boundary=next_scheduler_boundary,
        )
    )


def _rejected(reason: TrailingGapRejectionReason) -> TrailingGapEvaluation:
    return TrailingGapEvaluation(gap=None, reason=reason)


def _same_schedule(target_snapshot: Snapshot, incumbent_snapshot: Snapshot) -> bool:
    """Require the two snapshots to retain the same interval semantics."""
    return (
        target_snapshot.node.cron == incumbent_snapshot.node.cron
        and target_snapshot.node.cron_tz == incumbent_snapshot.node.cron_tz
        and target_snapshot.node.interval_unit == incumbent_snapshot.node.interval_unit
        and target_snapshot.allow_partials == incumbent_snapshot.allow_partials
        and target_snapshot.node.start == incumbent_snapshot.node.start
        and target_snapshot.node.end == incumbent_snapshot.node.end
    )


def _covers(intervals: t.Sequence[Interval], start: int, end: int) -> bool:
    if start >= end:
        return True

    cursor = start
    for interval_start, interval_end in intervals:
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return False


def _subtract_coverage(
    source: t.Sequence[Interval],
    covered: t.Sequence[Interval],
    start: int,
    end: int,
) -> t.List[Interval]:
    """Return uncovered portions of source in ``[start, end)``."""
    missing: t.List[Interval] = []
    covered_index = 0

    for source_start, source_end in source:
        source_start = max(source_start, start)
        source_end = min(source_end, end)
        if source_start >= source_end:
            continue

        cursor = source_start
        while covered_index < len(covered) and covered[covered_index][1] <= cursor:
            covered_index += 1

        index = covered_index
        while index < len(covered):
            covered_start, covered_end = covered[index]
            if covered_start >= source_end:
                break
            if covered_end <= cursor:
                index += 1
                continue
            if covered_start > cursor:
                missing.append((cursor, min(covered_start, source_end)))
            cursor = max(cursor, covered_end)
            if cursor >= source_end:
                break
            index += 1

        if cursor < source_end:
            missing.append((cursor, source_end))

    return missing


@dataclass(frozen=True)
class _SchedulerBoundaryEvaluation:
    boundaries: t.Optional[t.List[Interval]]
    bound_exceeded: bool = False


def _scheduler_boundaries(
    snapshot: Snapshot,
    start: int,
    end: int,
    max_count: int,
) -> _SchedulerBoundaryEvaluation:
    """Evaluate scheduler alignment while preserving bound failures separately."""
    if start >= end:
        return _SchedulerBoundaryEvaluation(boundaries=[])

    node = snapshot.node
    interval_unit = node.interval_unit

    def coverage_boundary(cron_timestamp: int) -> int:
        # Match Snapshot.missing_intervals: non-partial coverage ends at the
        # interval-unit floor of the scheduler tick, including offset crons.
        return (
            cron_timestamp
            if snapshot.allow_partials
            else to_timestamp(interval_unit.cron_floor(cron_timestamp))
        )

    try:
        cron_timestamp = to_timestamp(node.cron_floor(start))
        if coverage_boundary(cron_timestamp) < start:
            cron_timestamp = to_timestamp(node.cron_next(start))
        if coverage_boundary(cron_timestamp) != start:
            return _SchedulerBoundaryEvaluation(boundaries=None)
    except Exception:
        return _SchedulerBoundaryEvaluation(boundaries=None)

    boundaries: t.List[Interval] = []
    current = start
    while current < end:
        if len(boundaries) >= max_count:
            return _SchedulerBoundaryEvaluation(boundaries=None, bound_exceeded=True)
        try:
            next_cron_timestamp = to_timestamp(node.cron_next(cron_timestamp))
            next_boundary = coverage_boundary(next_cron_timestamp)
        except Exception:
            return _SchedulerBoundaryEvaluation(boundaries=None)
        if next_boundary <= current or next_boundary > end:
            return _SchedulerBoundaryEvaluation(boundaries=None)
        boundaries.append((current, next_boundary))
        current = next_boundary
        cron_timestamp = next_cron_timestamp

    return _SchedulerBoundaryEvaluation(boundaries=boundaries if current == end else None)
