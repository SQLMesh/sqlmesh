import os
import pathlib
import stat

from pytest_mock.plugin import MockerFixture
from sqlmesh.core.config import Config
from sqlmesh.core.context import Context
from sqlmesh.core.dialect import parse
from sqlmesh.core.audit import ModelAudit
from sqlmesh.core.model import SqlModel, load_sql_based_model
from tests.utils.test_filesystem import create_temp_file
from unittest.mock import call
from sqlmesh.core.config import ModelDefaultsConfig


def test_format_files(tmp_path: pathlib.Path, mocker: MockerFixture):
    models_dir = pathlib.Path("models")
    audits_dir = pathlib.Path("audits")

    f1 = create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "model_1.sql"),
        "MODEL(name this.model, dialect 'duckdb'); SELECT 1 AS \"CaseSensitive\"",
    )
    f2 = create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "model_2.sql"),
        "MODEL(name other.model); SELECT 2 AS another_column",
    )
    f3 = create_temp_file(
        tmp_path,
        pathlib.Path(audits_dir, "audit_1.sql"),
        "AUDIT(name assert_positive_id, dialect 'duckdb'); SELECT  * FROM @this_model WHERE  \"CaseSensitive_item_id\" < 0;",
    )
    f4 = create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "model_3.sql"),
        "MODEL(name audit.model, audits (inline_audit)); SELECT 3 AS item_id; AUDIT(name inline_audit); SELECT  * FROM @this_model WHERE  item_id < 0;",
    )

    config = Config()
    context = Context(paths=tmp_path, config=config)
    context.console = mocker.Mock()
    context.load()

    assert isinstance(context.get_model("this.model"), SqlModel)
    assert isinstance(context.get_model("other.model"), SqlModel)
    assert isinstance(context.get_model("audit.model"), SqlModel)
    assert isinstance(context._audits["assert_positive_id"], ModelAudit)
    assert context.get_model("this.model").query.sql() == 'SELECT 1 AS "CaseSensitive"'  # type: ignore
    assert context.get_model("other.model").query.sql() == "SELECT 2 AS another_column"  # type: ignore
    assert context.get_model("audit.model").query.sql() == "SELECT 3 AS item_id"  # type: ignore
    assert (
        context.get_model("audit.model").audit_definitions["inline_audit"].query.sql()
        == "SELECT * FROM @this_model WHERE item_id < 0"
    )
    assert (
        context._audits["assert_positive_id"].query.sql()
        == 'SELECT * FROM @this_model WHERE "CaseSensitive_item_id" < 0'
    )

    assert not context.format(check=True)
    assert all(
        c in context.console.log_status_update.mock_calls  # type: ignore
        for c in [
            call(f"{tmp_path / 'models/model_3.sql'} needs reformatting."),
            call(f"{tmp_path / 'models/model_2.sql'} needs reformatting."),
            call(f"{tmp_path / 'models/model_1.sql'} needs reformatting."),
            call(f"{tmp_path / 'audits/audit_1.sql'} needs reformatting."),
            call("\n4 file(s) need reformatting."),
        ]
    )

    # Transpile project to BigQuery
    context.format(transpile="bigquery")

    # Ensure format check is successful
    assert context.format(transpile="bigquery", check=True)

    # Ensure transpilation success AND model specific dialect is mutated
    upd1 = f1.read_text(encoding="utf-8")
    assert (
        upd1
        == "MODEL (\n  name this.model,\n  dialect 'bigquery'\n);\n\nSELECT\n  1 AS `CaseSensitive`"
    )
    context.upsert_model(load_sql_based_model(parse(upd1, "bigquery"), default_catalog="memory"))
    assert context.models['"memory"."this"."model"'].dialect == "bigquery"

    # Ensure no dialect is added if it's not needed
    upd2 = f2.read_text(encoding="utf-8")
    assert upd2 == "MODEL (\n  name other.model\n);\n\nSELECT\n  2 AS another_column"

    # Ensure audit specific dialect is updated and formatting
    upd3 = f3.read_text(encoding="utf-8")
    assert (
        upd3
        == "AUDIT (\n  name assert_positive_id,\n  dialect 'bigquery'\n);\n\nSELECT\n  *\nFROM @this_model\nWHERE\n  `CaseSensitive_item_id` < 0"
    )

    # Ensure inline audit is formatted within model definition
    upd4 = f4.read_text(encoding="utf-8")
    assert (
        upd4
        == "MODEL (\n  name audit.model,\n  audits (\n    inline_audit\n  )\n);\n\nSELECT\n  3 AS item_id;\n\nAUDIT (\n  name inline_audit\n);\n\nSELECT\n  *\nFROM @this_model\nWHERE\n  item_id < 0"
    )


def test_ignore_formating_files(tmp_path: pathlib.Path):
    models_dir = pathlib.Path("models")
    audits_dir = pathlib.Path("audits")

    # Case 1: Model and Audit are not formatted if the flag is set to false (overriding defaults)
    model1_text = "MODEL(name this.model1, dialect 'duckdb', formatting false); SELECT 1 col"
    model1 = create_temp_file(tmp_path, pathlib.Path(models_dir, "model_1.sql"), model1_text)

    audit1_text = "AUDIT(name audit1, dialect 'duckdb', formatting false); SELECT col1 col2 FROM @this_model WHERE     foo < 0;"
    audit1 = create_temp_file(tmp_path, pathlib.Path(audits_dir, "audit_1.sql"), audit1_text)

    audit2_text = "AUDIT(name audit2, dialect 'duckdb', standalone true, formatting false); SELECT col1 col2 FROM @this_model WHERE     foo < 0;"
    audit2 = create_temp_file(tmp_path, pathlib.Path(audits_dir, "audit_2.sql"), audit2_text)

    Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(formatting=True))
    ).format()

    assert model1.read_text(encoding="utf-8") == model1_text
    assert audit1.read_text(encoding="utf-8") == audit1_text
    assert audit2.read_text(encoding="utf-8") == audit2_text

    # Case 2: Model is formatted (or not) based on it's flag and the defaults flag
    model2_text = "MODEL(name this.model2, dialect 'duckdb'); SELECT 1 col"
    model2 = create_temp_file(tmp_path, pathlib.Path(models_dir, "model_2.sql"), model2_text)

    model3_text = "MODEL(name this.model3, dialect 'duckdb', formatting true); SELECT 1 col"
    model3 = create_temp_file(tmp_path, pathlib.Path(models_dir, "model_3.sql"), model3_text)

    Context(
        paths=tmp_path, config=Config(model_defaults=ModelDefaultsConfig(formatting=False))
    ).format()

    # Case 2.1: Model is not formatted if the defaults flag is set to false
    assert model2.read_text(encoding="utf-8") == model2_text

    # Case 2.2: Model is formatted if it's flag is set to true, overriding defaults
    assert (
        model3.read_text(encoding="utf-8")
        == "MODEL (\n  name this.model3,\n  dialect 'duckdb',\n  formatting TRUE\n);\n\nSELECT\n  1 AS col"
    )


def test_format_check_read_only_files(tmp_path: pathlib.Path, mocker: MockerFixture):
    models_dir = pathlib.Path("models")

    model_text = "MODEL(name this.model, dialect 'duckdb'); SELECT 1 AS col"
    model = create_temp_file(
        tmp_path,
        pathlib.Path(models_dir, "model.sql"),
        model_text,
    )
    os.chmod(model, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    context = Context(paths=tmp_path, config=Config())
    context.console = mocker.Mock()
    context.load()

    assert not context.format(check=True)
    assert model.read_text(encoding="utf-8") == model_text


def test_format_without_state_load(tmp_path: pathlib.Path, mocker: MockerFixture):
    mock = mocker.patch(
        "sqlmesh.core.state_sync.db.facade.EngineAdapterStateSync.get_versions",
        side_effect=RuntimeError("state should not be accessed"),
    )

    create_temp_file(
        tmp_path,
        pathlib.Path("models/example.sql"),
        "MODEL(name local.example, dialect 'duckdb'); SELECT 1 AS col",
    )

    context = Context(paths=tmp_path, config=Config(project="local_only"), load_state=False)
    context.format(check=True)
    mock.assert_not_called()


def _format_project(tmp_path: pathlib.Path) -> dict:
    """A project with a model, an audit, a standalone audit, a macro and a python model."""
    files = {
        "models/model_1.sql": "MODEL(name this.model1, dialect 'duckdb'); SELECT 1 AS   col",
        "models/model_2.sql": "MODEL(name this.model2, dialect 'duckdb'); SELECT 2 AS   col",
        "models/model_3.py": "# python model, must be left alone\n",
        "audits/audit_1.sql": "AUDIT(name audit1, dialect 'duckdb'); SELECT  * FROM @this_model WHERE   col < 0;",
        "macros/macro_1.sql": "SELECT   1",
    }
    created = {}
    for rel, text in files.items():
        created[rel] = create_temp_file(tmp_path, pathlib.Path(rel), text)
    return created


def test_format_paths_does_not_load_project(tmp_path: pathlib.Path, mocker: MockerFixture):
    """`sqlmesh format models/a.sql` must not parse unrelated models."""
    files = _format_project(tmp_path)
    before_2 = files["models/model_2.sql"].read_text(encoding="utf-8")

    context = Context(paths=tmp_path, config=Config(), load=False)
    load_spy = mocker.spy(Context, "load")

    assert context.format(paths=(str(files["models/model_1.sql"]),))

    load_spy.assert_not_called()
    assert not context._models
    # The selected model is formatted even though nothing was loaded.
    assert files["models/model_1.sql"].read_text(encoding="utf-8") == (
        "MODEL (\n  name this.model1,\n  dialect 'duckdb'\n);\n\nSELECT\n  1 AS col"
    )
    # The unselected model is untouched.
    assert files["models/model_2.sql"].read_text(encoding="utf-8") == before_2


def test_format_paths_output_matches_loaded_format(tmp_path: pathlib.Path):
    """The path-based formatter must produce exactly what the loading one produces."""
    loaded_path = tmp_path / "loaded"
    unloaded_path = tmp_path / "unloaded"
    loaded = _format_project(loaded_path)
    unloaded = _format_project(unloaded_path)

    Context(paths=loaded_path, config=Config()).format()

    context = Context(paths=unloaded_path, config=Config(), load=False)
    context.format(paths=(str(unloaded["models/model_1.sql"]), str(unloaded["audits/audit_1.sql"])))

    for rel in ("models/model_1.sql", "audits/audit_1.sql"):
        assert unloaded[rel].read_text(encoding="utf-8") == loaded[rel].read_text(encoding="utf-8")


def test_format_paths_ignores_non_model_sql(tmp_path: pathlib.Path):
    """macros/*.sql and other non-model SQL are a no-op, same as today."""
    files = _format_project(tmp_path)
    macro_before = files["macros/macro_1.sql"].read_text(encoding="utf-8")
    model_before = files["models/model_1.sql"].read_text(encoding="utf-8")

    context = Context(paths=tmp_path, config=Config(), load=False)
    context.format(paths=(str(files["macros/macro_1.sql"]), str(files["models/model_1.sql"])))

    assert files["macros/macro_1.sql"].read_text(encoding="utf-8") == macro_before
    # Guard against the assertion above passing simply because nothing was formatted.
    assert files["models/model_1.sql"].read_text(encoding="utf-8") != model_before


def test_format_paths_ignores_python_models(tmp_path: pathlib.Path):
    files = _format_project(tmp_path)
    before = files["models/model_3.py"].read_text(encoding="utf-8")

    context = Context(paths=tmp_path, config=Config(), load=False)
    context.format(paths=(str(files["models/model_3.py"]), str(files["models/model_1.sql"])))

    assert files["models/model_3.py"].read_text(encoding="utf-8") == before
    # Guard against the assertion above passing simply because nothing was formatted.
    assert "1 AS col" in files["models/model_1.sql"].read_text(encoding="utf-8")


def test_format_paths_honors_formatting_false(tmp_path: pathlib.Path):
    text = "MODEL(name this.model, dialect 'duckdb', formatting false); SELECT 1 AS   col"
    model = create_temp_file(tmp_path, pathlib.Path("models/model.sql"), text)

    other = create_temp_file(
        tmp_path,
        pathlib.Path("models/other.sql"),
        "MODEL(name this.other, dialect 'duckdb'); SELECT 1 AS   col",
    )
    context = Context(paths=tmp_path, config=Config(), load=False)
    context.format(paths=(str(model), str(other)))

    assert model.read_text(encoding="utf-8") == text
    # Guard against the assertion above passing simply because nothing was formatted.
    assert (
        other.read_text(encoding="utf-8")
        != "MODEL(name this.other, dialect 'duckdb'); SELECT 1 AS   col"
    )


def test_format_paths_honors_model_defaults_formatting_false(tmp_path: pathlib.Path):
    text = "MODEL(name this.model, dialect 'duckdb'); SELECT 1 AS   col"
    model = create_temp_file(tmp_path, pathlib.Path("models/model.sql"), text)

    override = create_temp_file(
        tmp_path,
        pathlib.Path("models/override.sql"),
        "MODEL(name this.override, dialect 'duckdb', formatting true); SELECT 1 AS   col",
    )
    context = Context(
        paths=tmp_path,
        config=Config(model_defaults=ModelDefaultsConfig(formatting=False)),
        load=False,
    )
    context.format(paths=(str(model), str(override)))

    assert model.read_text(encoding="utf-8") == text
    # An explicit `formatting true` in the header still overrides the default.
    assert override.read_text(encoding="utf-8") == (
        "MODEL (\n  name this.override,\n  dialect 'duckdb',\n  formatting TRUE\n);"
        "\n\nSELECT\n  1 AS col"
    )


def test_format_paths_check_reports_unformatted(tmp_path: pathlib.Path, mocker: MockerFixture):
    files = _format_project(tmp_path)
    before = files["models/model_1.sql"].read_text(encoding="utf-8")

    context = Context(paths=tmp_path, config=Config(), load=False)
    context.console = mocker.Mock()

    assert not context.format(paths=(str(files["models/model_1.sql"]),), check=True)
    # check must not rewrite the file
    assert files["models/model_1.sql"].read_text(encoding="utf-8") == before


def test_format_without_paths_still_loads(tmp_path: pathlib.Path):
    """No paths means the whole project is formatted, unchanged."""
    files = _format_project(tmp_path)

    context = Context(paths=tmp_path, config=Config(), load=False)
    context.format()

    assert context._loaded
    assert files["models/model_1.sql"].read_text(encoding="utf-8") == (
        "MODEL (\n  name this.model1,\n  dialect 'duckdb'\n);\n\nSELECT\n  1 AS col"
    )
    assert files["models/model_2.sql"].read_text(encoding="utf-8") == (
        "MODEL (\n  name this.model2,\n  dialect 'duckdb'\n);\n\nSELECT\n  2 AS col"
    )


def test_format_paths_formats_standalone_audits(tmp_path: pathlib.Path):
    """Standalone audits are skipped by the project-wide format; by path they are formatted."""
    text = (
        "AUDIT(name sa, dialect 'duckdb', standalone true); SELECT 1 AS   c FROM t WHERE   c < 0;"
    )
    audit = create_temp_file(tmp_path, pathlib.Path("audits/standalone.sql"), text)

    Context(paths=tmp_path, config=Config()).format()
    assert audit.read_text(encoding="utf-8") == text, "project-wide format skips standalone audits"

    context = Context(paths=tmp_path, config=Config(), load=False)
    context.format(paths=(str(audit),))
    assert audit.read_text(encoding="utf-8") != text


def test_format_paths_honors_string_model_defaults_formatting(tmp_path: pathlib.Path):
    """A configured `formatting: 'false'` string is coerced the way the model validator does."""
    text = "MODEL(name this.model, dialect 'duckdb'); SELECT 1 AS   col"
    model = create_temp_file(tmp_path, pathlib.Path("models/model.sql"), text)

    context = Context(
        paths=tmp_path,
        config=Config(model_defaults=ModelDefaultsConfig(formatting="false")),
        load=False,
    )
    context.format(paths=(str(model),))

    assert model.read_text(encoding="utf-8") == text
