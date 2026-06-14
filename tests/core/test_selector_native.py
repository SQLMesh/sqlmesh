from __future__ import annotations

import typing as t
from pathlib import Path
from unittest.mock import call

import pytest
from pytest_mock.plugin import MockerFixture
import subprocess

from sqlmesh.core import dialect as d
from sqlmesh.core.audit import StandaloneAudit
from sqlmesh.core.environment import Environment
from sqlmesh.core.model import Model, SqlModel
from sqlmesh.core.model.common import ParsableSql
from sqlmesh.core.selector import NativeSelector
from sqlmesh.core.snapshot import SnapshotChangeCategory
from sqlmesh.utils import UniqueKeyDict
from sqlmesh.utils.date import now_timestamp
from sqlmesh.utils.git import GitClient


@pytest.mark.parametrize(
    "default_catalog",
    [
        None,
        "test_catalog",
    ],
)
def test_select_models(mocker: MockerFixture, make_snapshot, default_catalog: t.Optional[str]):
    added_model = SqlModel(
        name="db.added_model",
        query=d.parse_one("SELECT 1 AS a"),
        default_catalog=default_catalog,
        tags=["tag1"],
    )
    modified_model_v1 = SqlModel(
        name="db.modified_model",
        query=d.parse_one("SELECT a + 1 FROM db.added_model"),
        default_catalog=default_catalog,
        tags=["tag2"],
    )
    modified_model_v2 = SqlModel(
        name="db.modified_model",
        query=d.parse_one("SELECT a + 2 FROM db.added_model"),
        default_catalog=default_catalog,
        tags=["tag2"],
    )
    removed_model = SqlModel(
        name="db.removed_model",
        query=d.parse_one("SELECT a FROM db.added_model"),
        default_catalog=default_catalog,
    )
    standalone_audit = StandaloneAudit(
        name="test_audit", query=d.parse_one("SELECT * FROM added_model WHERE a IS NULL")
    )

    modified_model_v1_snapshot = make_snapshot(modified_model_v1)
    modified_model_v1_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    removed_model_snapshot = make_snapshot(removed_model)
    removed_model_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    standalone_audit_snapshot = make_snapshot(standalone_audit)
    standalone_audit_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    env_name = "test_env"

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.return_value = Environment(
        name=env_name,
        snapshots=[
            s.table_info
            for s in (modified_model_v1_snapshot, removed_model_snapshot, standalone_audit_snapshot)
        ],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )
    state_reader_mock.get_snapshots.return_value = {
        modified_model_v1_snapshot.snapshot_id: modified_model_v1_snapshot,
        removed_model_snapshot.snapshot_id: removed_model_snapshot,
        standalone_audit_snapshot.snapshot_id: standalone_audit_snapshot,
    }

    added_model_schema = {'"db"': {'"added_model"': {"a": "INT"}}}
    if default_catalog:
        added_model_schema = {f'"{default_catalog}"': added_model_schema}  # type: ignore

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    local_models[added_model.fqn] = added_model
    local_models[modified_model_v2.fqn] = modified_model_v2.copy(
        update={"mapping_schema": added_model_schema}
    )
    selector = NativeSelector(state_reader_mock, local_models, default_catalog=default_catalog)

    _assert_models_equal(
        selector.select_models(["db.added_model"], env_name),
        {
            added_model.fqn: added_model,
            modified_model_v1.fqn: modified_model_v1.copy(
                update={"mapping_schema": added_model_schema}
            ),
            removed_model.fqn: removed_model,
        },
    )
    _assert_models_equal(
        selector.select_models(["db.modified_model"], "missing_env", fallback_env_name=env_name),
        {
            modified_model_v2.fqn: modified_model_v2,
            removed_model.fqn: removed_model,
        },
    )
    _assert_models_equal(
        selector.select_models(["db.removed_model"], env_name),
        {
            modified_model_v1.fqn: modified_model_v1,
        },
    )
    _assert_models_equal(
        selector.select_models(
            ["db.added_model", "db.modified_model"], "missing_env", fallback_env_name=env_name
        ),
        {
            added_model.fqn: added_model,
            modified_model_v2.fqn: modified_model_v2.copy(
                update={"mapping_schema": added_model_schema}
            ),
            removed_model.fqn: removed_model,
        },
    )
    _assert_models_equal(
        selector.select_models(["+db.modified_model"], env_name),
        {
            added_model.fqn: added_model,
            modified_model_v2.fqn: modified_model_v2.copy(
                update={"mapping_schema": added_model_schema}
            ),
            removed_model.fqn: removed_model,
        },
    )
    _assert_models_equal(
        selector.select_models(["db.added_model+"], env_name),
        {
            added_model.fqn: added_model,
            modified_model_v2.fqn: modified_model_v2.copy(
                update={"mapping_schema": added_model_schema}
            ),
            removed_model.fqn: removed_model,
        },
    )
    _assert_models_equal(
        selector.select_models(
            ["db.added_model", "db.modified_model", "db.removed_model"], env_name
        ),
        local_models,
    )
    _assert_models_equal(
        selector.select_models(["*_model", "db.removed_model"], env_name),
        local_models,
    )
    _assert_models_equal(
        selector.select_models(["tag:tag1", "tag:tag2"], "missing_env", fallback_env_name=env_name),
        {
            added_model.fqn: added_model,
            modified_model_v2.fqn: modified_model_v2.copy(
                update={"mapping_schema": added_model_schema}
            ),
            removed_model.fqn: removed_model,
        },
    )
    _assert_models_equal(
        selector.select_models(["tag:tag*"], "missing_env", fallback_env_name=env_name),
        {
            added_model.fqn: added_model,
            modified_model_v2.fqn: modified_model_v2.copy(
                update={"mapping_schema": added_model_schema}
            ),
            removed_model.fqn: removed_model,
        },
    )
    _assert_models_equal(
        selector.select_models(["+tag:tag2"], env_name),
        {
            added_model.fqn: added_model,
            modified_model_v2.fqn: modified_model_v2.copy(
                update={"mapping_schema": added_model_schema}
            ),
            removed_model.fqn: removed_model,
        },
    )


def test_select_models_expired_environment(mocker: MockerFixture, make_snapshot):
    modified_model_v1 = SqlModel(
        name="db.modified_model",
        query=d.parse_one("SELECT a + 1 FROM db.added_model"),
    )
    modified_model_v2 = SqlModel(
        name="db.modified_model",
        query=d.parse_one("SELECT a + 2 FROM db.added_model"),
    )
    removed_model = SqlModel(
        name="db.removed_model",
        query=d.parse_one("SELECT a FROM db.added_model"),
    )
    standalone_audit = StandaloneAudit(
        name="test_audit", query=d.parse_one("SELECT * FROM added_model WHERE a IS NULL")
    )

    modified_model_v1_snapshot = make_snapshot(modified_model_v1)
    modified_model_v1_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    removed_model_snapshot = make_snapshot(removed_model)
    removed_model_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    standalone_audit_snapshot = make_snapshot(standalone_audit)
    standalone_audit_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    prod_env = Environment(
        name="prod",
        snapshots=[modified_model_v1_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )

    env_name = "test_env"
    dev_env = Environment(
        name=env_name,
        snapshots=[modified_model_v1_snapshot.table_info, removed_model_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.side_effect = (
        lambda name: prod_env if name == "prod" else dev_env
    )

    all_snapshots = {
        modified_model_v1_snapshot.snapshot_id: modified_model_v1_snapshot,
        removed_model_snapshot.snapshot_id: removed_model_snapshot,
    }
    state_reader_mock.get_snapshots.side_effect = lambda snapshots: {
        s.snapshot_id: all_snapshots[s.snapshot_id] for s in snapshots
    }

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    local_models[modified_model_v2.fqn] = modified_model_v2
    selector = NativeSelector(state_reader_mock, local_models)

    _assert_models_equal(
        selector.select_models(["*.modified_model"], env_name, fallback_env_name="prod"),
        {
            removed_model.fqn: removed_model,
            modified_model_v2.fqn: modified_model_v2,
        },
    )

    dev_env.expiration_ts = now_timestamp() - 1
    _assert_models_equal(
        selector.select_models(["*.modified_model"], env_name, fallback_env_name="prod"),
        {
            modified_model_v2.fqn: modified_model_v2,
        },
    )


def test_select_change_schema(mocker: MockerFixture, make_snapshot):
    parent = SqlModel(
        name="db.parent",
        query=d.parse_one("SELECT 1 AS a"),
    )
    child = SqlModel(
        name="db.child",
        query=d.parse_one("SELECT * FROM db.parent"),
        mapping_schema={'"db"': {'"parent"': {"a": "INT"}}},
    )

    parent_snapshot = make_snapshot(parent)
    parent_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    child_snapshot = make_snapshot(child)
    child_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    env_name = "test_env"

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.return_value = Environment(
        name=env_name,
        snapshots=[s.table_info for s in (parent_snapshot, child_snapshot)],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )
    state_reader_mock.get_snapshots.return_value = {
        parent_snapshot.snapshot_id: parent_snapshot,
        child_snapshot.snapshot_id: child_snapshot,
    }

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    local_parent = parent.copy(
        update={
            "query_": ParsableSql(
                sql=parent.query.select("2 as b", append=False).sql(dialect=parent.dialect)  # type: ignore
            )
        }
    )
    local_models[local_parent.fqn] = local_parent
    local_child = child.copy(update={"mapping_schema": {'"db"': {'"parent"': {"b": "INT"}}}})
    local_models[local_child.fqn] = local_child

    selector = NativeSelector(state_reader_mock, local_models)

    selected, _ = selector.select_models(["db.parent"], env_name)
    assert selected[local_child.fqn].render_query() != child.render_query()

    _assert_models_equal(
        selected,
        {
            local_parent.fqn: local_parent,
            local_child.fqn: local_child,
        },
    )

    selected, _ = selector.select_models(["db.child"], env_name)
    assert selected[local_child.fqn].data_hash == child.data_hash

    _assert_models_equal(
        selected,
        {
            parent.fqn: parent,
            child.fqn: child,
        },
    )


def test_select_models_missing_env(mocker: MockerFixture, make_snapshot):
    model = SqlModel(name="test_model", query=d.parse_one("SELECT 1 AS a"))

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.return_value = None

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    local_models[model.fqn] = model

    selector = NativeSelector(state_reader_mock, local_models)

    assert selector.select_models([model.name], "missing_env")[0].keys() == {model.fqn}
    assert not selector.select_models(["missing"], "missing_env")[0]

    assert selector.select_models(
        [model.name], "missing_env", fallback_env_name="another_missing_env"
    )[0].keys() == {model.fqn}

    state_reader_mock.get_environment.assert_has_calls(
        [
            call("missing_env"),
            call("missing_env"),
            call("missing_env"),
            call("another_missing_env"),
        ]
    )


@pytest.mark.parametrize(
    "model_defs, selections, output",
    [
        # Direct matching only
        (
            [("model1", "tag1", None), ("model2", "tag2", None), ("model3", "tag3", None)],
            ["tag:tag1", "tag:tag3"],
            {'"model1"', '"model3"'},
        ),
        # Wildcard works
        (
            [("model1", "tag1", None), ("model2", "tag2", None), ("model3", "tag3", None)],
            ["tag:tag*"],
            {'"model1"', '"model2"', '"model3"'},
        ),
        # Downstream models are included
        (
            [("model1", "tag1", None), ("model2", "tag2", {"model1"}), ("model3", "tag3", None)],
            ["tag:tag1+"],
            {'"model1"', '"model2"'},
        ),
        # Upstream models are included
        (
            [("model1", "tag1", None), ("model2", "tag2", None), ("model3", "tag3", {"model2"})],
            ["+tag:tag3"],
            {'"model2"', '"model3"'},
        ),
        # Upstream and downstream models are included
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", {"model1"}),
                ("model3", "tag3", {"model2"}),
            ],
            ["+tag:tag2+"],
            {'"model1"', '"model2"', '"model3"'},
        ),
        # Wildcard works with upstream and downstream models
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", {"model1"}),
                ("model3", "tag3", {"model2"}),
                ("model4", "blah", {"model3"}),
                ("model5", "tag4", None),
                # Only excluded model since it doesn't match wildcard nor upstream/downstream
                ("model6", "blah", None),
            ],
            ["+tag:tag*+"],
            {'"model1"', '"model2"', '"model3"', '"model4"', '"model5"'},
        ),
        # Multiple tags work
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", None),
                ("model3", "tag3", None),
                ("model4", "tag4", None),
            ],
            ["tag:tag1", "tag:tag3"],
            {'"model1"', '"model3"'},
        ),
        # Multiple tags work with upstream and downstream models
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", {"model1"}),
                ("model3", "tag3", {"model2"}),
                ("model4", "tag4", None),
                ("model5", "tag5", {"model4"}),
                ("model6", "tag6", {"model5"}),
            ],
            ["+tag:tag3", "tag:tag5"],
            {'"model1"', '"model2"', '"model3"', '"model5"'},
        ),
        # Case-insensitive matching
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", None),
                ("model3", "tag3", None),
            ],
            ["tag:TAG*"],
            {'"model1"', '"model2"', '"model3"'},
        ),
        # Wildcard returns everything
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", None),
                ("model3", "tag3", None),
            ],
            ["tag:*"],
            {'"model1"', '"model2"', '"model3"'},
        ),
        # Upstream that don't exist is fine
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", None),
            ],
            ["+tag:tag2"],
            {'"model2"'},
        ),
        # No matches returns empty set
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", None),
            ],
            ["+tag:tag3*+", "+tag:tag3+"],
            set(),
        ),
        # Mix of models and tags
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", None),
                ("model3", "tag3", None),
            ],
            ["tag:tag1", "model2"],
            {'"model1"', '"model2"'},
        ),
        # Intersection of tags and model names
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag1", {"model1"}),
                ("model3", "tag2", {"model1"}),
                ("model4", "tag1", None),
            ],
            ["tag:tag1 & model1+"],
            {'"model1"', '"model2"'},
        ),
        # Intersection of tags and model names (order doesn't matter)
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag1", {"model1"}),
                ("model3", "tag2", {"model1"}),
                ("model4", "tag1", None),
            ],
            ["model1+ & tag:tag1"],
            {'"model1"', '"model2"'},
        ),
        # negation
        (
            [("model1", "tag1", None), ("model2", "tag2", None), ("model3", "tag3", None)],
            ["^tag:tag1"],
            {'"model2"', '"model3"'},
        ),
        (
            [("model1", "tag1", None), ("model2", "tag2", None), ("model3", "tag3", None)],
            ["^model1"],
            {'"model2"', '"model3"'},
        ),
        (
            [("model1", "tag1", None), ("model2", "tag2", None), ("model3", "tag3", None)],
            ["model* & ^(tag:tag1 | tag:tag2)"],
            {'"model3"'},
        ),
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", {"model1"}),
                ("model3", "tag3", {"model1"}),
            ],
            ["(model1*)+"],
            {'"model1"', '"model2"', '"model3"'},
        ),
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", {"model1"}),
                ("model3", "tag3", {"model2"}),
            ],
            ["+(+model2*+)+"],
            {'"model1"', '"model2"', '"model3"'},
        ),
        (
            [
                ("model1", "tag1", None),
                ("model2", "tag2", {"model1"}),
                ("model3", "tag3", {"model1"}),
            ],
            ["(model* & ^*1)+"],
            {'"model2"', '"model3"'},
        ),
        (
            [("model2", "tag1", None), ("model2_1", "tag2", None), ("model2_2", "tag3", None)],
            ["*2_*"],
            {'"model2_1"', '"model2_2"'},
        ),
    ],
)
def test_expand_model_selections(
    mocker: MockerFixture, make_snapshot, model_defs, selections, output
):
    models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    for model_name, tag, depends_on in model_defs:
        model = SqlModel(
            name=model_name, query=d.parse_one("SELECT 1 AS a"), depends_on=depends_on, tags=[tag]
        )
        models[model.fqn] = model

    selector = NativeSelector(mocker.Mock(), models)
    assert selector.expand_model_selections(selections) == output


def test_model_selection_normalized(mocker: MockerFixture, make_snapshot):
    models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    model = SqlModel(
        name="`db.test_Model`",
        query=d.parse_one("SELECT 1 AS a"),
        tags=["tag1"],
        dialect="bigquery",
    )
    models[model.fqn] = model
    selector = NativeSelector(mocker.Mock(), models, dialect="bigquery")
    assert selector.expand_model_selections(["db.test_Model"]) == {'"db"."test_Model"'}


@pytest.mark.parametrize(
    "expressions, expected_fqns",
    [
        (["git:main"], {'"test_model_a"', '"test_model_c"'}),
        (["git:main & +*model_c"], {'"test_model_c"'}),
        (["git:main+"], {'"test_model_a"', '"test_model_c"', '"test_model_d"'}),
        (["+git:main"], {'"test_model_a"', '"test_model_c"', '"test_model_b"'}),
        (["+git:main+"], {'"test_model_a"', '"test_model_c"', '"test_model_b"', '"test_model_d"'}),
    ],
)
def test_expand_git_selection(
    mocker: MockerFixture, expressions: t.List[str], expected_fqns: t.Set[str]
):
    models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")

    model_a = SqlModel(name="test_model_a", query=d.parse_one("SELECT 1 AS a"))
    model_a._path = Path("/path/to/test_model_a.sql")
    models[model_a.fqn] = model_a

    model_b = SqlModel(name="test_model_b", query=d.parse_one("SELECT 2 AS b"))
    model_b._path = Path("/path/to/test_model_b.sql")
    models[model_b.fqn] = model_b

    model_c = SqlModel(
        name="test_model_c",
        query=d.parse_one("SELECT b AS c FROM test_model_b"),
        depends_on={"test_model_b"},
    )
    model_c._path = Path("/path/to/test_model_c.sql")
    models[model_c.fqn] = model_c

    model_d = SqlModel(
        name="test_model_d",
        query=d.parse_one("SELECT c FROM test_model_c"),
        depends_on={"test_model_c"},
    )
    model_d._path = Path("/path/to/test_model_d.sql")
    models[model_d.fqn] = model_d

    git_client_mock = mocker.Mock()
    git_client_mock.list_untracked_files.return_value = []
    git_client_mock.list_uncommitted_changed_files.return_value = []
    git_client_mock.list_committed_changed_files.return_value = [model_a._path, model_c._path]

    selector = NativeSelector(mocker.Mock(), models)
    selector._git_client = git_client_mock

    assert selector.expand_model_selections(expressions) == expected_fqns

    git_client_mock.list_committed_changed_files.assert_called_once_with(target_branch="main")
    git_client_mock.list_uncommitted_changed_files.assert_called_once()
    git_client_mock.list_untracked_files.assert_called_once()


def test_expand_git_selection_integration(tmp_path: Path, mocker: MockerFixture):
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True)

    models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    model_a_path = repo_path / "model_a.sql"
    model_a_path.write_text("SELECT 1 AS a")
    model_a = SqlModel(name="test_model_a", query=d.parse_one("SELECT 1 AS a"))
    model_a._path = model_a_path
    models[model_a.fqn] = model_a

    model_b_path = repo_path / "model_b.sql"
    model_b_path.write_text("SELECT 2 AS b")
    model_b = SqlModel(name="test_model_b", query=d.parse_one("SELECT 2 AS b"))
    model_b._path = model_b_path
    models[model_b.fqn] = model_b

    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Max",
            "-c",
            "user.email=max@rb.com",
            "commit",
            "-m",
            "Initial commit",
        ],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # no changes should select nothing
    git_client = GitClient(repo_path)
    selector = NativeSelector(mocker.Mock(), models)
    selector._git_client = git_client
    assert selector.expand_model_selections([f"git:main"]) == set()

    # modify A but dont stage it, should be only selected
    model_a_path.write_text("SELECT 10 AS a")
    assert selector.expand_model_selections([f"git:main"]) == {'"test_model_a"'}

    # stage model A, should still select it
    subprocess.run(["git", "add", "model_a.sql"], cwd=repo_path, check=True, capture_output=True)
    assert selector.expand_model_selections([f"git:main"]) == {'"test_model_a"'}

    # now add unstaged change to B and both should be selected
    model_b_path.write_text("SELECT 20 AS b")
    assert selector.expand_model_selections([f"git:main"]) == {
        '"test_model_a"',
        '"test_model_b"',
    }

    subprocess.run(
        ["git", "checkout", "-b", "dev"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Max",
            "-c",
            "user.email=max@rb.com",
            "commit",
            "-m",
            "Update model_a",
        ],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # now A is committed in the dev branch and B unstaged but should both be selected
    assert selector.expand_model_selections([f"git:main"]) == {
        '"test_model_a"',
        '"test_model_b"',
    }


def test_select_models_with_external_parent(mocker: MockerFixture):
    default_catalog = "test_catalog"
    added_model = SqlModel(
        name="db.added_model",
        query=d.parse_one("SELECT 1 AS a FROM external"),
        default_catalog=default_catalog,
        tags=["tag1"],
    )

    env_name = "test_env"

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.return_value = Environment(
        name=env_name,
        snapshots=[],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )
    state_reader_mock.get_snapshots.return_value = {}

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    local_models[added_model.fqn] = added_model

    selector = NativeSelector(state_reader_mock, local_models, default_catalog=default_catalog)

    expanded_selections = selector.expand_model_selections(["+*added_model*"])
    assert expanded_selections == {added_model.fqn}


def test_select_models_local_tags_take_precedence_over_remote(
    mocker: MockerFixture, make_snapshot: t.Callable
) -> None:
    existing_model = SqlModel(
        name="db.existing",
        query=d.parse_one("SELECT 1 AS a"),
    )

    existing_snapshot = make_snapshot(existing_model)
    existing_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    env_name = "test_env"

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.return_value = Environment(
        name=env_name,
        snapshots=[existing_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )
    state_reader_mock.get_snapshots.return_value = {
        existing_snapshot.snapshot_id: existing_snapshot
    }

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    local_new = SqlModel(
        name="db.new",
        tags=["a"],
        query=d.parse_one("SELECT 1 as a"),
    )
    local_existing = existing_model.copy(update={"tags": ["a"]})  # type: ignore
    local_models[local_existing.fqn] = local_existing
    local_models[local_new.fqn] = local_new

    selector = NativeSelector(state_reader_mock, local_models)

    selected, _ = selector.select_models(["tag:a"], env_name)

    # both should get selected because they both now have the 'a' tag locally, even though one exists in remote state without the 'a' tag
    _assert_models_equal(
        selected,
        {
            local_existing.fqn: local_existing,
            local_new.fqn: local_new,
        },
    )


def test_select_models_returns_selected_fqns(mocker: MockerFixture, make_snapshot):
    """select_models should return the set of all matched FQNs (including env-only models)
    alongside the model dict."""
    local_model = SqlModel(
        name="db.local_model",
        query=d.parse_one("SELECT 1 AS a"),
    )
    deleted_model = SqlModel(
        name="db.deleted_model",
        query=d.parse_one("SELECT 2 AS b"),
    )

    deleted_model_snapshot = make_snapshot(deleted_model)
    deleted_model_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    env_name = "test_env"

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.return_value = Environment(
        name=env_name,
        snapshots=[deleted_model_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )
    state_reader_mock.get_snapshots.return_value = {
        deleted_model_snapshot.snapshot_id: deleted_model_snapshot,
    }

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    local_models[local_model.fqn] = local_model

    selector = NativeSelector(state_reader_mock, local_models)

    # Selecting a deleted model: selected_fqns includes it even though models dict won't.
    _, selected_fqns = selector.select_models(["db.deleted_model"], env_name)
    assert deleted_model.fqn in selected_fqns

    # Selecting a local model: selected_fqns includes it.
    _, selected_fqns = selector.select_models(["db.local_model"], env_name)
    assert local_model.fqn in selected_fqns

    # Mixed selection (active + deleted): both appear in selected_fqns.
    _, selected_fqns = selector.select_models(["db.deleted_model", "db.local_model"], env_name)
    assert selected_fqns == {deleted_model.fqn, local_model.fqn}

    # Wildcard should match both local and env models.
    _, selected_fqns = selector.select_models(["*_model"], env_name)
    assert selected_fqns == {deleted_model.fqn, local_model.fqn}

    # Non-existent model should not appear.
    _, selected_fqns = selector.select_models(["db.nonexistent"], env_name)
    assert selected_fqns == set()


def test_select_models_selected_fqns_fallback(mocker: MockerFixture, make_snapshot):
    """select_models selected_fqns should include env models found via fallback environment."""
    deleted_model = SqlModel(
        name="db.deleted_model",
        query=d.parse_one("SELECT 1 AS a"),
    )

    deleted_model_snapshot = make_snapshot(deleted_model)
    deleted_model_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    fallback_env = Environment(
        name="prod",
        snapshots=[deleted_model_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
    )

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.side_effect = (
        lambda name: fallback_env if name == "prod" else None
    )
    state_reader_mock.get_snapshots.return_value = {
        deleted_model_snapshot.snapshot_id: deleted_model_snapshot,
    }

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    selector = NativeSelector(state_reader_mock, local_models)

    _, selected_fqns = selector.select_models(
        ["db.deleted_model"], "missing_env", fallback_env_name="prod"
    )
    assert deleted_model.fqn in selected_fqns


def test_select_models_selected_fqns_expired(mocker: MockerFixture, make_snapshot):
    """select_models should not match env models from expired environments."""
    deleted_model = SqlModel(
        name="db.deleted_model",
        query=d.parse_one("SELECT 1 AS a"),
    )

    deleted_model_snapshot = make_snapshot(deleted_model)
    deleted_model_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    expired_env = Environment(
        name="test_env",
        snapshots=[deleted_model_snapshot.table_info],
        start_at="2023-01-01",
        end_at="2023-02-01",
        plan_id="test_plan_id",
        expiration_ts=now_timestamp() - 1,
    )

    state_reader_mock = mocker.Mock()
    state_reader_mock.get_environment.return_value = expired_env
    state_reader_mock.get_snapshots.return_value = {
        deleted_model_snapshot.snapshot_id: deleted_model_snapshot,
    }

    local_models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
    selector = NativeSelector(state_reader_mock, local_models)

    _, selected_fqns = selector.select_models(["db.deleted_model"], "test_env")
    assert selected_fqns == set()


def _assert_models_equal(
    actual: t.Union[t.Dict[str, Model], t.Tuple[t.Dict[str, Model], t.Set[str]]],
    expected: t.Dict[str, Model],
) -> None:
    # select_models returns a tuple; unwrap if needed.
    if isinstance(actual, tuple):
        actual = actual[0]
    assert set(actual) == set(expected)
    for name, model in actual.items():
        # Use dict() to make Pydantic V2 happy.
        assert model.dict() == expected[name].dict()
