from io import StringIO

import pytest
from rich.console import Console

from sqlmesh.core.console import MarkdownConsole, TerminalConsole
from sqlmesh.core.context_diff import ContextDiff
from sqlmesh.core.dialect import parse
from sqlmesh.core.environment import EnvironmentNamingInfo, EnvironmentStatements
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.snapshot import SnapshotChangeCategory
from sqlmesh.core.table_diff import RowDiff
from sqlmesh.utils.metaprogramming import Executable


@pytest.fixture
def markdown_context_diff(make_snapshot):
    def make_diff(old_query, new_query, dialect="tsql"):
        old, new = [
            make_snapshot(
                load_sql_based_model(parse(f"MODEL (name test.model, dialect {dialect}); {query}"))
            )
            for query in (old_query, new_query)
        ]
        new.categorize_as(SnapshotChangeCategory.NON_BREAKING)
        return ContextDiff(
            environment="prod",
            is_new_environment=False,
            is_unfinalized_environment=False,
            normalize_environment_name=True,
            create_from="prod",
            create_from_env_exists=True,
            added=set(),
            removed_snapshots={},
            modified_snapshots={new.name: (new, old)},
            snapshots={new.snapshot_id: new},
            new_snapshots={new.snapshot_id: new},
            previous_plan_id=None,
            previously_promoted_snapshot_ids=set(),
            previous_finalized_snapshots=None,
            previous_gateway_managed_virtual_layer=False,
            gateway_managed_virtual_layer=False,
            environment_statements=[],
        )

    return make_diff


@pytest.mark.parametrize(
    "dialect,old_column,new_column,table",
    [
        ("tsql", "[old_col]", "[new_col]", "[schema].[table]"),
        ("tsql", "[old column]", "[new]]column]", "[my schema].[my]]table]"),
        ("duckdb", "old_col", "new_col", "schema_name.table_name"),
    ],
)
def test_markdown_console_model_diff_identifiers(
    markdown_context_diff, dialect, old_column, new_column, table
):
    context_diff = markdown_context_diff(
        f"SELECT {old_column} FROM {table}",
        f"SELECT {new_column} FROM {table}",
        dialect,
    )
    snapshot = next(iter(context_diff.snapshots.values()))
    diff = context_diff.text_diff(snapshot.name)
    assert old_column in diff and new_column in diff and table in diff

    console = MarkdownConsole()
    console.show_model_difference_summary(
        context_diff, EnvironmentNamingInfo(), None, no_diff=False
    )

    output = console.consume_captured_output()
    assert (
        "  ```diff\n" + "\n".join(f"  {line}" for line in diff.split("\n")) + "\n  ```\n" in output
    )
    assert "**Directly Modified:**" in output
    assert "(Non-breaking)" in output
    assert console.consume_captured_output() == ""


@pytest.mark.parametrize("categorized", [False, True])
@pytest.mark.parametrize("backticks", ["", "```", "`````"])
def test_markdown_console_literal_model_diffs(
    markdown_context_diff, mocker, capsys, categorized, backticks
):
    literal = f"[bold] [/invalid] :smile: \\path\t{'long text ' * 20}\n{backticks}\nend"
    context_diff = markdown_context_diff("SELECT 1 AS [old]", f"SELECT '{literal}' AS [new]")
    snapshot = next(iter(context_diff.snapshots.values()))
    diff = context_diff.text_diff(snapshot.name)
    assert all(line in diff for line in literal.split("\n"))
    console = MarkdownConsole(width=30)
    console.log_success("before diff")
    if categorized:
        plan = mocker.Mock(
            context_diff=context_diff,
            categorized=[snapshot],
            indirectly_modified={},
            environment_naming_info=EnvironmentNamingInfo(),
        )
        console._show_categorized_snapshots(plan, None)
    else:
        console.show_model_difference_summary(
            context_diff, EnvironmentNamingInfo(), None, no_diff=False
        )
    console.log_success("after diff")

    output = console.consume_captured_output()
    fence = "`" * max(3, len(backticks) + 1)
    indent = "" if categorized else "  "
    block = (
        f"{indent}{fence}diff\n"
        + "\n".join(f"{indent}{line}" for line in diff.split("\n"))
        + f"\n{indent}{fence}\n"
    )
    assert block in output
    assert output.count(block) == 1
    assert output.index("before diff") < output.index(block) < output.index("after diff")
    assert console.consume_captured_output() == ""
    visible = capsys.readouterr().out
    assert visible.count(f"{fence}diff") == 1
    assert "[bold] [/invalid] :smile:" in visible
    if categorized:
        assert "Directly Modified:" in output
        assert "[bold]" not in output.split(block, 1)[1]


@pytest.mark.parametrize("no_diff,empty_diff", [(True, False), (False, True)])
def test_markdown_console_omitted_model_diffs(markdown_context_diff, mocker, no_diff, empty_diff):
    context_diff = markdown_context_diff("SELECT 1 AS [old]", "SELECT 2 AS [new]")
    text_diff = mocker.patch.object(ContextDiff, "text_diff", return_value="")
    console = MarkdownConsole()
    console.show_model_difference_summary(
        context_diff, EnvironmentNamingInfo(), None, no_diff=no_diff
    )
    output = console.consume_captured_output()
    assert "```" not in output
    assert "**Directly Modified:**" in output
    if empty_diff:
        text_diff.assert_called_once()
    else:
        text_diff.assert_not_called()


@pytest.mark.parametrize("no_diff", [False, True])
def test_markdown_console_environment_sql_diffs(markdown_context_diff, no_diff):
    context_diff = markdown_context_diff("SELECT 1", "SELECT 2")
    context_diff.previous_environment_statements = [
        EnvironmentStatements(
            before_all=["SELECT [old] FROM [schema].[table]"],
            after_all=["SELECT [old]"],
            python_env={"version": Executable.value(1)},
        )
    ]
    context_diff.environment_statements = [
        EnvironmentStatements(
            before_all=["SELECT [new], ':smile: [/invalid]\\path\t' FROM [schema].[table]"],
            after_all=[f"SELECT '```\n{'long text ' * 20}\n```' AS [new]"],
            python_env={"version": Executable.value(2)},
        )
    ]
    console = MarkdownConsole(width=35)
    console.show_environment_difference_summary(context_diff, no_diff=no_diff)
    output = console.consume_captured_output()
    assert "**Summary of differences from" in output and "`prod`:**" in output
    if no_diff:
        assert "Environment statements:" not in output
        assert "```" not in output
        return

    assert "Environment statements:" in output
    for language, diff in context_diff.environment_statements_diff(include_python_env=True):
        if language == "sql":
            fence = "````" if "```" in diff else "```"
            assert f"{fence}diff\n{diff}\n{fence}\n" in output
        else:
            assert diff in output
    assert output.index("before_all:") < output.index("after_all:") < output.index("dependencies:")


@pytest.mark.parametrize("indirect_only", [False, True])
def test_markdown_console_model_change_categories(
    markdown_context_diff, make_snapshot, indirect_only
):
    context_diff = markdown_context_diff("SELECT 1 AS [old]", "SELECT 2 AS [new]")
    current, previous = next(iter(context_diff.modified_snapshots.values()))
    child_model = load_sql_based_model(
        parse("MODEL (name test.child, dialect tsql); SELECT * FROM test.model")
    )
    old_child, new_child = [
        make_snapshot(child_model, nodes={parent.name: parent.model})
        for parent in (previous, current)
    ]
    new_child.categorize_as(SnapshotChangeCategory.INDIRECT_NON_BREAKING)
    old_metadata, new_metadata = [
        make_snapshot(
            load_sql_based_model(
                parse(f"MODEL (name test.metadata, owner {owner}); SELECT 1 AS value")
            )
        )
        for owner in ("old_owner", "new_owner")
    ]
    context_diff.modified_snapshots.update(
        {
            new_child.name: (new_child, old_child),
            new_metadata.name: (new_metadata, old_metadata),
        }
    )
    context_diff.snapshots.update(
        {new_child.snapshot_id: new_child, new_metadata.snapshot_id: new_metadata}
    )
    assert context_diff.indirectly_modified(new_child.name)
    assert context_diff.metadata_updated(new_metadata.name)
    if indirect_only:
        del context_diff.modified_snapshots[current.name]

    console = MarkdownConsole()
    console.show_model_difference_summary(
        context_diff, EnvironmentNamingInfo(), None, no_diff=False
    )
    output = console.consume_captured_output()
    assert "**Indirectly Modified:**\n- `test.child` (Indirect Non-breaking)" in output
    assert "**Metadata Updated:**\n- `test.metadata`" in output
    if indirect_only:
        assert "**Directly Modified:**" not in output
        assert "```" not in output
    else:
        assert "  Indirectly Modified Children:\n    - `test.child`" in output
        assert output.index("  ```diff") < output.index("  Indirectly Modified Children:")


@pytest.fixture
def row_diff():
    import pandas as pd

    return RowDiff(
        source="source",
        target="target",
        stats={
            "s_count": 2,
            "t_count": 2,
            "distinct_count_s": 2,
            "distinct_count_t": 2,
            "join_count": 2,
            "full_match_count": 0,
            "s_only_count": 0,
            "t_only_count": 0,
            "null_grain_count": 0,
        },
        column_stats=pd.DataFrame(
            {"pct_match": [50.0, 50.0, 100.0]}, index=["early", "late", "unchanged"]
        ),
        sample=pd.DataFrame(),
        joined_sample=pd.DataFrame(
            {"id": [1], "s__early": [10], "t__early": [99], "s__late": [20], "t__late": [20]}
        ),
        s_sample=pd.DataFrame(),
        t_sample=pd.DataFrame(),
    )


@pytest.mark.parametrize("console_type", [TerminalConsole, MarkdownConsole])
def test_show_row_diff_omits_empty_column_samples(row_diff, console_type):
    output = StringIO()
    console = console_type()
    console.console = Console(file=output, width=120)

    console.show_row_diff(row_diff)

    rendered = output.getvalue()
    assert "Column: early" in rendered
    assert "99" in rendered
    assert "Column: late" not in rendered
    assert "Column: unchanged" not in rendered
    assert "early" in rendered and "late" in rendered and "unchanged" in rendered
    assert "50.0" in rendered and "100.0" in rendered
    assert "PARTIAL MATCH: 2 rows (100.0%)" in rendered


@pytest.mark.parametrize("dtype", ["object", "Int64", "boolean", "float64"])
def test_show_row_diff_omits_equal_null_samples(row_diff, dtype):
    import pandas as pd

    nulls = pd.Series([float("nan")] if dtype == "float64" else [None], dtype=dtype)
    row_diff = row_diff.copy(
        update={"joined_sample": row_diff.joined_sample.assign(s__late=nulls, t__late=nulls.copy())}
    )
    output = StringIO()

    TerminalConsole(console=Console(file=output)).show_row_diff(row_diff)

    assert "Column: early" in output.getvalue()
    assert "Column: late" not in output.getvalue()


def test_show_row_diff_preserves_null_differences_aliases_keys_and_inputs(row_diff):
    import pandas as pd

    row_diff = row_diff.copy(
        update={
            "source_alias": "dev",
            "target_alias": "prod",
            "stats": {
                **row_diff.stats,
                "s_count": 3,
                "t_count": 3,
                "distinct_count_s": 3,
                "distinct_count_t": 3,
                "s_only_count": 1,
                "t_only_count": 1,
            },
            "joined_sample": pd.DataFrame(
                {
                    "id": [1, 2],
                    "region": ["east", "west"],
                    "DEV__early": pd.Series([10, None], dtype="Int64"),
                    "PROD__early": pd.Series([None, 20], dtype="Int64"),
                    "DEV__late": pd.Series([True, None], dtype="boolean"),
                    "PROD__late": pd.Series([True, None], dtype="boolean"),
                }
            ),
            "s_sample": pd.DataFrame({"id": [3], "region": ["source-only"]}),
            "t_sample": pd.DataFrame({"id": [4], "region": ["target-only"]}),
        }
    )
    original_stats = row_diff.stats.copy()
    frames = {
        name: getattr(row_diff, name).copy(deep=True)
        for name in ("sample", "joined_sample", "s_sample", "t_sample", "column_stats")
    }
    output = StringIO()

    TerminalConsole(console=Console(file=output, width=120)).show_row_diff(row_diff)

    rendered = output.getvalue()
    assert "Column: early" in rendered
    assert "Column: late" not in rendered
    assert all(label in rendered for label in ("id", "region", "DEV", "PROD"))
    assert all(value in rendered for value in ("east", "west", "10", "20", "<NA>"))
    assert "DEV ONLY: 1 rows" in rendered and "PROD ONLY: 1 rows" in rendered
    assert "DEV ONLY sample rows:" in rendered and "source-only" in rendered
    assert "PROD ONLY sample rows:" in rendered and "target-only" in rendered
    assert row_diff.stats == original_stats
    for name, frame in frames.items():
        pd.testing.assert_frame_equal(getattr(row_diff, name), frame)


def test_show_row_diff_without_samples(row_diff):
    output = StringIO()

    TerminalConsole(console=Console(file=output)).show_row_diff(row_diff, show_sample=False)

    rendered = output.getvalue()
    assert "PARTIAL MATCH: 2 rows (100.0%)" in rendered
    assert "column comparison stats:" in rendered
    assert "late" in rendered and "unchanged" in rendered
    assert "sample" not in rendered
    assert "Column:" not in rendered


def test_show_row_diff_without_joined_sample(row_diff):
    row_diff = row_diff.copy(
        update={
            "joined_sample": row_diff.joined_sample.iloc[:0],
            "stats": {**row_diff.stats, "full_match_count": 2},
        }
    )
    output = StringIO()

    TerminalConsole(console=Console(file=output)).show_row_diff(row_diff)

    rendered = output.getvalue()
    assert "FULL MATCH: 2 rows (100.0%)" in rendered
    assert "All joined rows match" in rendered
    assert "Column:" not in rendered


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
