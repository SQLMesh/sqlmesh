# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from sqlglot import parse_one

from sqlmesh.core.model import IncrementalByTimeRangeKind, SqlModel
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.promotion import (
    PromotionFrontier,
    TrailingGapRejectionReason,
    classify_trailing_gap,
    evaluate_trailing_gap,
)
from sqlmesh.core.snapshot import Snapshot
from sqlmesh.utils.date import to_timestamp


def _snapshot_pair(
    make_snapshot,
    *,
    cron: str = "@hourly",
    allow_partials: bool = False,
    interval_unit: IntervalUnit | None = None,
    lookback: int = 0,
    end: str | None = None,
) -> tuple[Snapshot, Snapshot]:
    kind = IncrementalByTimeRangeKind(
        time_column="ds", **({"lookback": lookback} if lookback else {})
    )
    model_kwargs = {
        "name": "a",
        "cron": cron,
        "allow_partials": allow_partials,
        "query": parse_one("select 1, ds"),
        "kind": kind,
        "start": "2023-01-01",
    }
    if interval_unit is not None:
        model_kwargs["interval_unit"] = interval_unit
    if end is not None:
        model_kwargs["end"] = end

    incumbent_model = SqlModel(**model_kwargs)
    target_model = incumbent_model.copy()
    incumbent = make_snapshot(incumbent_model, version="incumbent")
    target = make_snapshot(target_model, version="target")
    target.fingerprint = incumbent.fingerprint.copy(update={"data_hash": "target"})
    return incumbent, target


def _frontier(snapshot: Snapshot, end: str) -> PromotionFrontier:
    return PromotionFrontier(snapshot_id=snapshot.snapshot_id, end=to_timestamp(end))


def test_classify_accepts_one_stored_scheduler_suffix(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 11:00")

    result = classify_trailing_gap(
        target,
        incumbent,
        _frontier(incumbent, "2023-01-01 11:00"),
        1,
        start=to_timestamp("2023-01-01"),
    )

    assert result.start == to_timestamp("2023-01-01 11:00")
    assert result.end == to_timestamp("2023-01-01 12:00")
    assert result.interval_count == 1
    assert result.next_scheduler_boundary > to_timestamp("2023-01-01 12:00")


def test_classify_uses_stored_coverage_without_lookback_expansion(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot, lookback=14)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 11:00")

    result = classify_trailing_gap(
        target,
        incumbent,
        _frontier(incumbent, "2023-01-01 11:00"),
        1,
        start=to_timestamp("2023-01-01"),
    )

    assert result is not None
    assert result.interval_count == 1
    assert result.interval == (
        to_timestamp("2023-01-01 11:00"),
        to_timestamp("2023-01-01 12:00"),
    )


def test_classify_accepts_hourly_partial_suffix_inside_daily_interval(make_snapshot):
    incumbent, target = _snapshot_pair(
        make_snapshot,
        allow_partials=True,
        interval_unit=IntervalUnit.DAY,
    )
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 11:00")

    result = classify_trailing_gap(
        target,
        incumbent,
        _frontier(incumbent, "2023-01-01 11:00"),
        1,
        start=to_timestamp("2023-01-01"),
    )

    assert result is not None
    assert result.interval_count == 1


@pytest.mark.parametrize(
    "cron, interval_unit, planned_end, observed_end",
    [
        (
            "0 */2 * * *",
            None,
            "2023-01-01 02:00",
            "2023-01-01 04:00",
        ),
        (
            "@weekly",
            None,
            "2023-01-08 00:00",
            "2023-01-15 00:00",
        ),
        (
            "@hourly",
            IntervalUnit.HALF_HOUR,
            "2023-01-01 01:00",
            "2023-01-01 02:00",
        ),
    ],
)
def test_classify_counts_non_partial_scheduler_advances(
    make_snapshot, cron, interval_unit, planned_end, observed_end
):
    incumbent, target = _snapshot_pair(make_snapshot, cron=cron, interval_unit=interval_unit)
    incumbent.add_interval("2023-01-01 00:00", observed_end)
    target.add_interval("2023-01-01 00:00", planned_end)

    result = classify_trailing_gap(
        target,
        incumbent,
        _frontier(incumbent, planned_end),
        1,
        start=to_timestamp("2023-01-01"),
    )

    assert result is not None
    assert result.interval_count == 1


@pytest.mark.parametrize(
    "cron, planned_end, observed_end",
    [
        (
            "0 */2 * * *",
            "2023-01-01 02:00",
            "2023-01-01 06:00",
        ),
        (
            "@weekly",
            "2023-01-08 00:00",
            "2023-01-22 00:00",
        ),
    ],
)
def test_classify_bounds_multiple_non_partial_scheduler_advances(
    make_snapshot, cron, planned_end, observed_end
):
    incumbent, target = _snapshot_pair(make_snapshot, cron=cron)
    incumbent.add_interval("2023-01-01 00:00", observed_end)
    target.add_interval("2023-01-01 00:00", planned_end)
    frontier = _frontier(incumbent, planned_end)
    start = to_timestamp("2023-01-01")

    assert classify_trailing_gap(target, incumbent, frontier, 1, start=start) is None

    result = classify_trailing_gap(target, incumbent, frontier, 2, start=start)

    assert result is not None
    assert result.interval_count == 2


def test_classify_preserves_floored_storage_boundaries_for_offset_cron(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot, cron="1 0 * * *")
    incumbent.add_interval("2023-01-01 00:00", "2023-01-03 00:00")
    target.add_interval("2023-01-01 00:00", "2023-01-02 00:00")

    result = classify_trailing_gap(
        target,
        incumbent,
        _frontier(incumbent, "2023-01-02 00:00"),
        1,
        start=to_timestamp("2023-01-01"),
    )

    assert result is not None
    assert result.interval == (
        to_timestamp("2023-01-02 00:00"),
        to_timestamp("2023-01-03 00:00"),
    )
    assert result.interval_count == 1


@pytest.mark.parametrize(
    "planned_end, observed_end",
    [
        ("2023-01-01 02:00", "2023-01-01 03:00"),
        ("2023-01-01 03:00", "2023-01-01 04:00"),
    ],
)
def test_classify_rejects_storage_aligned_non_scheduler_boundaries(
    make_snapshot, planned_end, observed_end
):
    incumbent, target = _snapshot_pair(make_snapshot, cron="0 */2 * * *")
    incumbent.add_interval("2023-01-01 00:00", observed_end)
    target.add_interval("2023-01-01 00:00", planned_end)

    assert (
        classify_trailing_gap(
            target,
            incumbent,
            _frontier(incumbent, planned_end),
            2,
            start=to_timestamp("2023-01-01"),
        )
        is None
    )


@pytest.mark.parametrize(
    "target_intervals, planned_end",
    [
        (
            [
                ("2023-01-01 01:00", "2023-01-01 12:00"),
            ],
            "2023-01-01 11:00",
        ),
        (
            [
                ("2023-01-01 00:00", "2023-01-01 02:00"),
                ("2023-01-01 03:00", "2023-01-01 05:00"),
            ],
            "2023-01-01 02:00",
        ),
    ],
)
def test_classify_rejects_historical_or_internal_holes(
    make_snapshot, target_intervals, planned_end
):
    incumbent, target = _snapshot_pair(make_snapshot)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 05:00")
    for start, end in target_intervals:
        target.add_interval(start, end)

    assert (
        classify_trailing_gap(
            target,
            incumbent,
            _frontier(incumbent, planned_end),
            2,
            start=to_timestamp("2023-01-01"),
        )
        is None
    )


def test_classify_rejects_candidate_coverage_after_gap(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 04:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 02:00")
    target.add_interval("2023-01-01 04:00", "2023-01-01 05:00")

    assert (
        classify_trailing_gap(
            target,
            incumbent,
            _frontier(incumbent, "2023-01-01 02:00"),
            2,
            start=to_timestamp("2023-01-01"),
        )
        is None
    )


def test_classify_rejects_excessive_suffix(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 10:00")

    assert (
        classify_trailing_gap(
            target,
            incumbent,
            _frontier(incumbent, "2023-01-01 10:00"),
            1,
            start=to_timestamp("2023-01-01"),
        )
        is None
    )


def test_classify_rejects_suffix_past_target_model_end(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot, end="2023-01-01 11:00")
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 11:00")

    assert (
        classify_trailing_gap(
            target,
            incumbent,
            _frontier(incumbent, "2023-01-01 11:00"),
            1,
            start=to_timestamp("2023-01-01"),
        )
        is None
    )


def test_classify_rejects_frontier_identity_or_schedule_mismatch(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot)
    other_incumbent, _ = _snapshot_pair(make_snapshot)
    other_incumbent.fingerprint = incumbent.fingerprint.copy(update={"data_hash": "other"})
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 11:00")

    assert (
        classify_trailing_gap(
            target,
            incumbent,
            _frontier(other_incumbent, "2023-01-01 11:00"),
            1,
            start=to_timestamp("2023-01-01"),
        )
        is None
    )

    changed_model = SqlModel(
        name="a",
        cron="@daily",
        query=parse_one("select 1, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        start="2023-01-01",
    )
    changed_target = make_snapshot(changed_model, version="target")
    changed_target.fingerprint = incumbent.fingerprint.copy(update={"data_hash": "changed"})
    changed_target.add_interval("2023-01-01 00:00", "2023-01-01 11:00")

    assert (
        classify_trailing_gap(
            changed_target,
            incumbent,
            _frontier(incumbent, "2023-01-01 11:00"),
            1,
            start=to_timestamp("2023-01-01"),
        )
        is None
    )


def test_evaluate_reports_frontier_and_coverage_reasons(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 10:00")
    frontier = _frontier(incumbent, "2023-01-01 11:00")

    assert (
        evaluate_trailing_gap(
            target,
            incumbent,
            frontier,
            0,
            start=to_timestamp("2023-01-01"),
        ).reason
        == TrailingGapRejectionReason.DISABLED
    )
    assert (
        evaluate_trailing_gap(
            target,
            incumbent,
            None,
            1,
            start=to_timestamp("2023-01-01"),
        ).reason
        == TrailingGapRejectionReason.FRONTIER_UNAVAILABLE
    )
    other_incumbent, _ = _snapshot_pair(make_snapshot)
    other_incumbent.fingerprint = incumbent.fingerprint.copy(update={"data_hash": "other"})
    assert (
        evaluate_trailing_gap(
            target,
            incumbent,
            _frontier(other_incumbent, "2023-01-01 11:00"),
            1,
            start=to_timestamp("2023-01-01"),
        ).reason
        == TrailingGapRejectionReason.FRONTIER_UNAVAILABLE
    )
    assert (
        evaluate_trailing_gap(
            target,
            incumbent,
            _frontier(incumbent, "2023-01-01 12:00"),
            1,
            start=to_timestamp("2023-01-01"),
        ).reason
        == TrailingGapRejectionReason.FRONTIER_UNAVAILABLE
    )
    assert (
        evaluate_trailing_gap(
            target,
            incumbent,
            frontier,
            1,
            start=to_timestamp("2023-01-01"),
        ).reason
        == TrailingGapRejectionReason.PLANNED_COVERAGE_INCOMPLETE
    )


def test_evaluate_distinguishes_schedule_and_bound_rejections(make_snapshot):
    incumbent, target = _snapshot_pair(
        make_snapshot,
        cron="0 */2 * * *",
    )
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 03:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 02:00")
    frontier = _frontier(incumbent, "2023-01-01 02:00")

    assert (
        evaluate_trailing_gap(
            target,
            incumbent,
            frontier,
            1,
            start=to_timestamp("2023-01-01"),
        ).reason
        == TrailingGapRejectionReason.SCHEDULE_INELIGIBLE
    )

    incumbent, target = _snapshot_pair(make_snapshot)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 13:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 11:00")
    frontier = _frontier(incumbent, "2023-01-01 11:00")
    assert (
        evaluate_trailing_gap(
            target,
            incumbent,
            frontier,
            1,
            start=to_timestamp("2023-01-01"),
        ).reason
        == TrailingGapRejectionReason.BOUND_EXCEEDED
    )


def test_evaluate_reports_non_trailing_gap(make_snapshot):
    incumbent, target = _snapshot_pair(make_snapshot)
    incumbent.add_interval("2023-01-01 00:00", "2023-01-01 12:00")
    target.add_interval("2023-01-01 00:00", "2023-01-01 10:00")
    target.add_interval("2023-01-01 11:00", "2023-01-01 12:00")

    result = evaluate_trailing_gap(
        target,
        incumbent,
        _frontier(incumbent, "2023-01-01 10:00"),
        2,
        start=to_timestamp("2023-01-01"),
    )
    assert result.gap is None
    assert result.reason == TrailingGapRejectionReason.NON_TRAILING_GAP
