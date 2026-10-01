from sqlmesh.core.console import MarkdownConsole
from sqlmesh.core.snapshot import SnapshotId
from sqlmesh.utils.concurrency import NodeExecutionFailedError


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


def test_markdown_console_failed_model_includes_node_in_captured_error():
    error = NodeExecutionFailedError(SnapshotId(name="model", identifier="snapshot"))
    error.__cause__ = RuntimeError("driver error")
    console = MarkdownConsole()

    console.log_failed_models([error])

    assert "model: driver error" in console.consume_captured_errors()
    assert "* `model`" in console.consume_captured_output()
