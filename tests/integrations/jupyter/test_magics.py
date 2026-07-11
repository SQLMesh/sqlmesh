import logging
import pathlib
import typing as t
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup
import time_machine
from hyperscript import h
from IPython.core.error import UsageError
from IPython.testing.globalipapp import start_ipython
from IPython.utils.capture import CapturedIO, capture_output
from pytest_mock.plugin import MockerFixture
from rich.console import Console as RichConsole

from sqlmesh import Context, RuntimeEnv
from sqlmesh.magics import register_magics
from pathlib import Path

logger = logging.getLogger(__name__)


SUSHI_EXAMPLE_PATH = pathlib.Path("./examples/sushi")
SUCCESS_STYLE = "color: #008000; text-decoration-color: #008000"
NEUTRAL_STYLE = "color: #008080; text-decoration-color: #008080"
BOLD_ONLY = "font-weight: bold"
BOLD_NEUTRAL_STYLE = f"{NEUTRAL_STYLE}; {BOLD_ONLY}"
BOLD_SUCCESS_STYLE = f"{SUCCESS_STYLE}; {BOLD_ONLY}"
RICH_PRE_STYLE = "white-space:pre;overflow-x:auto;line-height:normal;font-family:Menlo,'DejaVu Sans Mono',consolas,'Courier New',monospace"
FREEZE_TIME = "2023-01-01 00:00:00 UTC"

pytestmark = pytest.mark.jupyter


@pytest.fixture(scope="session")
def ip():
    logger.root.handlers.clear()
    return start_ipython()


@pytest.fixture
def notebook(mocker: MockerFixture, ip):
    mocker.patch("sqlmesh.RuntimeEnv.get", MagicMock(side_effect=lambda: RuntimeEnv.JUPYTER))
    mocker.patch(
        "sqlmesh.core.console.RichConsole",
        MagicMock(return_value=RichConsole(force_jupyter=True, color_system="truecolor")),
    )
    register_magics()
    return ip


@pytest.fixture
def sushi_context(copy_to_temp_path, notebook, tmp_path) -> Context:
    sushi_dir = copy_to_temp_path(SUSHI_EXAMPLE_PATH)[0]
    notebook.run_line_magic(
        magic_name="context", line=f"{str(sushi_dir)} --log-file-dir {tmp_path}"
    )
    return notebook.user_ns["context"]


@pytest.fixture
@time_machine.travel(FREEZE_TIME)
def loaded_sushi_context(sushi_context) -> Context:
    with capture_output():
        sushi_context.plan(no_prompts=True, auto_apply=True)
    return sushi_context


@pytest.fixture
def convert_all_html_output_to_text():
    def _convert(output: CapturedIO) -> t.List[str]:
        return [
            BeautifulSoup(output.data["text/html"]).get_text().strip() for output in output.outputs
        ]

    return _convert


@pytest.fixture
def convert_all_html_output_to_tags():
    def _convert_html_to_tags(html: str) -> t.List[str]:
        # BS4 automatically adds html and body tags so we remove those since they are not actually part of the output
        return [
            tag.name  # type: ignore
            for tag in BeautifulSoup(html, "html").find_all()
            if tag.name not in {"html", "body"}  # type: ignore
        ]

    def _convert(output: CapturedIO) -> t.List[t.List[str]]:
        return [_convert_html_to_tags(output.data["text/html"]) for output in output.outputs]

    return _convert


@pytest.fixture
def get_all_html_output():
    def _convert(output: CapturedIO) -> t.List[str]:
        # We remove newlines to make it easier to compare the output to expected output
        return [output.data["text/html"].replace("\n", "") for output in output.outputs]

    return _convert


def test_context(notebook, convert_all_html_output_to_text, get_all_html_output, tmp_path):
    with capture_output() as output:
        notebook.run_line_magic(
            magic_name="context", line=f"{str(SUSHI_EXAMPLE_PATH)}  --log-file-dir {tmp_path}"
        )

    assert output.stdout == ""
    assert output.stderr == ""
    assert len(output.outputs) == 1
    sushi_path = str(Path("examples/sushi"))
    assert convert_all_html_output_to_text(output) == [
        f"SQLMesh project context set to: {sushi_path}"
    ]
    assert get_all_html_output(output) == [
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    f"SQLMesh project context set to: {sushi_path}",
                    autoescape=False,
                ),
                autoescape=False,
            )
        )
    ]


def test_init(tmp_path, notebook, convert_all_html_output_to_text, get_all_html_output):
    with pytest.raises(UsageError, match="the following arguments are required: path"):
        notebook.run_line_magic(magic_name="init", line="")
    with pytest.raises(UsageError, match="the following arguments are required: engine"):
        notebook.run_line_magic(magic_name="init", line="foo")
    with capture_output() as output:
        notebook.run_line_magic(magic_name="init", line=f"{tmp_path} duckdb")

    assert output.stdout == ""
    assert output.stderr == ""
    assert len(output.outputs) == 1
    assert convert_all_html_output_to_text(output) == ["SQLMesh project scaffold created"]
    assert get_all_html_output(output) == [
        str(
            h(
                "div",
                h(
                    "span",
                    {"style": "color: green; font-weight: bold"},
                    "SQLMesh project scaffold created",
                    autoescape=False,
                ),
                autoescape=False,
            )
        )
    ]


@pytest.mark.slow
def test_render(
    notebook, sushi_context, convert_all_html_output_to_text, convert_all_html_output_to_tags
):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="render", line="sushi.top_waiters")

    assert output.stdout == ""
    assert output.stderr == ""
    assert len(output.outputs) == 2
    assert len(convert_all_html_output_to_text(output)[1]) > 2200
    assert len(convert_all_html_output_to_tags(output)[1]) > 150


@pytest.mark.slow
def test_render_no_format(
    notebook, sushi_context, convert_all_html_output_to_text, convert_all_html_output_to_tags
):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="render", line="sushi.top_waiters --no-format")

    assert output.stdout == ""
    assert output.stderr == ""
    assert len(output.outputs) == 2
    assert len(convert_all_html_output_to_text(output)[1]) >= 700
    assert len(convert_all_html_output_to_tags(output)[1]) >= 50


@pytest.mark.slow
@time_machine.travel(FREEZE_TIME)
def test_evaluate(notebook, loaded_sushi_context):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="evaluate", line="sushi.top_waiters")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 1
    # TODO: Add console method to display pandas dataframe and then use that instead of calling display directly
    assert not output.outputs[0].data.get("text/html")
    # Since there is no ordering in the query, we can't assert the exact output, but we can assert the columns
    # and it starts with the index
    assert output.outputs[0].data["text/plain"].startswith("   waiter_id  revenue\n0")


def test_format(notebook, sushi_context):
    with capture_output():
        test_model_path = sushi_context.path / "models" / "test_model.sql"
        test_model_path.write_text("MODEL(name db.test); SELECT 1 AS foo FROM t")
        sushi_context.load()
    assert test_model_path.read_text() == "MODEL(name db.test); SELECT 1 AS foo FROM t"
    with capture_output() as output:
        notebook.run_line_magic(magic_name="format", line="")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 0
    assert (
        test_model_path.read_text()
        == """MODEL (
  name db.test
);

SELECT
  1 AS foo
FROM t"""
    )


@pytest.mark.slow
def test_diff(sushi_context, notebook, convert_all_html_output_to_text, get_all_html_output):
    with capture_output():
        test_model_path = sushi_context.path / "models" / "test_model.sql"
        test_model_path.write_text("MODEL(name sqlmesh_example.test); SELECT 1 AS foo")
        sushi_context.load()
        notebook.run_line_magic(magic_name="plan", line="--no-prompts --auto-apply")
    test_model_path.write_text("MODEL(name sqlmesh_example.test); SELECT 1 AS foo, 2 AS bar")
    with capture_output() as output:
        notebook.run_line_magic(magic_name="diff", line="prod")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 2
    assert convert_all_html_output_to_text(output) == [
        "Differences from the `prod` environment:",
        "Models:\n└── Directly Modified:\n    └── sqlmesh_example.test",
    ]
    assert get_all_html_output(output) == [
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                h(
                    "span",
                    {"style": "font-weight: bold"},
                    "Differences from the `prod` environment:",
                    autoescape=False,
                ),
                autoescape=False,
            )
        ),
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                str(
                    h(
                        "span",
                        {"style": "font-weight: bold"},
                        "Models:",
                        autoescape=False,
                    )
                )
                + "└── "
                + str(
                    h(
                        "span",
                        {"style": "font-weight: bold"},
                        "Directly Modified:",
                        autoescape=False,
                    )
                )
                + "    └── sqlmesh_example.test",
                autoescape=False,
            )
        ),
    ]


@pytest.mark.slow
def test_plan(
    notebook, sushi_context, convert_all_html_output_to_text, convert_all_html_output_to_tags
):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="plan", line="--no-prompts --auto-apply")

    assert not output.stderr
    assert len(output.outputs) == 4
    text_output = convert_all_html_output_to_text(output)
    # TODO: Is this what we expect?
    # This has minor differences between CI/CD and local.
    assert "[2K" in text_output[0]
    assert text_output[1].startswith(
        "Updating virtual layer  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0%"
    )
    # TODO: Is this what we expect?
    assert text_output[2] == ""
    assert text_output[3] == "✔ Virtual layer updated"
    assert convert_all_html_output_to_tags(output) == [
        ["pre", "span"],
        ["pre"] + ["span"] * 5,
        ["pre"],
        ["pre", "span"],
    ]


@pytest.mark.slow
@time_machine.travel("2023-01-03 00:00:00 UTC")
def test_run_dag(
    notebook, loaded_sushi_context, convert_all_html_output_to_text, get_all_html_output
):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="run_dag", line="")

    assert not output.stdout.startswith(
        "'Executing model batches ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 18/18"
    )
    assert not output.stderr

    # At least 4 outputs expected as the number of models in the particular batch might vary
    assert len(output.outputs) >= 4

    html_text_actual = convert_all_html_output_to_text(output)

    # Check for key elements in the output
    assert any("[2K" in text for text in html_text_actual)
    assert any("Executing model batches" in text for text in html_text_actual)
    assert any("✔ Model batches executed" in text for text in html_text_actual)
    assert any("Run finished for environment 'prod'" in text for text in html_text_actual)

    # Check the final messages
    final_outputs = [text for text in html_text_actual if text.strip()]
    assert final_outputs[-2] == "✔ Model batches executed"
    assert final_outputs[-1] == "Run finished for environment 'prod'"

    actual_html_output = get_all_html_output(output)
    # Replace dynamic elapsed time with 00
    for i, chunk in enumerate(actual_html_output):
        pattern = r'font-weight: bold">0.</span>\d{2}s   </pre>'
        import re

        actual_html_output[i] = re.sub(pattern, 'font-weight: bold">0.</span>00s   </pre>', chunk)
    expected_html_output = [
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                "\x1b",
                h(
                    "span",
                    {"style": BOLD_ONLY},
                    "[",
                    autoescape=False,
                ),
                "2K",
                autoescape=False,
            )
        ),
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                h(
                    "span",
                    {"style": "color: #000080; text-decoration-color: #000080; font-weight: bold"},
                    "Executing model batches",
                    autoescape=False,
                ),
                " ",
                h(
                    "span",
                    {"style": "color: #f92672; text-decoration-color: #f92672"},
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╸",
                    autoescape=False,
                ),
                h(
                    "span",
                    {"style": "color: #3a3a3a; text-decoration-color: #3a3a3a"},
                    "━━",
                    autoescape=False,
                ),
                " ",
                h(
                    "span",
                    {"style": "color: #800080; text-decoration-color: #800080"},
                    "93.8%",
                    autoescape=False,
                ),
                " • ",
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    "15/16",
                    autoescape=False,
                ),
                " • ",
                h(
                    "span",
                    {"style": "color: #808000; text-decoration-color: #808000"},
                    "0:00:00",
                    autoescape=False,
                ),
                "sushi.waiter_as_customer_by_day ",
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    ".. ",
                    autoescape=False,
                ),
                "                                                     ",
                autoescape=False,
            )
        ),
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                h(
                    "span",
                    {"style": BOLD_ONLY},
                    "[",
                    autoescape=False,
                ),
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "1",
                    autoescape=False,
                ),
                "/",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "1",
                    autoescape=False,
                ),
                h(
                    "span",
                    {"style": BOLD_ONLY},
                    "]",
                    autoescape=False,
                ),
                " sushi.waiter_as_customer_by_day   ",
                h(
                    "span",
                    {"style": BOLD_ONLY},
                    "[",
                    autoescape=False,
                ),
                "insert ",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "2023",
                    autoescape=False,
                ),
                "-",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "01",
                    autoescape=False,
                ),
                "-",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "01",
                    autoescape=False,
                ),
                " - ",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "2023",
                    autoescape=False,
                ),
                "-",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "01",
                    autoescape=False,
                ),
                "-",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "02",
                    autoescape=False,
                ),
                ", audits ",
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    "✔",
                    autoescape=False,
                ),
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "2",
                    autoescape=False,
                ),
                h(
                    "span",
                    {"style": BOLD_ONLY},
                    "]",
                    autoescape=False,
                ),
                "   ",
                h(
                    "span",
                    {"style": BOLD_NEUTRAL_STYLE},
                    "0.",
                    autoescape=False,
                ),
                "00s   ",
                autoescape=False,
            )
        ),
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                "",
                autoescape=False,
            )
        ),
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    "✔ Model batches executed",
                    autoescape=False,
                ),
                autoescape=False,
            )
        ),
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    "Run finished for environment ",
                    autoescape=False,
                ),
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    "'prod'",
                    autoescape=False,
                ),
                autoescape=False,
            )
        ),
    ]


@pytest.mark.slow
@time_machine.travel(FREEZE_TIME)
def test_invalidate(
    notebook, loaded_sushi_context, convert_all_html_output_to_text, get_all_html_output
):
    with capture_output():
        notebook.run_line_magic(
            magic_name="plan", line="dev --include-unmodified --no-prompts --auto-apply"
        )
    with capture_output() as output:
        notebook.run_line_magic(magic_name="invalidate", line="dev")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 1
    assert convert_all_html_output_to_text(output) == [
        "Environment 'dev' invalidated.",
    ]
    assert get_all_html_output(output) == [
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                str(
                    h(
                        "span",
                        {"style": SUCCESS_STYLE},
                        "Environment ",
                        autoescape=False,
                    )
                )
                + str(
                    h(
                        "span",
                        {"style": SUCCESS_STYLE},
                        "'dev'",
                        autoescape=False,
                    )
                )
                + str(
                    h(
                        "span",
                        {"style": SUCCESS_STYLE},
                        " invalidated.",
                        autoescape=False,
                    )
                ),
                autoescape=False,
            )
        )
    ]


@pytest.mark.slow
def test_janitor(
    notebook, loaded_sushi_context, convert_all_html_output_to_text, get_all_html_output
):
    with capture_output():
        notebook.run_line_magic(
            magic_name="plan", line="dev --include-unmodified --no-prompts --auto-apply"
        )
        notebook.run_line_magic(magic_name="invalidate", line="dev")

    with capture_output() as output:
        notebook.run_line_magic(magic_name="janitor", line="")

    assert not output.stdout
    assert not output.stderr
    assert convert_all_html_output_to_text(output) == [
        "Deleted object memory.sushi__dev",
        "Cleanup complete.",
    ]


def test_dag(tmp_path_factory, notebook, sushi_context):
    temp_dir = tmp_path_factory.mktemp("dag")
    dag_file = temp_dir / "dag.html"
    with capture_output() as output:
        notebook.run_line_magic(magic_name="dag", line=f"--file {str(dag_file)}")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 1
    output_plain = output.outputs[0].data["text/plain"]
    # When we capture output it just captures the repr of the class, but it gets displayed fine in a notebook
    assert "GraphHTML" in output_plain
    file_contents = dag_file.read_text()
    assert '<div id="sqlglot-lineage">' in file_contents
    assert "waiter_revenue_by_day" in file_contents


def test_create_test(notebook, sushi_context):
    with capture_output():
        notebook.run_line_magic(
            magic_name="create_test",
            line='''sushi.top_waiters --query sushi.waiter_revenue_by_day "SELECT 1 as waiter_id"''',
        )

    test_file = sushi_context.path / "tests" / "test_top_waiters.yaml"
    assert test_file.exists()
    assert (
        test_file.read_text()
        == """test_top_waiters:
  model: '"memory"."sushi"."top_waiters"'
  inputs:
    '"memory"."sushi"."waiter_revenue_by_day"':
    - waiter_id: 1
  outputs:
    query: []
"""
    )


def test_test(notebook, sushi_context):
    """
    Not sure how to test the cell contents. If we can figure that out, then it would be great to test
    starting with a line magic, output the test to the cell, update cell contents, and then run the cell
    and verify the new contents get written to the file. That is a common workflow a user would go through.

    Also easier test is testing the ls command
    """
    with capture_output() as output:
        notebook.run_cell_magic(
            magic_name="test",
            line="sushi.customer_revenue_by_day test_customer_revenue_by_day",
            cell="TESTING",
        )
    assert not output.stdout
    assert not output.stderr
    assert not output.outputs
    test_file = sushi_context.path / "tests" / "test_customer_revenue_by_day.yaml"
    assert test_file.exists()
    assert test_file.read_text() == """test_customer_revenue_by_day: TESTING\n"""


@pytest.mark.slow
def test_audit(notebook, loaded_sushi_context, convert_all_html_output_to_text):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="audit", line="sushi.top_waiters")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 4
    assert convert_all_html_output_to_text(output) == [
        "Found 1 audit(s).",
        "unique_values on model sushi.top_waiters ✅ PASS.",
        "Finished with 0 audit errors and 0 audits skipped.",
        "Done.",
    ]


def test_fetchdf(notebook, sushi_context):
    with capture_output() as output:
        notebook.run_cell_magic(magic_name="fetchdf", line="my_result", cell="SELECT 1 AS foo")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 1
    assert output.outputs[0].data["text/plain"] == """   foo\n0    1"""
    assert notebook.user_ns["my_result"].to_dict() == {"foo": {0: 1}}


def test_info(notebook, sushi_context, convert_all_html_output_to_text, get_all_html_output):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="info", line="--verbose")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 6
    assert convert_all_html_output_to_text(output) == [
        "Models: 20",
        "Macros: 8",
        "",
        "Connection:\n  type: duckdb\n  concurrent_tasks: 1\n  register_comments: true\n  pre_ping: false\n  pretty_sql: false\n  extensions: []\n  connector_config: {}\n  secrets: None\n  filesystems: []\n  shared_connection: true",
        "Test Connection:\n  type: duckdb\n  concurrent_tasks: 1\n  register_comments: true\n  pre_ping: false\n  pretty_sql: false\n  extensions: []\n  connector_config: {}\n  secrets: None\n  filesystems: []\n  shared_connection: true",
        "Data warehouse connection succeeded",
    ]
    assert get_all_html_output(output) == [
        "<pre style=\"white-space:pre;overflow-x:auto;line-height:normal;font-family:Menlo,'DejaVu Sans Mono',consolas,'Courier New',monospace\">Models: <span style=\"color: #008080; text-decoration-color: #008080; font-weight: bold\">20</span></pre>",
        "<pre style=\"white-space:pre;overflow-x:auto;line-height:normal;font-family:Menlo,'DejaVu Sans Mono',consolas,'Courier New',monospace\">Macros: <span style=\"color: #008080; text-decoration-color: #008080; font-weight: bold\">8</span></pre>",
        "<pre style=\"white-space:pre;overflow-x:auto;line-height:normal;font-family:Menlo,'DejaVu Sans Mono',consolas,'Courier New',monospace\"></pre>",
        '<pre style="white-space:pre;overflow-x:auto;line-height:normal;font-family:Menlo,\'DejaVu Sans Mono\',consolas,\'Courier New\',monospace">Connection:  type: duckdb  concurrent_tasks: <span style="color: #008080; text-decoration-color: #008080; font-weight: bold">1</span>  register_comments: true  pre_ping: false  pretty_sql: false  extensions: <span style="font-weight: bold">[]</span>  connector_config: <span style="font-weight: bold">{}</span>  secrets: <span style="color: #800080; text-decoration-color: #800080; font-style: italic">None</span>  filesystems: <span style="font-weight: bold">[]</span>  shared_connection: true</pre>',
        '<pre style="white-space:pre;overflow-x:auto;line-height:normal;font-family:Menlo,\'DejaVu Sans Mono\',consolas,\'Courier New\',monospace">Test Connection:  type: duckdb  concurrent_tasks: <span style="color: #008080; text-decoration-color: #008080; font-weight: bold">1</span>  register_comments: true  pre_ping: false  pretty_sql: false  extensions: <span style="font-weight: bold">[]</span>  connector_config: <span style="font-weight: bold">{}</span>  secrets: <span style="color: #800080; text-decoration-color: #800080; font-style: italic">None</span>  filesystems: <span style="font-weight: bold">[]</span>  shared_connection: true</pre>',
        "<pre style=\"white-space:pre;overflow-x:auto;line-height:normal;font-family:Menlo,'DejaVu Sans Mono',consolas,'Courier New',monospace\">Data warehouse connection <span style=\"color: #008000; text-decoration-color: #008000\">succeeded</span></pre>",
    ]


@pytest.mark.slow
def test_migrate(
    notebook, loaded_sushi_context, convert_all_html_output_to_text, get_all_html_output
):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="migrate", line="")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 1
    assert convert_all_html_output_to_text(output) == [
        "Migration complete",
    ]
    assert get_all_html_output(output) == [
        str(
            h(
                "pre",
                {"style": RICH_PRE_STYLE},
                h(
                    "span",
                    {"style": SUCCESS_STYLE},
                    "Migration complete",
                    autoescape=False,
                ),
                autoescape=False,
            )
        )
    ]


# TODO: Add test for rollback
# def test_rollback(notebook, loaded_sushi_context):
#     with capture_output() as output:
#         notebook.run_line_magic(magic_name="rollback", line="")
#


@pytest.mark.slow
def test_create_external_models(notebook, loaded_sushi_context, convert_all_html_output_to_text):
    external_model_file = loaded_sushi_context.path / "external_models.yaml"
    external_model_file.unlink()
    assert not external_model_file.exists()
    loaded_sushi_context.load()
    with capture_output() as output:
        notebook.run_line_magic(magic_name="create_external_models", line="")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 2
    converted = sorted(convert_all_html_output_to_text(output))
    assert 'Unable to get schema for \'"memory"."raw"."model1"\'' in converted[0]
    assert 'Unable to get schema for \'"memory"."raw"."model2"\'' in converted[1]

    assert external_model_file.exists()
    assert (
        external_model_file.read_text()
        == """- name: '"memory"."raw"."demographics"'
  columns:
    customer_id: INT
    zip: TEXT
  gateway: duckdb
"""
    )


@pytest.mark.slow
@time_machine.travel(FREEZE_TIME)
def test_table_diff(notebook, loaded_sushi_context, convert_all_html_output_to_text):
    with capture_output():
        loaded_sushi_context.plan("dev", no_prompts=True, auto_apply=True, include_unmodified=True)
    with capture_output() as output:
        notebook.run_line_magic(magic_name="table_diff", line="dev:prod --model sushi.top_waiters")

    assert not output.stdout
    assert not output.stderr

    assert len(output.outputs) == 1
    assert convert_all_html_output_to_text(output) == [
        "No models contain differences with the selection criteria: 'sushi.top_waiters'"
    ]


@pytest.mark.slow
@time_machine.travel(FREEZE_TIME)
def test_table_name(notebook, loaded_sushi_context, convert_all_html_output_to_text):
    with capture_output() as output:
        notebook.run_line_magic(magic_name="table_name", line="sushi.orders")

    assert not output.stdout
    assert not output.stderr
    assert len(output.outputs) == 1
    assert convert_all_html_output_to_text(output)[0].startswith(
        "memory.sqlmesh__sushi.sushi__orders__"
    )


def test_lint(notebook, sushi_context):
    from sqlmesh.core.config import LinterConfig

    sushi_context.config.linter = LinterConfig(enabled=True, warn_rules="ALL")
    sushi_context.load()

    with capture_output() as output:
        notebook.run_line_magic(magic_name="lint", line="")

    assert len(output.outputs) > 1
    assert "Linter warnings for" in output.outputs[0].data["text/plain"]

    with capture_output() as output:
        notebook.run_line_magic(magic_name="lint", line="--models sushi.items")

    assert len(output.outputs) == 1
    assert "Linter warnings for" in output.outputs[0].data["text/plain"]

    with capture_output() as output:
        notebook.run_line_magic(magic_name="lint", line="--models sushi.items sushi.raw_marketing")

    assert len(output.outputs) == 2
    assert "Linter warnings for" in output.outputs[0].data["text/plain"]


@pytest.mark.slow
def test_destroy(
    notebook,
    loaded_sushi_context,
    convert_all_html_output_to_text,
    get_all_html_output,
    monkeypatch,
):
    # Mock input to return 'y' for the confirmation prompt
    monkeypatch.setattr("builtins.input", lambda: "y")

    with capture_output() as output:
        notebook.run_line_magic(magic_name="destroy", line="")

    assert not output.stdout
    assert not output.stderr
    text_output = convert_all_html_output_to_text(output)
    expected_messages = [
        "[WARNING] This will permanently delete all engine-managed objects, state tables and SQLMesh cache.\nThe operation may disrupt any currently running or scheduled plans.",
        "Schemas to be deleted:",
        "• memory.sushi",
        "Snapshot tables to be deleted:",
        "This action will DELETE ALL the above resources managed by SQLMesh AND\npotentially external resources created by other tools in these schemas.",
        "Are you ABSOLUTELY SURE you want to proceed with deletion? [y/n]:",
        "Environment 'prod' invalidated.",
        "Deleted object memory.sushi",
        "State tables removed.",
        "Destroy completed successfully.",
    ]
    for message in expected_messages:
        assert any(message in line for line in text_output)
