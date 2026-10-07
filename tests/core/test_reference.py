import pytest

from sqlmesh.core.reference import ReferenceGraph
from sqlmesh.utils.errors import SQLMeshError


@pytest.fixture
def make_model(mocker):
    def make(name, refs=None):
        mock = mocker.Mock()
        mock.name = name
        mock.columns_to_types = {}
        references = []
        for ref_name, unique in refs or []:
            mock_ref = mocker.Mock()
            mock_ref.name = ref_name
            mock_ref.model_name = name
            mock_ref.unique = unique
            references.append(mock_ref)
        mock.all_references = references
        return mock

    return make


def test_graph(make_model):
    graph = ReferenceGraph(
        [
            make_model("model_a", [("a", True), ("b", False), ("c", False), ("d", False)]),
            make_model("model_b", [("a", True)]),
            make_model("model_c", [("d", True), ("e", True)]),
            make_model("model_d", [("e", True)]),
            make_model("model_e", [("b", False)]),
        ]
    )

    def find_path(a, b):
        return [(r.model_name, r.name) for r in graph.find_path(a, b)]

    assert find_path("model_a", "model_b") == [("model_a", "a"), ("model_b", "a")]
    assert find_path("model_a", "model_d") == [("model_a", "d"), ("model_c", "e"), ("model_d", "e")]

    with pytest.raises(SQLMeshError):
        assert find_path("model_a", "model_e")


def test_models_for_column(sushi_context_pre_scheduling):
    graph = ReferenceGraph(sushi_context_pre_scheduling.models.values())
    assert graph.models_for_column("sushi.orders", "status") == [
        "sushi.customers",
        "sushi.marketing",
        "sushi.raw_marketing",
    ]
    assert graph.models_for_column("sushi.orders", "event_date") == ["sushi.orders"]


def test_target_grain_does_not_override_nonunique_join_reference(make_model):
    graph = ReferenceGraph(
        [
            make_model("fact", [("org", False)]),
            make_model("dimension", [("dimension_id", True), ("org", False)]),
        ]
    )

    with pytest.raises(SQLMeshError):
        graph.find_path("fact", "dimension")
