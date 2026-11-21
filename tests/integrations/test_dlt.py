from sqlmesh.integrations.dlt import generate_incremental_model


def test_generate_incremental_model_filters_on_timestamp_macros() -> None:
    # The DLT-generated model's time column is a timestamp
    # (TO_TIMESTAMP(...)). It must therefore be filtered with the inclusive
    # timestamp macros @start_ts/@end_ts, not the categorical date macros
    # @start_ds/@end_ds, which both render midnight and exclude any rows past
    # 00:00:00 on a single-day run.
    model = generate_incremental_model(
        "dataset_sqlmesh.incremental_equipment",
        "  CAST(c.item_id AS BIGINT) AS item_id",
        "",
        "dataset.equipment",
        "duckdb",
        "c._dlt_load_id",
    )

    assert "BETWEEN @start_ts AND @end_ts" in model
    assert "@start_ds" not in model
    assert "@end_ds" not in model


def test_generate_dlt_models_and_settings_supports_ducklake_destination(monkeypatch) -> None:
    import sys
    import types
    from sqlmesh.integrations.dlt import generate_dlt_models_and_settings

    class _FakeDestination:
        @staticmethod
        def to_name(_dest) -> str:
            return "ducklake"

    class _FakeSchema:
        loads_table_name = "_dlt_loads"
        _dlt_tables_prefix = "_dlt_"
        tables = {
            "items": {
                "columns": {
                    "id": {"name": "id", "data_type": "bigint", "primary_key": True},
                    "_dlt_load_id": {"name": "_dlt_load_id", "data_type": "text"},
                }
            }
        }

    class _FakePipeline:
        destination = _FakeDestination()
        default_schema = _FakeSchema()
        dataset_name = "mre"

        def _get_load_storage(self):
            return types.SimpleNamespace(list_loaded_packages=lambda: [])

    dlt_mod = types.ModuleType("dlt")
    dlt_mod.__path__ = []
    dlt_mod.attach = lambda **kwargs: _FakePipeline()
    dlt_pipeline_pkg = types.ModuleType("dlt.pipeline")
    dlt_pipeline_exc = types.ModuleType("dlt.pipeline.exceptions")
    dlt_pipeline_exc.CannotRestorePipelineException = Exception
    dlt_common = types.ModuleType("dlt.common")
    dlt_pipeline = types.ModuleType("dlt.common.pipeline")
    dlt_pipeline.get_dlt_pipelines_dir = lambda: "/tmp/dlt"
    dlt_schema = types.ModuleType("dlt.common.schema")
    dlt_schema_utils = types.ModuleType("dlt.common.schema.utils")
    dlt_schema_utils.is_complete_column = lambda col: bool(col.get("name") and col.get("data_type"))
    dlt_schema_utils.has_table_seen_data = lambda table: True

    for name, mod in [
        ("dlt", dlt_mod),
        ("dlt.pipeline", dlt_pipeline_pkg),
        ("dlt.pipeline.exceptions", dlt_pipeline_exc),
        ("dlt.common", dlt_common),
        ("dlt.common.pipeline", dlt_pipeline),
        ("dlt.common.schema", dlt_schema),
        ("dlt.common.schema.utils", dlt_schema_utils),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    models, connection_config, start_date = generate_dlt_models_and_settings("mre_ducklake", "duckdb")
    assert connection_config is None
    assert start_date
    assert any(name == "mre_sqlmesh.incremental_items" for name, _ in models)
