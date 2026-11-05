from sqlmesh import Context
from sqlmesh.core.linter.helpers import (
    read_range_from_file,
    get_range_of_model_block,
    get_range_of_a_key_in_model_block,
)
from sqlmesh.core.model import SqlModel


def test_get_position_of_model_block(copy_to_temp_path):
    context = Context(paths=copy_to_temp_path("examples/sushi"))

    sql_models = [
        model
        for model in context.models.values()
        if isinstance(model, SqlModel)
        and model._path is not None
        and str(model._path).endswith(".sql")
    ]
    assert len(sql_models) > 0

    for model in sql_models:
        dialect = model.dialect
        assert dialect is not None

        path = model._path
        assert path is not None

        with open(path, "r", encoding="utf-8") as file:
            content = file.read()

        as_lines = content.splitlines()

        range = get_range_of_model_block(content, dialect)
        assert range is not None

        #  Check that the range starts with MODEL and ends with ;
        read_range = read_range_from_file(path, range)
        assert read_range.startswith("MODEL")
        assert read_range.endswith(";")


def test_get_range_of_a_key_in_model_block_testing_on_sushi(copy_to_temp_path):
    context = Context(paths=copy_to_temp_path("examples/sushi"))

    sql_models = [
        model
        for model in context.models.values()
        if isinstance(model, SqlModel)
        and model._path is not None
        and str(model._path).endswith(".sql")
    ]
    assert len(sql_models) > 0

    # Test that the function works for all keys in the model block
    for model in sql_models:
        possible_keys = [
            "name",
            "tags",
            "description",
            "column_descriptions",
            "owner",
            "cron",
            "dialect",
        ]

        dialect = model.dialect
        assert dialect is not None

        path = model._path
        assert path is not None

        with open(path, "r", encoding="utf-8") as file:
            content = file.read()

        count_properties_checked = 0

        for key in possible_keys:
            ranges = get_range_of_a_key_in_model_block(content, dialect, key)

            if ranges:
                key_range, value_range = ranges
                read_key = read_range_from_file(path, key_range)
                assert read_key.lower() == key.lower()
                # Value range should be non-empty
                read_value = read_range_from_file(path, value_range)
                assert len(read_value) > 0
                count_properties_checked += 1

        assert count_properties_checked > 0

    # Test that the function works for different kind of value blocks
    tests = [
        ("sushi.customers", "name", "sushi.customers"),
        (
            "sushi.customers",
            "tags",
            "(pii, fact)",
        ),
        ("sushi.customers", "description", "'Sushi customer data'"),
        (
            "sushi.customers",
            "column_descriptions",
            "(    customer_id = 'customer_id uniquely identifies customers'  )",
        ),
        ("sushi.customers", "owner", "jen"),
        ("sushi.customers", "cron", "'@daily'"),
    ]
    for model_name, key, value in tests:
        model = context.get_model(model_name)
        assert model is not None

        dialect = model.dialect
        assert dialect is not None

        path = model._path
        assert path is not None

        with open(path, "r", encoding="utf-8") as file:
            content = file.read()

        ranges = get_range_of_a_key_in_model_block(content, dialect, key)
        assert ranges is not None, f"Could not find key '{key}' in model '{model_name}'"

        key_range, value_range = ranges
        read_key = read_range_from_file(path, key_range)
        assert read_key.lower() == key.lower()

        read_value = read_range_from_file(path, value_range)
        assert read_value == value
