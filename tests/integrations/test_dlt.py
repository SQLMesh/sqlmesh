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
