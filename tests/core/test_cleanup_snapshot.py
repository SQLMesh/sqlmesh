from types import SimpleNamespace
import pytest

from sqlmesh.core.snapshot.evaluator import SnapshotEvaluator


def test_cleanup_snapshot_skips_when_get_data_object_raises(monkeypatch):
    completed = []

    class _FakeStrategy:
        def delete(self, *args, **kwargs):
            raise RuntimeError("drop failed")

    class _FakeAdapter:
        def get_data_object(self, _table_name):
            raise RuntimeError("catalog switch failed")

    monkeypatch.setattr(
        "sqlmesh.core.snapshot.evaluator._evaluation_strategy",
        lambda _snap, _adapter: _FakeStrategy(),
    )

    fake_table_info = SimpleNamespace(table_name=lambda is_deployable=False: "cat.sch.tbl", physical_schema="sch")
    fake_snapshot = SimpleNamespace(table_info=fake_table_info)

    evaluator = SnapshotEvaluator.__new__(SnapshotEvaluator)
    evaluator._cleanup_snapshot(
        fake_snapshot,
        dev_table_only=True,
        adapter=_FakeAdapter(),
        on_complete=completed.append,
    )
    assert completed == ["cat.sch.tbl"]


def test_cleanup_snapshot_reraises_when_table_still_exists(monkeypatch):
    class _FakeStrategy:
        def delete(self, *args, **kwargs):
            raise RuntimeError("drop failed")

    class _FakeAdapter:
        def get_data_object(self, _table_name):
            return object()

    monkeypatch.setattr(
        "sqlmesh.core.snapshot.evaluator._evaluation_strategy",
        lambda _snap, _adapter: _FakeStrategy(),
    )

    fake_table_info = SimpleNamespace(table_name=lambda is_deployable=False: "cat.sch.tbl", physical_schema="sch")
    fake_snapshot = SimpleNamespace(table_info=fake_table_info)

    evaluator = SnapshotEvaluator.__new__(SnapshotEvaluator)
    with pytest.raises(RuntimeError, match="drop failed"):
        evaluator._cleanup_snapshot(
            fake_snapshot,
            dev_table_only=True,
            adapter=_FakeAdapter(),
            on_complete=None,
        )
