from __future__ import annotations

import threading
from pathlib import Path

import pyarrow as pa  # type: ignore
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pytest_mock.plugin import MockerFixture

from sqlmesh.core.context import Context
from sqlmesh.core.environment import Environment
from sqlmesh.utils.errors import PlanError
from web.server.api.endpoints.files import _get_file_with_content
from web.server.settings import get_settings

pytestmark = pytest.mark.web


@pytest.fixture
def client(web_app: FastAPI) -> TestClient:
    return TestClient(web_app)


def test_get_files(client: TestClient, project_tmp_path: Path) -> None:
    models_dir = project_tmp_path / "models"
    models_dir.mkdir()
    sql_file = models_dir / "foo.sql"
    sql_file.write_text("MODEL (name foo); SELECT 1 ds;")

    response = client.get("/api/files")
    assert response.status_code == 200
    assert response.json() == {
        "name": project_tmp_path.name,
        "path": "",
        "directories": [
            {
                "name": "models",
                "path": "models",
                "directories": [],
                "files": [
                    {
                        "name": "foo.sql",
                        "path": "models/foo.sql",
                        "extension": ".sql",
                        "content": None,
                    }
                ],
            }
        ],
        "files": [
            {
                "name": "config.py",
                "path": "config.py",
                "extension": ".py",
                "content": None,
            }
        ],
    }


def test_get_file(client: TestClient, project_tmp_path: Path) -> None:
    txt_file = project_tmp_path / "foo.txt"
    txt_file.write_text("bar")

    response = client.get("/api/files/foo.txt")
    assert response.status_code == 200
    assert response.json() == {
        "name": "foo.txt",
        "path": "foo.txt",
        "extension": ".txt",
        "content": "bar",
    }


def test_get_file_not_found(client: TestClient) -> None:
    response = client.get("/api/files/not_found.txt")
    assert response.status_code == 404


def test_get_file_invalid_path(client: TestClient, project_tmp_path: Path) -> None:
    config = project_tmp_path / "config.py"
    config.write_text(
        """from sqlmesh.core.config import Config, ModelDefaultsConfig
config = Config(ignore_patterns=["*.txt"], model_defaults=ModelDefaultsConfig(dialect=''))
    """
    )
    foo_txt = project_tmp_path / "foo.txt"
    foo_txt.touch()

    response = client.get("/api/files/foo.txt")
    assert response.status_code == 404


def test_write_file(client: TestClient, project_tmp_path: Path) -> None:
    response = client.post("/api/files/foo.txt", json={"content": "bar"})
    file = _get_file_with_content(project_tmp_path / "foo.txt", "foo.txt")
    assert response.status_code == 204
    assert file.dict() == {
        "name": "foo.txt",
        "path": "foo.txt",
        "extension": ".txt",
        "content": "bar",
    }


def test_write_file_non_ascii(client: TestClient, project_tmp_path: Path) -> None:
    response = client.post("/api/files/foo.txt", json={"content": "何か良いこと"})
    file = _get_file_with_content(project_tmp_path / "foo.txt", "foo.txt")
    assert response.status_code == 204
    assert file.dict() == {
        "name": "foo.txt",
        "path": "foo.txt",
        "extension": ".txt",
        "content": "何か良いこと",
    }


def test_update_file(client: TestClient, project_tmp_path: Path) -> None:
    txt_file = project_tmp_path / "foo.txt"
    txt_file.write_text("bar")

    response = client.post("/api/files/foo.txt", json={"content": "baz"})
    file = _get_file_with_content(project_tmp_path / "foo.txt", "foo.txt")
    assert response.status_code == 204
    assert file.dict() == {
        "name": "foo.txt",
        "path": "foo.txt",
        "extension": ".txt",
        "content": "baz",
    }


def test_rename_file(client: TestClient, project_tmp_path: Path) -> None:
    txt_file = project_tmp_path / "foo.txt"
    txt_file.write_text("bar")

    response = client.post("/api/files/foo.txt", json={"new_path": "baz.txt"})
    file = _get_file_with_content(project_tmp_path / "baz.txt", "baz.txt")
    assert response.status_code == 204
    assert file.dict() == {
        "name": "baz.txt",
        "path": "baz.txt",
        "extension": ".txt",
        "content": "bar",
    }
    assert not txt_file.exists()


def test_rename_file_and_keep_content(client: TestClient, project_tmp_path: Path) -> None:
    txt_file = project_tmp_path / "foo.txt"
    txt_file.write_text("bar")

    response = client.post(
        "/api/files/foo.txt", json={"content": "hello world", "new_path": "baz.txt"}
    )
    file = _get_file_with_content(project_tmp_path / "baz.txt", "baz.txt")
    assert response.status_code == 204
    assert file.dict() == {
        "name": "baz.txt",
        "path": "baz.txt",
        "extension": ".txt",
        "content": "bar",
    }
    assert not txt_file.exists()


def test_rename_file_not_found(client: TestClient, project_tmp_path: Path) -> None:
    response = client.post("/api/files/foo.txt", json={"new_path": "baz.txt"})
    assert response.status_code == 404


def test_rename_file_already_exists(client: TestClient, project_tmp_path: Path) -> None:
    foo_file = project_tmp_path / "foo.txt"
    foo_file.write_text("foo")
    bar_file = project_tmp_path / "bar.txt"
    bar_file.write_text("bar")

    response = client.post("/api/files/foo.txt", json={"new_path": "bar.txt"})
    file = _get_file_with_content(project_tmp_path / "bar.txt", "bar.txt")
    assert response.status_code == 204
    assert file.dict() == {
        "name": "bar.txt",
        "path": "bar.txt",
        "extension": ".txt",
        "content": "foo",
    }
    assert not foo_file.exists()


def test_rename_file_to_existing_directory(client: TestClient, project_tmp_path: Path) -> None:
    foo_file = project_tmp_path / "foo.txt"
    foo_file.touch()
    existing_dir = project_tmp_path / "existing_dir"
    existing_dir.mkdir()

    response = client.post("/api/files/foo.txt", json={"new_path": "existing_dir"})
    assert response.status_code == 422
    assert foo_file.exists()


def test_write_file_empty_body(client: TestClient, project_tmp_path: Path) -> None:
    response = client.post("/api/files/foo.txt", json={})
    assert response.status_code == 204


def test_delete_file(client: TestClient, project_tmp_path: Path) -> None:
    txt_file = project_tmp_path / "foo.txt"
    txt_file.write_text("bar")

    response = client.delete("/api/files/foo.txt")
    assert response.status_code == 204
    assert not txt_file.exists()


def test_delete_file_not_found(client: TestClient, project_tmp_path: Path) -> None:
    response = client.delete("/api/files/not_found.txt")
    assert response.status_code == 404


def test_create_directory(client: TestClient, project_tmp_path: Path) -> None:
    response = client.post("/api/directories/new_dir")
    assert response.status_code == 200
    assert (project_tmp_path / "new_dir").exists()
    assert response.json() == {
        "directories": [],
        "files": [],
        "name": "new_dir",
        "path": "new_dir",
    }


def test_create_directory_already_exists(client: TestClient, project_tmp_path: Path) -> None:
    new_dir = project_tmp_path / "new_dir"
    new_dir.mkdir()

    response = client.post("/api/directories/new_dir")
    assert response.status_code == 422
    assert response.json()["message"] == "Directory already exists"


def test_rename_directory(client: TestClient, project_tmp_path: Path) -> None:
    new_dir = project_tmp_path / "new_dir"
    new_dir.mkdir()

    response = client.post("/api/directories/new_dir", json={"new_path": "renamed_dir"})
    assert response.status_code == 200
    assert not new_dir.exists()
    assert (project_tmp_path / "renamed_dir").exists()
    assert response.json() == {
        "directories": [],
        "files": [],
        "name": "renamed_dir",
        "path": "renamed_dir",
    }


def test_rename_directory_already_exists_empty(client: TestClient, project_tmp_path: Path) -> None:
    new_dir = project_tmp_path / "new_dir"
    new_dir.mkdir()
    existing_dir = project_tmp_path / "renamed_dir"
    existing_dir.mkdir()

    response = client.post("/api/directories/new_dir", json={"new_path": "renamed_dir"})
    assert response.status_code == 200
    assert not new_dir.exists()
    assert (project_tmp_path / "renamed_dir").exists()
    assert response.json() == {
        "directories": [],
        "files": [],
        "name": "renamed_dir",
        "path": "renamed_dir",
    }


def test_rename_directory_already_exists_not_empty(
    client: TestClient, project_tmp_path: Path
) -> None:
    new_dir = project_tmp_path / "new_dir"
    new_dir.mkdir()
    existing_dir = project_tmp_path / "renamed_dir"
    existing_dir.mkdir()
    existing_file = existing_dir / "foo.txt"
    existing_file.touch()

    response = client.post("/api/directories/new_dir", json={"new_path": "renamed_dir"})
    assert response.status_code == 422
    assert response.json()["message"] == "Unable to move a file"
    assert new_dir.exists()


def test_rename_directory_to_existing_file(client: TestClient, project_tmp_path: Path) -> None:
    new_dir = project_tmp_path / "new_dir"
    new_dir.mkdir()
    existing_file = project_tmp_path / "foo.txt"
    existing_file.touch()

    response = client.post("/api/directories/new_dir", json={"new_path": "foo.txt"})
    assert response.status_code == 422
    assert response.json()["message"] == "Unable to move a file"
    assert new_dir.exists()


def test_delete_directory(client: TestClient, project_tmp_path: Path) -> None:
    new_dir = project_tmp_path / "new_dir"
    new_dir.mkdir()

    response = client.delete("/api/directories/new_dir")
    assert response.status_code == 204
    assert not new_dir.exists()


def test_delete_directory_not_found(client: TestClient, project_tmp_path: Path) -> None:
    response = client.delete("/api/directories/fake_dir")
    assert response.status_code == 404


def test_delete_directory_not_a_directory(client: TestClient, project_tmp_path: Path) -> None:
    txt_file = project_tmp_path / "foo.txt"
    txt_file.touch()

    response = client.delete("/api/directories/foo.txt")
    assert response.status_code == 422
    assert response.json()["message"] == "Not a directory"


def test_delete_directory_not_empty(client: TestClient, project_tmp_path: Path) -> None:
    new_dir = project_tmp_path / "new_dir"
    new_dir.mkdir()
    (new_dir / "foo.txt").touch()

    response = client.delete("/api/directories/new_dir")
    assert response.status_code == 204
    assert not new_dir.exists()


def test_apply(client: TestClient, project_tmp_path: Path) -> None:
    models_dir = project_tmp_path / "models"
    models_dir.mkdir()
    sql_file = models_dir / "foo.sql"
    sql_file.write_text("MODEL (name foo); SELECT 1;")

    client.app.state.circuit_breaker = threading.Event()  # type: ignore
    response = client.post("/api/commands/apply", json={"environment": "dev"})
    assert response.status_code == 204


@pytest.mark.skip(
    reason="needs to be fixed: plan tests are failing inside coroutine and won't throw 422"
)
def test_apply_test_failures(
    client: TestClient, web_sushi_context: Context, mocker: MockerFixture
) -> None:
    mocker.patch.object(web_sushi_context, "_run_plan_tests", side_effect=PlanError())
    response = client.post("/api/commands/apply", json={"environment": "dev"})
    assert response.status_code == 422
    assert response.json()["message"] == "Unable to run a plan"


def test_plan(client: TestClient, web_sushi_context: Context) -> None:
    client.app.state.circuit_breaker = threading.Event()  # type: ignore
    response = client.post("/api/plan", json={"environment": "dev"})
    assert response.status_code == 204


@pytest.mark.skip(
    reason="needs to be fixed: plan tests are failing inside coroutine and won't throw 422"
)
def test_plan_test_failures(
    client: TestClient, web_sushi_context: Context, mocker: MockerFixture
) -> None:
    mocker.patch.object(web_sushi_context, "_run_plan_tests", side_effect=PlanError())
    response = client.post("/api/plan", json={"environment": "dev"})
    assert response.status_code == 422
    assert response.json()["message"] == "Unable to run a plan"


@pytest.mark.asyncio
async def test_cancel(client: TestClient) -> None:
    client.app.state.circuit_breaker = threading.Event()  # type: ignore
    transport = ASGITransport(client.app)  # type: ignore
    async with AsyncClient(transport=transport, base_url="http://testserver") as _client:
        await _client.post("/api/plan", json={"environment": "dev"})
        response = await _client.post("/api/plan/cancel")
    assert response.status_code == 204
    assert client.app.state.task.cancelled()  # type: ignore


def test_cancel_no_task(client: TestClient) -> None:
    response = client.post("/api/plan/cancel")
    assert response.status_code == 204


def test_evaluate(client: TestClient, web_sushi_context: Context) -> None:
    response = client.post(
        "/api/commands/evaluate",
        json={
            "model": "sushi.top_waiters",
            "start": "2022-01-01",
            "end": "now",
            "execution_time": "now",
            "limit": 100,
        },
    )
    assert response.status_code == 200
    with pa.ipc.open_stream(response.content) as reader:
        df = reader.read_pandas()
    assert not df.empty


def test_meta(client: TestClient) -> None:
    from sqlmesh.cli.main import _sqlmesh_version

    response = client.get("/api/meta")
    assert response.status_code == 200
    assert response.json() == {
        "version": _sqlmesh_version(),
        "has_running_task": False,
        "node_colors": {},
    }


def test_modules(client: TestClient) -> None:
    settings = get_settings()
    response = client.get("/api/modules")
    assert response.status_code == 200
    assert response.json() == list(settings.modules)


def test_fetchdf(client: TestClient, web_sushi_context: Context) -> None:
    response = client.post("/api/commands/fetchdf", json={"sql": "SELECT * from sushi.top_waiters"})
    assert response.status_code == 200
    with pa.ipc.open_stream(response.content) as reader:
        df = reader.read_pandas()
    assert not df.empty


def test_get_model(client: TestClient, web_sushi_context: Context) -> None:
    response = client.get("/api/models/sushi.customers")

    assert response.status_code == 200

    test_model = response.json()

    assert test_model.get("name") == "sushi.customers"
    assert test_model.get("path") == "models/customers.sql"
    assert test_model.get("dialect") == "duckdb"
    assert test_model.get("columns") == [
        {
            "name": "customer_id",
            "type": "INT",
            "description": "customer_id uniquely identifies customers",
        },
        {"name": "status", "type": "TEXT"},
        {"name": "zip", "type": "TEXT"},
    ]


# TODO: add better tests for this endpoint
def test_get_models(client: TestClient, web_sushi_context: Context) -> None:
    response = client.get("/api/models")

    assert response.status_code == 200

    test_model = response.json()[0]

    assert test_model.get("name")
    assert test_model.get("path")
    assert test_model.get("dialect")
    assert test_model.get("columns")


def test_render(client: TestClient, web_sushi_context: Context) -> None:
    response = client.post("/api/commands/render", json={"model": "sushi.items"})
    assert response.status_code == 200
    assert response.json()["sql"]


def test_render_invalid_model(client: TestClient, web_sushi_context: Context) -> None:
    response = client.post("/api/commands/render", json={"model": "foo.bar"})
    assert response.status_code == 422
    assert response.json()["message"] == "Unable to find a model"


def test_get_environments(client: TestClient, project_context: Context) -> None:
    response = client.get("/api/environments")
    assert response.status_code == 200
    response_json = response.json()
    assert len(response_json["environments"]) == 1

    environment = Environment.parse_obj(response_json["environments"]["prod"])
    assert environment == Environment(
        name="prod",
        snapshots=[],
        start_at="1970-01-01",
        plan_id="",
        suffix_target="schema",
    )
    assert response_json["pinned_environments"] == list(project_context.config.pinned_environments)
    assert (
        response_json["default_target_environment"]
        == project_context.config.default_target_environment
    )


def test_delete_environment_success(client: TestClient, web_sushi_context: Context):
    response = client.delete("/api/environments/test")

    assert response.status_code == 204


def test_delete_environment_failure(
    client: TestClient, web_sushi_context: Context, mocker: MockerFixture
):
    mocker.patch.object(
        web_sushi_context.state_sync,
        "invalidate_environment",
        side_effect=Exception("Some error"),
    )

    response = client.delete("/api/environments/test")

    assert response.status_code == 422
    assert response.json()["message"] == "Unable to delete environments"


def test_table_diff(client: TestClient, web_sushi_context: Context) -> None:
    web_sushi_context.plan(
        "dev",
        no_prompts=True,
        auto_apply=True,
        skip_tests=True,
        include_unmodified=True,
    )
    response = client.get(
        "/api/table_diff",
        params={
            "source": "prod",
            "target": "dev",
            "model_or_snapshot": "sushi.customer_revenue_by_day",
        },
    )
    assert response.status_code == 200
    assert response.json() == None


def test_test(client: TestClient, web_sushi_context: Context) -> None:
    response = client.get("/api/commands/test")
    assert response.status_code == 200
    response_json = response.json()
    assert response_json["tests_run"] == 3
    assert response_json["failures"] == []

    # Single test
    response = client.get("/api/commands/test", params={"test": "tests/test_order_items.yaml"})
    assert response.status_code == 200
    response_json = response.json()
    assert response_json["tests_run"] == 1
    assert response_json["failures"] == []


def test_test_failure(client: TestClient, project_context: Context) -> None:
    models_dir = project_context.path / "models"
    models_dir.mkdir()
    sql_file = models_dir / "foo.sql"
    sql_file.write_text("MODEL (name foo); SELECT 1 ds;")

    tests_dir = project_context.path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_foo.yaml"
    test_file.write_text(
        """test_foo:
  model: foo
  outputs:
    query:
      - ds: 2
  vars:
    start: 2022-01-01
    end: 2022-01-01
    latest: 2022-01-01"""
    )

    project_context.load()
    response = client.get("/api/commands/test")
    assert response.status_code == 200
    response_json = response.json()
    assert response_json["tests_run"] == 1
    assert response_json["failures"] == [
        {
            "name": "test_foo",
            "path": "tests/test_foo.yaml",
            "tb": "AssertionError: Data mismatch (exp: expected, act: actual)\n\n   ds    \n  exp act\n0   2   1\n",
        }
    ]
