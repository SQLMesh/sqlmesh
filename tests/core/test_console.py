import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from rich.console import Console as RichConsole
from sqlglot import parse_one

from sqlmesh.core.console import DebuggerTerminalConsole, MarkdownConsole, TerminalConsole
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.model import SqlModel
from sqlmesh.core.model.kind import IncrementalByTimeRangeKind, TimeColumn
from sqlmesh.core.snapshot import Snapshot, SnapshotChangeCategory
from sqlmesh.utils.date import to_timestamp
from sqlmesh.utils.rich import strip_ansi_codes


def test_markdown_console_warning_block():
    console = MarkdownConsole(
        alert_block_max_content_length=100, alert_block_collapsible_threshold=45
    )
    assert console.consume_captured_warnings() == ""

    # single warning, within threshold
    console.log_warning("First warning")
    assert console.consume_captured_warnings() == "> [!WARNING]\n>\n> First warning\n\n"

    # multiple warnings, within threshold (list syntax)
    console.log_warning("First warning")
    console.log_warning("Second warning")
    assert (
        console.consume_captured_warnings()
        == "> [!WARNING]\n>\n> - First warning\n>\n> - Second warning\n\n"
    )

    # single warning, within max threshold but over collapsible section threshold
    warning = "The snowflake engine is not recommended for storing SQLMesh state in production deployments"
    assert len(warning) > console.alert_block_collapsible_threshold
    assert len(warning) < console.alert_block_max_content_length
    console.log_warning(warning)
    assert (
        console.consume_captured_warnings()
        == "> [!WARNING]\n> <details>\n>\n> The snowflake engine is not recommended for storing SQLMesh state in production deployments\n> </details>\n"
    )

    # single warning, over max threshold
    warning = "The snowflake engine is not recommended for storing SQLMesh state in production deployments. Please see <web link> for a list of recommended engines and more information."
    assert len(warning) > console.alert_block_collapsible_threshold
    assert len(warning) > console.alert_block_max_content_length
    console.log_warning(warning)
    assert (
        console.consume_captured_warnings()
        == "> [!WARNING]\n> <details>\n>\n> The snowflake engine is not re...\n>\n> Truncated. Please check the console for full information.\n> </details>\n"
    )

    # multiple warnings, within max threshold but over collapsible section threshold
    warning_1 = "This is the first warning"
    warning_2 = "This is the second warning"
    assert (len(warning_1) + len(warning_2)) > console.alert_block_collapsible_threshold
    assert (len(warning_1) + len(warning_2)) < console.alert_block_max_content_length
    console.log_warning(warning_1)
    console.log_warning(warning_2)
    assert (
        console.consume_captured_warnings()
        == "> [!WARNING]\n> <details>\n>\n> - This is the first warning\n>\n> - This is the second warning\n> </details>\n"
    )

    # multiple warnings, over max threshold
    warning_1 = "This is the first warning and its really really long"
    warning_2 = "This is the second warning and its also really really long"
    assert (len(warning_1) + len(warning_2)) > console.alert_block_collapsible_threshold
    assert (len(warning_1) + len(warning_2)) > console.alert_block_max_content_length
    console.log_warning(warning_1)
    console.log_warning(warning_2)
    assert (
        console.consume_captured_warnings()
        == "> [!WARNING]\n> <details>\n>\n> - This is the first warning an...\n>\n> Truncated. Please check the console for full information.\n> </details>\n"
    )

    assert console.consume_captured_warnings() == ""


def test_markdown_console_error_block():
    console = MarkdownConsole(
        alert_block_max_content_length=100, alert_block_collapsible_threshold=40
    )
    assert console.consume_captured_errors() == ""

    # single error, within threshold
    console.log_error("First error")
    assert console.consume_captured_errors() == "> [!CAUTION]\n>\n> First error\n\n"

    # multiple errors, within threshold (list syntax)
    console.log_error("First error")
    console.log_error("Second error")
    assert (
        console.consume_captured_errors()
        == "> [!CAUTION]\n>\n> - First error\n>\n> - Second error\n\n"
    )

    # single error, within max threshold but over collapsible section threshold
    error = "The snowflake engine is not recommended for storing SQLMesh state in production deployments"
    assert len(error) > console.alert_block_collapsible_threshold
    assert len(error) < console.alert_block_max_content_length
    console.log_error(error)
    assert (
        console.consume_captured_errors()
        == "> [!CAUTION]\n> <details>\n>\n> The snowflake engine is not recommended for storing SQLMesh state in production deployments\n> </details>\n"
    )

    # single error, over max threshold
    error = "The snowflake engine is not recommended for storing SQLMesh state in production deployments. Please see <web link> for a list of recommended engines and more information."
    assert len(error) > console.alert_block_collapsible_threshold
    assert len(error) > console.alert_block_max_content_length
    console.log_error(error)
    assert (
        console.consume_captured_errors()
        == "> [!CAUTION]\n> <details>\n>\n> The snowflake engine is not re...\n>\n> Truncated. Please check the console for full information.\n> </details>\n"
    )

    # multiple errors, within max threshold but over collapsible section threshold
    error_1 = "This is the first error"
    error_2 = "This is the second error"
    assert (len(error_1) + len(error_2)) > console.alert_block_collapsible_threshold
    assert (len(error_1) + len(error_2)) < console.alert_block_max_content_length
    console.log_error(error_1)
    console.log_error(error_2)
    assert (
        console.consume_captured_errors()
        == "> [!CAUTION]\n> <details>\n>\n> - This is the first error\n>\n> - This is the second error\n> </details>\n"
    )

    # multiple errors, over max threshold
    error_1 = "This is the first error and its really really long"
    error_2 = "This is the second error and its also really really long"
    assert (len(error_1) + len(error_2)) > console.alert_block_collapsible_threshold
    assert (len(error_1) + len(error_2)) > console.alert_block_max_content_length
    console.log_error(error_1)
    console.log_error(error_2)
    assert (
        console.consume_captured_errors()
        == "> [!CAUTION]\n> <details>\n>\n> - This is the first error and ...\n>\n> Truncated. Please check the console for full information.\n> </details>\n"
    )

    assert console.consume_captured_errors() == ""


def _make_evaluation_console(width: int = 200, force_terminal: bool = True) -> tuple:
    """Return (buf, TerminalConsole) backed by a no-color in-memory console."""
    buf = io.StringIO()
    rich = RichConsole(file=buf, force_terminal=force_terminal, no_color=True, width=width)
    return buf, TerminalConsole(console=rich)


def _readable_lines(buf: io.StringIO) -> list:
    """Strip ANSI codes and return non-blank lines from buf."""
    return [line for line in strip_ansi_codes(buf.getvalue()).splitlines() if line.strip()]


@pytest.mark.parametrize("force_terminal", [True, False])
def test_snapshot_evaluation_running_row(make_snapshot, force_terminal: bool):
    """Running row appears before completion; both rows share the same column alignment."""
    model = SqlModel(
        name="silver.model4",
        kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ts")),
        cron="@hourly",
        query=parse_one("SELECT ts FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    # Hourly interval: exclusive end 15:00:00 => inclusive end 14:59:59
    interval = (to_timestamp("2026-07-17 13:45:00"), to_timestamp("2026-07-17 15:00:00"))
    env_info = EnvironmentNamingInfo()
    batched_intervals = {snapshot: [interval]}

    buf, tc = _make_evaluation_console(force_terminal=force_terminal)
    tc.start_evaluation_progress(batched_intervals, env_info, default_catalog=None)

    tc.start_snapshot_evaluation_progress(snapshot)
    tc.start_snapshot_evaluation_batch(snapshot, interval, 0)

    # Completion row with fixed 1234 ms => "1.23s"
    tc.update_snapshot_evaluation_progress(snapshot, interval, 0, 1234, 0, 0)
    tc.stop_evaluation_progress(success=False)

    lines = _readable_lines(buf)

    running_lines = [ln for ln in lines if ln.rstrip().endswith("Running")]
    assert len(running_lines) == 1, f"Expected exactly one Running line, got: {running_lines!r}"

    duration_lines = [ln for ln in lines if "1.23s" in ln]
    assert len(duration_lines) == 1, f"Expected exactly one duration line, got: {duration_lines!r}"

    # Running must precede completion
    running_pos = next(i for i, ln in enumerate(lines) if ln.rstrip().endswith("Running"))
    duration_pos = next(i for i, ln in enumerate(lines) if "1.23s" in ln)
    assert running_pos < duration_pos, "Running row must appear before the completion row"

    # Both rows carry batch indicator, model name, and inclusive interval end
    for row in (running_lines[0], duration_lines[0]):
        assert "[1/1]" in row, f"Batch indicator missing: {row!r}"
        assert "silver.model4" in row, f"Model name missing: {row!r}"
        assert "13:45:00-14:59:59" in row, f"Inclusive end missing: {row!r}"

    # Status/duration starts at the same column in both rows
    running_stripped = running_lines[0].rstrip()
    duration_stripped = duration_lines[0].rstrip()
    running_prefix_len = len(running_stripped) - len("Running")
    duration_prefix_len = len(duration_stripped) - len("1.23s")
    assert running_prefix_len == duration_prefix_len, (
        f"Status not aligned: Running col={running_prefix_len}, duration col={duration_prefix_len}\n"
        f"  Running : {running_stripped!r}\n"
        f"  Duration: {duration_stripped!r}"
    )


def test_snapshot_evaluation_zero_duration(make_snapshot):
    """A zero-millisecond batch must print '0.00s', not be silently skipped."""
    model = SqlModel(
        name="gold.zeromodel",
        kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ts")),
        cron="@hourly",
        query=parse_one("SELECT ts FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    interval = (to_timestamp("2026-07-17 00:00:00"), to_timestamp("2026-07-17 01:00:00"))
    env_info = EnvironmentNamingInfo()
    batched_intervals = {snapshot: [interval]}

    buf, tc = _make_evaluation_console()
    tc.start_evaluation_progress(batched_intervals, env_info, default_catalog=None)
    tc.start_snapshot_evaluation_progress(snapshot)
    tc.start_snapshot_evaluation_batch(snapshot, interval, 0)
    tc.update_snapshot_evaluation_progress(snapshot, interval, 0, 0, 0, 0)
    tc.stop_evaluation_progress(success=False)

    lines = _readable_lines(buf)
    zero_dur_lines = [ln for ln in lines if "0.00s" in ln]
    assert len(zero_dur_lines) == 1, f"Expected one '0.00s' line, got: {zero_dur_lines!r}"


def test_snapshot_evaluation_failed_row(make_snapshot):
    """A failed batch replaces the durable running state with a terminal status."""
    model = SqlModel(
        name="silver.failed_model",
        kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ts")),
        cron="@hourly",
        query=parse_one("SELECT ts FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    interval = (to_timestamp("2026-07-17 13:00:00"), to_timestamp("2026-07-17 14:00:00"))
    buf, tc = _make_evaluation_console(width=80, force_terminal=False)
    tc.start_evaluation_progress(
        {snapshot: [interval]}, EnvironmentNamingInfo(), default_catalog=None
    )
    tc.start_snapshot_evaluation_progress(snapshot)
    tc.start_snapshot_evaluation_batch(snapshot, interval, 0)

    running_lines = [line for line in _readable_lines(buf) if line.rstrip().endswith("Running")]
    assert len(running_lines) == 1
    assert "[1/1]" in running_lines[0]
    assert "silver.failed_model" in running_lines[0]
    assert "13:00:00-13:59:59" in running_lines[0]

    # A missing duration indicates that evaluation raised before it completed.
    tc.update_snapshot_evaluation_progress(snapshot, interval, 0, None, 0, 0)
    tc.stop_evaluation_progress(success=False)

    failed_lines = [line for line in _readable_lines(buf) if line.rstrip().endswith("Failed")]
    assert len(failed_lines) == 1
    assert "[1/1]" in failed_lines[0]
    assert "silver.failed_model" in failed_lines[0]
    assert "13:00:00-13:59:59" in failed_lines[0]


def test_snapshot_evaluation_audit_only_multiple_batches(make_snapshot):
    """Audit-only progress retains its model task until every interval completes."""
    model = SqlModel(
        name="silver.audited_model",
        kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ts")),
        cron="@hourly",
        query=parse_one("SELECT ts FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    intervals = [
        (to_timestamp("2026-07-17 13:00:00"), to_timestamp("2026-07-17 14:00:00")),
        (to_timestamp("2026-07-17 14:00:00"), to_timestamp("2026-07-17 15:00:00")),
    ]
    _, console = _make_evaluation_console(force_terminal=False)
    console.start_evaluation_progress(
        {snapshot: intervals},
        EnvironmentNamingInfo(),
        default_catalog=None,
        audit_only=True,
    )

    for batch_idx, interval in enumerate(intervals):
        console.start_snapshot_evaluation_progress(snapshot, audit_only=True)
        console.update_snapshot_evaluation_progress(
            snapshot,
            interval,
            batch_idx,
            duration_ms=100,
            num_audits_passed=1,
            num_audits_failed=0,
            audit_only=True,
        )

    assert console.evaluation_model_progress is not None
    model_task_id = console.evaluation_model_tasks[snapshot.name]
    assert model_task_id not in console.evaluation_model_progress._tasks
    console.stop_evaluation_progress(success=False)


def test_snapshot_evaluation_concurrent_running_rows(make_snapshot):
    """Concurrent models each emit one complete running row."""
    snapshots = []
    for model_name in ("silver.concurrent_a", "silver.concurrent_b"):
        model = SqlModel(
            name=model_name,
            kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ts")),
            cron="@hourly",
            query=parse_one("SELECT ts FROM tbl"),
        )
        snapshot = make_snapshot(model)
        snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
        snapshots.append(snapshot)

    interval = (to_timestamp("2026-07-17 13:00:00"), to_timestamp("2026-07-17 14:00:00"))
    buf, tc = _make_evaluation_console(force_terminal=False)
    tc.start_evaluation_progress(
        {snapshot: [interval] for snapshot in snapshots},
        EnvironmentNamingInfo(),
        default_catalog=None,
    )

    def start_batch(snapshot: Snapshot) -> None:
        tc.start_snapshot_evaluation_progress(snapshot)
        tc.start_snapshot_evaluation_batch(snapshot, interval, 0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(start_batch, snapshots))

    tc.stop_evaluation_progress(success=False)
    running_lines = [line for line in _readable_lines(buf) if line.rstrip().endswith("Running")]

    assert len(running_lines) == 2
    for model_name in ("silver.concurrent_a", "silver.concurrent_b"):
        assert sum(model_name in line for line in running_lines) == 1


def test_debugger_console_evaluation_batch_start_is_safe(make_snapshot):
    """The debugger console does not depend on TerminalConsole progress-bar state."""
    model = SqlModel(
        name="silver.debugger_model",
        kind=IncrementalByTimeRangeKind(time_column=TimeColumn(column="ts")),
        cron="@hourly",
        query=parse_one("SELECT ts FROM tbl"),
    )
    snapshot = make_snapshot(model)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    interval = (to_timestamp("2026-07-17 13:00:00"), to_timestamp("2026-07-17 14:00:00"))
    buf = io.StringIO()
    console = DebuggerTerminalConsole(
        RichConsole(file=buf, force_terminal=False, no_color=True, width=200)
    )

    console.start_snapshot_evaluation_progress(snapshot)
    console.start_snapshot_evaluation_batch(snapshot, interval, 0)

    assert "Evaluating" in buf.getvalue()
    assert "debugger_model" in buf.getvalue()
