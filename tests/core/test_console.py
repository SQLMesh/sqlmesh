import typing as t
from datetime import datetime
from io import StringIO

import pytest
from rich.console import Console as RichConsole

from sqlmesh.core.console import MarkdownConsole, TerminalConsole
from sqlmesh.core.engine_adapter.shared import QueryHistoryRecord
from sqlmesh.core.environment import Environment
from sqlmesh.utils.errors import SQLMeshError


def _create_test_console() -> t.Tuple[StringIO, TerminalConsole]:
    """Creates a console and buffer for validating console output."""
    console_output = StringIO()
    console = RichConsole(file=console_output, force_terminal=True)
    return console_output, TerminalConsole(console=console)


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


def test_show_history():
    output, console = _create_test_console()
    plan_id = "plan-id-123"
    records = [
        QueryHistoryRecord(
            started_at=datetime(2024, 1, 1, 10, 0, 0),
            sql="CREATE TABLE foo AS SELECT 1",
            status="success",
            duration_ms=1500,
            bytes_processed=2048,
        ),
        QueryHistoryRecord(
            started_at=datetime(2024, 1, 1, 10, 0, 5),
            sql="INSERT INTO foo VALUES (1)",
            status="failed",
            error="Column mismatch",
        ),
        QueryHistoryRecord(
            started_at=None,
            sql="MERGE INTO foo USING bar ON foo.id = bar.id",
            status="running",
        ),
    ]

    console.show_history(records, plan_id, environment=None)

    printed = output.getvalue()
    assert plan_id[:8] in printed
    assert "3 queries" in printed
    assert "CREATE TABLE" in printed
    assert "INSERT" in printed
    assert "MERGE" in printed
    assert "Column mismatch" in printed
    assert "re-run with" in printed


def test_show_history_empty():
    output, console = _create_test_console()
    console.show_history([], "plan-id-123", environment=None)
    assert "No queries found" in output.getvalue()
    # hints that a wrong plan id is a possible cause (debugging aid)
    assert "incorrect" in output.getvalue()


def test_select_plan(mocker):
    _, console = _create_test_console()
    environments = [
        Environment(name="dev", start_at="2023-01-01", plan_id="plan-a", snapshots=[]),
        Environment(name="prod", start_at="2023-01-01", plan_id="plan-b", snapshots=[]),
    ]
    mocker.patch.object(console, "_prompt", return_value="2")

    plan_id, env = console.select_plan(environments)

    assert plan_id == "plan-b"
    assert env is environments[1]


def test_select_plan_empty_raises():
    _, console = _create_test_console()
    with pytest.raises(SQLMeshError, match="No environments found"):
        console.select_plan([])
