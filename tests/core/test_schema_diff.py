import typing as t

import pytest
from sqlglot import exp

from sqlmesh.core.engine_adapter import create_engine_adapter
from sqlmesh.core.schema_diff import (
    SchemaDiffer,
    TableAlterColumn,
    TableAlterColumnPosition,
    TableAlterOperation,
    get_schema_differ,
    has_additive_alteration,
    has_drop_alteration,
    prepare_columns_for_static_diff,
    TableAlterAddColumnOperation,
    TableAlterDropColumnOperation,
    TableAlterChangeColumnTypeOperation,
    NestedSupport,
)
from sqlmesh.utils.errors import SQLMeshError


def test_schema_diff_calculate():
    alter_operations = SchemaDiffer(
        **{
            "support_positional_add": False,
            "nested_support": NestedSupport.NONE,
            "array_element_selector": "",
            "compatible_types": {
                exp.DataType.build("STRING"): {exp.DataType.build("INT")},
            },
        }
    ).compare_columns(
        exp.to_table("apply_to_table"),
        {
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("STRING"),
            "price": exp.DataType.build("DOUBLE"),
            "ds": exp.DataType.build("STRING"),
        },
        {
            "name": exp.DataType.build("INT"),
            "id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
            "new_column": exp.DataType.build("DOUBLE"),
        },
    )

    assert [x.expression.sql() for x in alter_operations] == [
        """ALTER TABLE apply_to_table DROP COLUMN price""",
        """ALTER TABLE apply_to_table ADD COLUMN new_column DOUBLE""",
        """ALTER TABLE apply_to_table ALTER COLUMN name SET DATA TYPE INT""",
    ]


def test_schema_diff_drop_cascade():
    alter_expressions = SchemaDiffer(
        **{
            "support_positional_add": False,
            "nested_support": NestedSupport.NONE,
            "array_element_selector": "",
            "drop_cascade": True,
        }
    ).compare_columns(
        exp.to_table("apply_to_table"),
        {
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("STRING"),
            "price": exp.DataType.build("DOUBLE"),
        },
        {
            "id": exp.DataType.build("INT"),
            "name": exp.DataType.build("STRING"),
        },
    )

    assert [x.expression.sql() for x in alter_expressions] == [
        """ALTER TABLE apply_to_table DROP COLUMN price CASCADE"""
    ]


def test_schema_diff_calculate_type_transitions():
    alter_expressions = SchemaDiffer(
        **{
            "support_positional_add": False,
            "nested_support": NestedSupport.NONE,
            "array_element_selector": "",
            "compatible_types": {
                exp.DataType.build("STRING"): {exp.DataType.build("INT")},
            },
        }
    ).compare_columns(
        exp.to_table("apply_to_table"),
        {
            "id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("STRING"),
        },
        {
            "id": exp.DataType.build("BIGINT"),
            "ds": exp.DataType.build("INT"),
        },
    )

    assert [x.expression.sql() for x in alter_expressions] == [
        """ALTER TABLE apply_to_table DROP COLUMN id""",
        """ALTER TABLE apply_to_table ADD COLUMN id BIGINT""",
        """ALTER TABLE apply_to_table ALTER COLUMN ds SET DATA TYPE INT""",
    ]


@pytest.mark.parametrize(
    "current_struct, new_struct, expected_diff, config",
    [
        # ###########
        # # Add Tests
        # ###########
        # No diff
        ("STRUCT<id INT, name STRING, age INT>", "STRUCT<id INT, name STRING, age INT>", [], {}),
        # Add root level column at the end
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id INT, name STRING, age INT, address STRING>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, age INT, address STRING>"
                    ),
                    array_element_selector="",
                )
            ],
            {},
        ),
        # Add root level column at the beginning
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<address STRING, id INT, name STRING, age INT>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<address STRING, id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.first(),
                )
            ],
            dict(support_positional_add=True),
        ),
        # Add root level column in the middle
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id INT, address STRING, name STRING, age INT>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address STRING, name STRING, age INT>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.middle(after="id"),
                )
            ],
            dict(support_positional_add=True),
        ),
        # Add columns at the beginning, middle, and end
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<address STRING, id INT, address2 STRING, name STRING, age INT, address3 STRING>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<address STRING, id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.first(),
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address2")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<address STRING, id INT, address2 STRING, name STRING, age INT>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.middle(after="id"),
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address3")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<address STRING, id INT, address2 STRING, name STRING, age INT, address3 STRING>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.last(after="age"),
                ),
            ],
            dict(support_positional_add=True),
        ),
        # Add two columns next to each other at the start
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<address STRING, address2 STRING, id INT, name STRING, age INT>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<address STRING, id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.first(),
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address2")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<address STRING, address2 STRING, id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.middle(after="address"),
                ),
            ],
            dict(support_positional_add=True),
        ),
        ############
        # Drop Tests
        ############
        # Drop root level column at the start
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<name STRING, age INT>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("id")],
                    expected_table_struct=exp.DataType.build("STRUCT<name STRING, age INT>"),
                    array_element_selector="",
                )
            ],
            {},
        ),
        # Drop root level column in the middle
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id INT, age INT>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, age INT>"),
                    array_element_selector="",
                )
            ],
            {},
        ),
        # Drop root level column at the end
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id INT, name STRING>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, name STRING>"),
                    array_element_selector="",
                )
            ],
            {},
        ),
        # Drop root level column at start, middle, and end
        (
            "STRUCT<id INT, name STRING, middle STRING, address STRING, age INT>",
            "STRUCT<name STRING, address STRING>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("id")],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<name STRING, middle STRING, address STRING, age INT>"
                    ),
                    array_element_selector="",
                ),
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("middle")],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<name STRING, address STRING, age INT>"
                    ),
                    array_element_selector="",
                ),
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    expected_table_struct=exp.DataType.build("STRUCT<name STRING, address STRING>"),
                    array_element_selector="",
                ),
            ],
            {},
        ),
        # Drop two columns next to each other at the start
        (
            "STRUCT<address STRING, address2 STRING, id INT, name STRING, age INT>",
            "STRUCT<id INT, name STRING, age INT>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<address2 STRING, id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                ),
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address2")],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                ),
            ],
            {},
        ),
        #############
        # Move Tests
        #############
        # Move root level column at the start
        ("STRUCT<id INT, name STRING, age INT>", "STRUCT<name STRING, id INT, age INT>", [], {}),
        # Move root level column in the middle
        ("STRUCT<id INT, name STRING, age INT>", "STRUCT<id INT, age INT, name STRING>", [], {}),
        # Move root level column at the end
        ("STRUCT<id INT, name STRING, age INT>", "STRUCT<age INT, id INT, name STRING>", [], {}),
        # ###################
        # # Type Change Tests
        # ###################
        # # Change root level column type that is allowed
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id STRING, name STRING, age INT>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("id")],
                    column_type=exp.DataType.build("STRING"),
                    current_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id STRING, name STRING, age INT>"
                    ),
                    array_element_selector="",
                )
            ],
            dict(
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("STRING")},
                }
            ),
        ),
        # ############
        # # Mix Tests
        # ############
        # # Add, drop, and change type
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id STRING, age INT, address STRING>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, age INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("id")],
                    column_type=exp.DataType.build("STRING"),
                    current_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id STRING, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("STRING")},
                }
            ),
        ),
        # ##############
        # # Struct Tests
        # ##############
        # Add a column to the start of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_d INT, col_a INT, col_b INT, col_c INT>>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_d INT, col_a INT, col_b INT, col_c INT>>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.first(),
                ),
            ],
            dict(support_positional_add=True, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # Add a column to the end of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT, col_d INT>>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT, col_d INT>>"
                    ),
                    array_element_selector="",
                    position=TableAlterColumnPosition.last(after="col_c"),
                ),
            ],
            dict(support_positional_add=True, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # Add a column to the middle of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_d INT, col_b INT, col_c INT>>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_d INT, col_b INT, col_c INT>>"
                    ),
                    position=TableAlterColumnPosition.middle(after="col_a"),
                    array_element_selector="",
                ),
            ],
            dict(support_positional_add=True, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # Add two columns at the start of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_d INT, col_e INT, col_a INT, col_b INT, col_c INT>>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_d INT, col_a INT, col_b INT, col_c INT>>"
                    ),
                    position=TableAlterColumnPosition.first(),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_e"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_d INT, col_e INT, col_a INT, col_b INT, col_c INT>>"
                    ),
                    position=TableAlterColumnPosition.middle(after="col_d"),
                    array_element_selector="",
                ),
            ],
            dict(support_positional_add=True, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # Add columns in different levels of nesting of structs
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT, col_d INT>, txt TEXT>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.primitive("txt"),
                    ],
                    column_type=exp.DataType.build("TEXT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>, txt TEXT>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT, col_d INT>, txt TEXT>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(support_positional_add=False, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # Remove a column from the start of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_b INT, col_c INT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_a"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_b INT, col_c INT>>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
            ),
        ),
        # Remove a column from the end of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_c"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT>>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
            ),
        ),
        # Remove a column from the middle of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_c INT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_b"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_c INT>>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
            ),
        ),
        # Remove a column from a struct where nested drop is not supported
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                    ],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT>>"
                    ),
                    column_type=exp.DataType.build("STRUCT<col_a INT, col_b INT>"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                nested_support=NestedSupport.ALL_BUT_DROP,
            ),
        ),
        # Remove two columns from the start of a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_c INT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_a"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_b INT, col_c INT>>"
                    ),
                    array_element_selector="",
                ),
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_b"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_c INT>>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
            ),
        ),
        # Change a column type in a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c TEXT>>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_c"),
                    ],
                    column_type=exp.DataType.build("TEXT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c TEXT>>"
                    ),
                    current_type=exp.DataType.build("INT"),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL_BUT_DROP,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("TEXT")},
                },
            ),
        ),
        # Add, remove and change a column in a struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_d INT, col_b INT, col_e INT, col_c TEXT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_a"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_b INT, col_c INT>>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_d INT, col_b INT, col_c INT>>"
                    ),
                    position=TableAlterColumnPosition.first(),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_e"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_d INT, col_b INT, col_e INT, col_c INT>>"
                    ),
                    position=TableAlterColumnPosition.middle(after="col_b"),
                    array_element_selector="",
                ),
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_c"),
                    ],
                    column_type=exp.DataType.build("TEXT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_d INT, col_b INT, col_e INT, col_c TEXT>>"
                    ),
                    current_type=exp.DataType.build("INT"),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("TEXT")},
                },
            ),
        ),
        # Add, remove and change a column from a struct where nested drop is not supported
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b TEXT, col_d INT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                    ],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b TEXT, col_d INT>>"
                    ),
                    column_type=exp.DataType.build("STRUCT<col_a INT, col_b TEXT, col_d INT>"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                nested_support=NestedSupport.ALL_BUT_DROP,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("TEXT")},
                },
            ),
        ),
        # Add and remove from outer and nested struct
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, nested_info STRUCT<nest_col_a INT, nest_col_b INT>>>",
            "STRUCT<id INT, info STRUCT<col_a INT, nested_info STRUCT<nest_col_a INT, nest_col_c INT>, col_c INT>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_b"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, nested_info STRUCT<nest_col_a INT, nest_col_b INT>>>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_c"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, nested_info STRUCT<nest_col_a INT, nest_col_b INT>, col_c INT>>"
                    ),
                    position=TableAlterColumnPosition.last("nested_info"),
                    array_element_selector="",
                ),
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.struct("nested_info"),
                        TableAlterColumn.primitive("nest_col_b"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, nested_info STRUCT<nest_col_a INT>, col_c INT>>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.struct("nested_info"),
                        TableAlterColumn.primitive("nest_col_c"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, nested_info STRUCT<nest_col_a INT, nest_col_c INT>, col_c INT>>"
                    ),
                    position=TableAlterColumnPosition.last("nest_col_a"),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
            ),
        ),
        # #####################
        # # Array Struct Tests
        # #####################
        # Add column to array of structs
        (
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>>",
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_d INT, col_c INT>>>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.array_of_struct("infos"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_d INT, col_c INT>>>"
                    ),
                    position=TableAlterColumnPosition.middle("col_b"),
                    array_element_selector="",
                ),
            ],
            dict(support_positional_add=True, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # Remove column from array of structs
        (
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>>",
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_c INT>>>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.array_of_struct("infos"),
                        TableAlterColumn.primitive("col_b"),
                    ],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_c INT>>>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
            ),
        ),
        # Alter column type in array of structs
        (
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>>",
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c TEXT>>>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.array_of_struct("infos"),
                        TableAlterColumn.primitive("col_c"),
                    ],
                    column_type=exp.DataType.build("TEXT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c TEXT>>>"
                    ),
                    current_type=exp.DataType.build("INT"),
                    array_element_selector="",
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL_BUT_DROP,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("TEXT")},
                },
            ),
        ),
        # Add columns to struct of array within different nesting levels
        (
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>>",
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT, col_d INT >>, col_e INT>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("col_e")],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>, col_e INT>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.array_of_struct("infos"),
                        TableAlterColumn.primitive("col_d"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT,  col_c INT, col_d INT>>, col_e INT>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(support_positional_add=False, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # Add an array of primitives
        (
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>>",
            "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>, values ARRAY<INT>>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.array_of_primitive("values"),
                    ],
                    column_type=exp.DataType.build("ARRAY<INT>"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, infos ARRAY<STRUCT<col_a INT, col_b INT, col_c INT>>, values ARRAY<INT>>"
                    ),
                    position=TableAlterColumnPosition.last("infos"),
                    array_element_selector="",
                ),
            ],
            dict(support_positional_add=True, nested_support=NestedSupport.ALL_BUT_DROP),
        ),
        # untyped array to support Snowflake
        (
            "STRUCT<id INT, ids ARRAY>",
            "STRUCT<id INT, ids ARRAY>",
            [],
            {},
        ),
        # Primitive to untyped array
        (
            "STRUCT<id INT, ids INT>",
            "STRUCT<id INT, ids ARRAY>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("ids")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("ids")],
                    column_type=exp.DataType.build("ARRAY"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, ids ARRAY>"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            {},
        ),
        # untyped array to primitive
        (
            "STRUCT<id INT, ids ARRAY>",
            "STRUCT<id INT, ids INT>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.array_of_primitive("ids"),
                    ],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.array_of_primitive("ids"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, ids INT>"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            {},
        ),
        # ############
        # Precision Tests
        # ############
        # Identical precision is a no-op with no changes
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR(120)>",
            [],
            {},
        ),
        # Increase the precision of a type is ALTER
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR(121)>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(121)"),
                    current_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(121)>"
                    ),
                    array_element_selector="",
                )
            ],
            {},
        ),
        # Increase the precision of a type is ALTER when the type is supported
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR(121)>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(121)"),
                    current_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(121)>"
                    ),
                    array_element_selector="",
                )
            ],
            dict(
                precision_increase_allowed_types={exp.DataType.build("VARCHAR").this},
            ),
        ),
        # Increase the precision of a type is DROP/ADD when the type is not supported
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR(121)>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(121)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(121)>"
                    ),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                precision_increase_allowed_types={exp.DataType.build("DECIMAL").this},
            ),
        ),
        # Decrease the precision of a type is DROP/ADD
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR(100)>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(100)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(100)>"
                    ),
                    position=TableAlterColumnPosition.last("id"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL,
            ),
        ),
        # Type with precision to same type with no precision and no default is DROP/ADD
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, address VARCHAR>"),
                    position=TableAlterColumnPosition.last("id"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                support_positional_add=True,
            ),
        ),
        # Type with no precision and no default to same type with precision is DROP/ADD
        (
            "STRUCT<id INT, address VARCHAR>",
            "STRUCT<id INT, address VARCHAR(120)>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(120)>"
                    ),
                    position=TableAlterColumnPosition.last("id"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                support_positional_add=True,
            ),
        ),
        # Increase precision of a type from a default is ALTER
        (
            "STRUCT<id INT, address VARCHAR>",  # default of 1 --> VARCHAR(1)
            "STRUCT<id INT, address VARCHAR(2)>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(2)"),
                    current_type=exp.DataType.build("VARCHAR"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, address VARCHAR(2)>"),
                    array_element_selector="",
                )
            ],
            dict(
                parameterized_type_defaults={
                    exp.DataType.build("VARCHAR").this: [(1,)],
                },
            ),
        ),
        # Decrease precision of a type to a default is DROP/ADD
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR>",  # default of 1 --> VARCHAR(1)
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, address VARCHAR>"),
                    position=TableAlterColumnPosition.last("id"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                parameterized_type_defaults={
                    exp.DataType.build("VARCHAR").this: [(1,)],
                },
                support_positional_add=True,
            ),
        ),
        # Change the precision of a type to exact "max" value is ALTER
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR(max)>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(max)"),
                    current_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(max)>"
                    ),
                    array_element_selector="",
                )
            ],
            dict(
                max_parameter_length={
                    exp.DataType.build("VARCHAR").this: 120,
                },
            ),
        ),
        # Increase the precision of a type to larger "max" is ALTER
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR(max)>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(max)"),
                    current_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(max)>"
                    ),
                    array_element_selector="",
                )
            ],
            dict(
                max_parameter_length={
                    exp.DataType.build("VARCHAR").this: 121,
                },
            ),
        ),
        # Decrease the precision of a type from "max" to smaller value is DROP/ADD
        (
            "STRUCT<id INT, address VARCHAR(max)>",
            "STRUCT<id INT, address VARCHAR(120)>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(120)>"
                    ),
                    position=TableAlterColumnPosition.last("id"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                support_positional_add=True,
                max_parameter_length={
                    exp.DataType.build("VARCHAR").this: 121,
                },
            ),
        ),
        # Increase the precision of a type to no-precision unlimited is ALTER
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address VARCHAR>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR"),
                    current_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, address VARCHAR>"),
                    array_element_selector="",
                )
            ],
            dict(
                types_with_unlimited_length={
                    exp.DataType.build("VARCHAR").this: {
                        exp.DataType.build("VARCHAR").this,
                    },
                }
            ),
        ),
        # Decrease the precision of a type from no-precision unlimited to any precision is DROP/ADD
        (
            "STRUCT<id INT, address VARCHAR>",
            "STRUCT<id INT, address VARCHAR(120)>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, address VARCHAR(120)>"
                    ),
                    position=TableAlterColumnPosition.last("id"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                support_positional_add=True,
                types_with_unlimited_length={
                    exp.DataType.build("VARCHAR").this: {
                        exp.DataType.build("VARCHAR").this,
                    },
                },
            ),
        ),
        # Increase the precision of one type to a different no-precision unlimited type is ALTER
        (
            "STRUCT<id INT, address VARCHAR(120)>",
            "STRUCT<id INT, address TEXT>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("TEXT"),
                    current_type=exp.DataType.build("VARCHAR(120)"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, address TEXT>"),
                    array_element_selector="",
                )
            ],
            dict(
                types_with_unlimited_length={
                    exp.DataType.build("TEXT").this: {
                        exp.DataType.build("VARCHAR").this,
                    },
                }
            ),
        ),
        # ############
        # Coerce Tests
        # ############
        # Single coercion results in no operation
        (
            "STRUCT<id INT, name STRING, revenue FLOAT>",
            "STRUCT<id INT, name STRING, revenue INT>",
            [],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL_BUT_DROP,
                support_coercing_compatible_types=True,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("FLOAT")},
                },
            ),
        ),
        (
            "STRUCT<id INT, name STRING, revenue FLOAT>",
            "STRUCT<id INT, name STRING, revenue INT>",
            [],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL_BUT_DROP,
                coerceable_types={
                    exp.DataType.build("FLOAT"): {exp.DataType.build("INT")},
                },
            ),
        ),
        (
            "STRUCT<id INT, name STRING, revenue FLOAT>",
            "STRUCT<id INT, name INT, revenue INT>",
            [],
            dict(
                support_positional_add=True,
                nested_support=NestedSupport.ALL_BUT_DROP,
                support_coercing_compatible_types=True,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("FLOAT")},
                },
                coerceable_types={
                    exp.DataType.build("STRING"): {exp.DataType.build("INT")},
                },
            ),
        ),
        # Coercion with an alter results in a single alter
        (
            "STRUCT<id INT, name STRING, revenue FLOAT, total INT>",
            "STRUCT<id INT, name STRING, revenue INT, total FLOAT>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("total")],
                    column_type=exp.DataType.build("FLOAT"),
                    current_type=exp.DataType.build("INT"),
                    # Note that the resulting table struct will not match what we defined as the desired
                    # result since it could be coerced
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, revenue FLOAT, total FLOAT>"
                    ),
                    array_element_selector="",
                )
            ],
            dict(
                support_positional_add=False,
                nested_support=NestedSupport.ALL_BUT_DROP,
                support_coercing_compatible_types=True,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("FLOAT")},
                },
            ),
        ),
        # ###################
        # Ignore Nested Tests
        # ###################
        # Remove nested col_c
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT>>",
            [],
            dict(nested_support=NestedSupport.IGNORE),
        ),
        # Add nested col_d
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT, col_d INT>>",
            [],
            dict(nested_support=NestedSupport.IGNORE),
        ),
        # Change nested col_c to incompatible type
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c DATE>>",
            [],
            dict(nested_support=NestedSupport.IGNORE),
        ),
        # Change nested col_c to compatible type
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c STRING>>",
            [],
            dict(
                nested_support=NestedSupport.IGNORE,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("STRING")},
                },
            ),
        ),
        # Mix of ignored nested and non-nested changes
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>, age INT>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c DATE>, age STRING, new_col INT>",
            [
                # `col_c` change is ignored
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("new_col")],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>, age INT, new_col INT>"
                    ),
                    position=TableAlterColumnPosition.last("age"),
                    array_element_selector="",
                ),
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    column_type=exp.DataType.build("STRING"),
                    current_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>, age STRING, new_col INT>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                nested_support=NestedSupport.IGNORE,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("STRING")},
                },
                support_positional_add=True,
            ),
        ),
        # ############################
        # Change Data Type Destructive
        # ############################
        (
            "STRUCT<id INT, age INT>",
            "STRUCT<id INT, age STRING>",
            [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    column_type=exp.DataType.build("STRING"),
                    current_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, age STRING>"),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                treat_alter_data_type_as_destructive=True,
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("STRING")},
                },
            ),
        ),
    ],
)
def test_struct_diff(
    current_struct,
    new_struct,
    expected_diff: t.List[TableAlterOperation],
    config: t.Dict[str, t.Any],
):
    resolver = SchemaDiffer(**config)
    operations = resolver._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
    )
    assert operations == expected_diff


def test_schema_diff_calculate_duckdb(duck_conn):
    engine_adapter = create_engine_adapter(lambda: duck_conn, "duckdb")

    engine_adapter.create_table(
        "apply_to_table",
        {
            "id": exp.DataType.build("int"),
            "name": exp.DataType.build("text"),
            "price": exp.DataType.build("double"),
            "ds": exp.DataType.build("text"),
        },
    )

    engine_adapter.create_table(
        "schema_from_table",
        {
            "name": exp.DataType.build("int"),
            "id": exp.DataType.build("int"),
            "ds": exp.DataType.build("text"),
            "new_column": exp.DataType.build("double"),
        },
    )

    alter_expressions = engine_adapter.get_alter_operations("apply_to_table", "schema_from_table")
    engine_adapter.alter_table(alter_expressions)
    assert engine_adapter.columns("apply_to_table") == {
        "id": exp.DataType.build("int"),
        "ds": exp.DataType.build("text"),
        "new_column": exp.DataType.build("double"),
        "name": exp.DataType.build("int"),
    }


def test_schema_diff_alter_op_column():
    nested = TableAlterAddColumnOperation(
        target_table=exp.to_table("apply_to_table"),
        column_parts=[
            TableAlterColumn.array_of_struct("nested"),
            TableAlterColumn.primitive("col_a"),
        ],
        column_type=exp.DataType.build("INT"),
        expected_table_struct=exp.DataType.build("STRUCT<id INT, nested ARRAY<STRUCT<col_a INT>>>"),
        position=TableAlterColumnPosition.last("id"),
        array_element_selector="",
    )
    assert nested.column.sql() == "nested.col_a"
    nested_complete_column = TableAlterAddColumnOperation(
        target_table=exp.to_table("apply_to_table"),
        column_parts=[
            TableAlterColumn.array_of_struct("nested_1", quoted=True),
            TableAlterColumn.struct("nested_2"),
            TableAlterColumn.array_of_struct("nested_3"),
            TableAlterColumn.primitive("col_a", quoted=True),
        ],
        column_type=exp.DataType.build("INT"),
        expected_table_struct=exp.DataType.build(
            """STRUCT<id INT, "nested_1" ARRAY<STRUCT<nested_2 STRUCT<nested_3 ARRAY<STRUCT<"col_a" INT>>>>>>"""
        ),
        position=TableAlterColumnPosition.last("id"),
        array_element_selector="",
    )
    assert nested_complete_column.column.sql() == '"nested_1".nested_2.nested_3."col_a"'
    nested_one_more_complete_column = TableAlterAddColumnOperation(
        target_table=exp.to_table("apply_to_table"),
        column_parts=[
            TableAlterColumn.array_of_struct("nested_1", quoted=True),
            TableAlterColumn.struct("nested_2"),
            TableAlterColumn.array_of_struct("nested_3"),
            TableAlterColumn.struct("nested_4"),
            TableAlterColumn.primitive("col_a", quoted=True),
        ],
        column_type=exp.DataType.build("INT"),
        expected_table_struct=exp.DataType.build(
            """STRUCT<id INT, "nested_1" ARRAY<STRUCT<nested_2 STRUCT<nested_3 ARRAY<STRUCT<nested_4 STRUCT<"col_a" INT>>>>>>>"""
        ),
        position=TableAlterColumnPosition.last("id"),
        array_element_selector="",
    )
    assert (
        nested_one_more_complete_column.column.sql()
        == '"nested_1".nested_2.nested_3.nested_4."col_a"'
    )
    super_nested = TableAlterAddColumnOperation(
        target_table=exp.to_table("apply_to_table"),
        column_parts=[
            TableAlterColumn.array_of_struct("nested_1", quoted=True),
            TableAlterColumn.struct("nested_2"),
            TableAlterColumn.array_of_struct("nested_3"),
            TableAlterColumn.struct("nested_4"),
            TableAlterColumn.struct("nested_5"),
            TableAlterColumn.struct("nested_6", quoted=True),
            TableAlterColumn.struct("nested_7"),
            TableAlterColumn.array_of_struct("nested_8"),
            TableAlterColumn.primitive("col_a", quoted=True),
        ],
        column_type=exp.DataType.build("INT"),
        expected_table_struct=exp.DataType.build(
            """STRUCT<id INT, "nested_1" ARRAY<STRUCT<nested_2 STRUCT<nested_3 ARRAY<STRUCT<nested_4 STRUCT<nested_5 STRUCT<"nested_6" STRUCT<nested_7 STRUCT<nested_8 ARRAY<STRUCT<"col_a" INT>>>>>>>>>>>>"""
        ),
        position=TableAlterColumnPosition.last("id"),
        array_element_selector="element",
    )
    assert (
        super_nested.column.sql()
        == '"nested_1".element.nested_2.nested_3.element.nested_4.nested_5."nested_6".nested_7.nested_8.element."col_a"'
    )


@pytest.mark.parametrize(
    "current_struct, new_struct, expected_diff_with_destructive, expected_diff_ignore_destructive, config",
    [
        # Simple DROP operation - should be ignored when ignore_destructive=True
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id INT, age INT>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, age INT>"),
                    array_element_selector="",
                )
            ],
            [],  # No operations when ignoring destructive
            {},
        ),
        # DROP + ADD operation (incompatible type change) - should be ignored when ignore_destructive=True
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id INT, name BIGINT, age INT>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, age INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    column_type=exp.DataType.build("BIGINT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, age INT, name BIGINT>"
                    ),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            [],  # No operations when ignoring destructive
            {},
        ),
        # Pure ADD operation - should work same way regardless of ignore_destructive
        (
            "STRUCT<id INT, name STRING>",
            "STRUCT<id INT, name STRING, new_col STRING>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("new_col")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, new_col STRING>"
                    ),
                    array_element_selector="",
                ),
            ],
            [
                # Same operation when ignoring destructive
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("new_col")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, new_col STRING>"
                    ),
                    array_element_selector="",
                ),
            ],
            {},
        ),
        # Mix of destructive and non-destructive operations
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id STRING, age INT, address STRING>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, age INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("id")],
                    column_type=exp.DataType.build("STRING"),
                    current_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id STRING, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
            ],
            [
                # Only non-destructive operations remain
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("id")],
                    column_type=exp.DataType.build("STRING"),
                    current_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id STRING, name STRING, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
            ],
            dict(
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("STRING")},
                }
            ),
        ),
    ],
)
def test_ignore_destructive_operations(
    current_struct,
    new_struct,
    expected_diff_with_destructive: t.List[TableAlterOperation],
    expected_diff_ignore_destructive: t.List[TableAlterOperation],
    config: t.Dict[str, t.Any],
):
    resolver = SchemaDiffer(**config)

    # Test with destructive operations allowed (default behavior)
    operations_with_destructive = resolver._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=False,
    )
    assert operations_with_destructive == expected_diff_with_destructive

    # Test with destructive operations ignored
    operations_ignore_destructive = resolver._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=True,
    )
    assert operations_ignore_destructive == expected_diff_ignore_destructive


def test_ignore_destructive_compare_columns():
    """Test ignore_destructive behavior in compare_columns method."""
    schema_differ = SchemaDiffer(
        support_positional_add=True,
        nested_support=NestedSupport.NONE,
        compatible_types={
            exp.DataType.build("INT"): {exp.DataType.build("STRING")},
        },
    )

    current = {
        "id": exp.DataType.build("INT"),
        "name": exp.DataType.build("STRING"),
        "to_drop": exp.DataType.build("DOUBLE"),
        "age": exp.DataType.build("INT"),
    }

    new = {
        "id": exp.DataType.build("STRING"),  # Compatible type change
        "name": exp.DataType.build("STRING"),
        "age": exp.DataType.build("INT"),
        "new_col": exp.DataType.build("DOUBLE"),  # New column
    }

    # With destructive operations allowed
    alter_expressions_with_destructive = schema_differ.compare_columns(
        "test_table", current, new, ignore_destructive=False
    )
    assert len(alter_expressions_with_destructive) == 3  # DROP + ADD + ALTER

    # With destructive operations ignored
    alter_expressions_ignore_destructive = schema_differ.compare_columns(
        "test_table", current, new, ignore_destructive=True
    )
    assert len(alter_expressions_ignore_destructive) == 2  # Only ADD + ALTER

    # Verify the operations are correct
    operations_sql = [expr.expression.sql() for expr in alter_expressions_ignore_destructive]
    add_column_found = any("ADD COLUMN new_col DOUBLE" in op for op in operations_sql)
    alter_column_found = any("ALTER COLUMN id SET DATA TYPE" in op for op in operations_sql)
    drop_column_found = any("DROP COLUMN to_drop" in op for op in operations_sql)

    assert add_column_found, f"ADD COLUMN not found in: {operations_sql}"
    assert alter_column_found, f"ALTER COLUMN not found in: {operations_sql}"
    assert not drop_column_found, f"DROP COLUMN should not be present in: {operations_sql}"


def test_ignore_destructive_nested_struct_without_support():
    """Test ignore_destructive with nested structs when nested_drop is not supported."""
    schema_differ = SchemaDiffer(
        nested_support=NestedSupport.ALL_BUT_DROP,  # This forces DROP+ADD for nested changes
    )

    current_struct = "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>>"
    new_struct = "STRUCT<id INT, info STRUCT<col_a INT, col_c INT>>"  # Removes col_b

    # With destructive operations allowed - should do DROP+ADD of entire struct
    operations_with_destructive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=False,
    )
    assert len(operations_with_destructive) == 2  # DROP struct + ADD struct
    assert isinstance(operations_with_destructive[0], TableAlterDropColumnOperation)
    assert isinstance(operations_with_destructive[1], TableAlterAddColumnOperation)

    # With destructive operations ignored - should do nothing
    operations_ignore_destructive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=True,
    )
    assert len(operations_ignore_destructive) == 0


def test_get_schema_differ():
    # Test that known dialects return SchemaDiffer instances
    for dialect in ["bigquery", "snowflake", "postgres", "databricks", "spark", "duckdb"]:
        schema_differ = get_schema_differ(dialect)
        assert isinstance(schema_differ, SchemaDiffer)

    # Test specific configurations
    # Databricks should support positional add and nested operations
    databricks_differ = get_schema_differ("databricks")
    assert databricks_differ.support_positional_add is True
    assert databricks_differ.nested_support == NestedSupport.ALL

    # BigQuery should have specific compatible types configured
    bigquery_differ = get_schema_differ("bigquery")
    assert len(bigquery_differ.compatible_types) > 0
    assert bigquery_differ.support_coercing_compatible_types is True

    # Snowflake should have parameterized type defaults
    snowflake_differ = get_schema_differ("snowflake")
    assert len(snowflake_differ.parameterized_type_defaults) > 0

    # Postgres should support drop cascade
    postgres_differ = get_schema_differ("postgres")
    assert postgres_differ.drop_cascade is True
    assert len(postgres_differ.types_with_unlimited_length) > 0

    # Test dialect aliases work correctly
    schema_differ_pg = get_schema_differ("postgresql")
    schema_differ_postgres = get_schema_differ("postgres")
    assert schema_differ_pg.drop_cascade == schema_differ_postgres.drop_cascade

    # Test unknown dialect returns default SchemaDiffer
    schema_differ_unknown = get_schema_differ("unknown_dialect")
    assert isinstance(schema_differ_unknown, SchemaDiffer)
    assert schema_differ_unknown.support_positional_add is False
    assert schema_differ_unknown.nested_support == NestedSupport.NONE

    # Test case insensitivity
    schema_differ_upper = get_schema_differ("BIGQUERY")
    schema_differ_lower = get_schema_differ("bigquery")
    assert (
        schema_differ_upper.support_coercing_compatible_types
        == schema_differ_lower.support_coercing_compatible_types
    )

    # Test override
    schema_differ_with_override = get_schema_differ("postgres", {"drop_cascade": False})
    assert schema_differ_with_override.drop_cascade is False


def test_ignore_destructive_edge_cases():
    """Test edge cases for ignore_destructive behavior."""
    schema_differ = SchemaDiffer(support_positional_add=True)

    # Test when all operations are destructive - should result in empty list
    current_struct = "STRUCT<col_a INT, col_b STRING, col_c DOUBLE>"
    new_struct = "STRUCT<>"  # Remove all columns

    operations_ignore_destructive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=True,
    )
    assert len(operations_ignore_destructive) == 0

    # Test when no operations are needed - should result in empty list regardless of ignore_destructive
    same_struct = "STRUCT<id INT, name STRING>"

    operations_same_with_destructive = schema_differ._from_structs(
        exp.DataType.build(same_struct),
        exp.DataType.build(same_struct),
        "apply_to_table",
        ignore_destructive=False,
    )
    operations_same_ignore_destructive = schema_differ._from_structs(
        exp.DataType.build(same_struct),
        exp.DataType.build(same_struct),
        "apply_to_table",
        ignore_destructive=True,
    )
    assert len(operations_same_with_destructive) == 0
    assert len(operations_same_ignore_destructive) == 0

    # Test when only ADD operations are needed - should be same regardless of ignore_destructive
    current_struct = "STRUCT<id INT>"
    new_struct = "STRUCT<id INT, name STRING, age INT>"

    operations_add_with_destructive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=False,
    )
    operations_add_ignore_destructive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=True,
    )
    assert len(operations_add_with_destructive) == 2  # ADD name, ADD age
    assert len(operations_add_ignore_destructive) == 2  # Same operations
    assert operations_add_with_destructive == operations_add_ignore_destructive


@pytest.mark.parametrize(
    "current_struct, new_struct, expected_diff_with_additive, expected_diff_ignore_additive, config",
    [
        # Simple ADD operation - should be ignored when ignore_additive=True
        (
            "STRUCT<id INT, name STRING>",
            "STRUCT<id INT, name STRING, age INT>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                )
            ],
            [],  # No operations when ignoring additive
            {},
        ),
        # Multiple ADD operations - should all be ignored when ignore_additive=True
        (
            "STRUCT<id INT>",
            "STRUCT<id INT, name STRING, age INT, address STRING>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, name STRING>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, age INT>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, name STRING, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
            ],
            [],  # No operations when ignoring additive
            {},
        ),
        # Pure DROP operation - should work same way regardless of ignore_additive
        (
            "STRUCT<id INT, name STRING, age INT>",
            "STRUCT<id INT, name STRING>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, name STRING>"),
                    array_element_selector="",
                ),
            ],
            [
                # Same operation when ignoring additive
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("age")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, name STRING>"),
                    array_element_selector="",
                ),
            ],
            {},
        ),
        # Mix of additive and non-additive operations
        (
            "STRUCT<id INT, name STRING, age INT, something STRING>",
            "STRUCT<id STRING, age INT, address STRING, something INT>",
            [
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, age INT, something STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("address")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, age INT, something STRING, address STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("id")],
                    column_type=exp.DataType.build("STRING"),
                    current_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id STRING, age INT, something STRING, address STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("something")],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id STRING, age INT, address STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("something")],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id STRING, age INT, address STRING, something INT>"
                    ),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            [
                # Only non-additive operations remain (alter is considered additive since it was a compatible change)
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("name")],
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, age INT, something STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterDropColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("something")],
                    expected_table_struct=exp.DataType.build("STRUCT<id INT, age INT>"),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("something")],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, age INT, something INT>"
                    ),
                    array_element_selector="",
                    is_part_of_destructive_change=True,
                ),
            ],
            dict(
                compatible_types={
                    exp.DataType.build("INT"): {exp.DataType.build("STRING")},
                }
            ),
        ),
        # ADD operations with nested structs - should be ignored when ignore_additive=True
        (
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT>>",
            "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>, new_field STRING>",
            [
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[TableAlterColumn.primitive("new_field")],
                    column_type=exp.DataType.build("STRING"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT>, new_field STRING>"
                    ),
                    array_element_selector="",
                ),
                TableAlterAddColumnOperation(
                    target_table=exp.to_table("apply_to_table"),
                    column_parts=[
                        TableAlterColumn.struct("info"),
                        TableAlterColumn.primitive("col_c"),
                    ],
                    column_type=exp.DataType.build("INT"),
                    expected_table_struct=exp.DataType.build(
                        "STRUCT<id INT, info STRUCT<col_a INT, col_b INT, col_c INT>, new_field STRING>"
                    ),
                    array_element_selector="",
                ),
            ],
            [],  # No operations when ignoring additive
            dict(nested_support=NestedSupport.ALL_BUT_DROP),
        ),
    ],
)
def test_ignore_additive_operations(
    current_struct,
    new_struct,
    expected_diff_with_additive: t.List[TableAlterOperation],
    expected_diff_ignore_additive: t.List[TableAlterOperation],
    config: t.Dict[str, t.Any],
):
    resolver = SchemaDiffer(**config)

    # Test with additive operations allowed (default behavior)
    operations_with_additive = resolver._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_additive=False,
    )
    assert operations_with_additive == expected_diff_with_additive

    # Test with additive operations ignored
    operations_ignore_additive = resolver._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_additive=True,
    )
    assert operations_ignore_additive == expected_diff_ignore_additive


def test_ignore_additive_edge_cases():
    """Test edge cases for ignore_additive behavior."""
    schema_differ = SchemaDiffer(support_positional_add=True)

    # Test when all operations are additive - should result in empty list
    current_struct = "STRUCT<id INT>"
    new_struct = "STRUCT<id INT, col_a STRING, col_b DOUBLE, col_c INT>"  # Add all columns

    operations_ignore_additive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_additive=True,
    )
    assert len(operations_ignore_additive) == 0

    # Test when no operations are needed - should result in empty list regardless of ignore_additive
    same_struct = "STRUCT<id INT, name STRING>"

    operations_same_with_additive = schema_differ._from_structs(
        exp.DataType.build(same_struct),
        exp.DataType.build(same_struct),
        "apply_to_table",
        ignore_additive=False,
    )
    operations_same_ignore_additive = schema_differ._from_structs(
        exp.DataType.build(same_struct),
        exp.DataType.build(same_struct),
        "apply_to_table",
        ignore_additive=True,
    )
    assert len(operations_same_with_additive) == 0
    assert len(operations_same_ignore_additive) == 0

    # Test when only DROP operations are needed - should be same regardless of ignore_additive
    current_struct = "STRUCT<id INT, name STRING, age INT>"
    new_struct = "STRUCT<id INT>"

    operations_drop_with_additive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_additive=False,
    )
    operations_drop_ignore_additive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_additive=True,
    )
    assert len(operations_drop_with_additive) == 2  # DROP name, DROP age
    assert len(operations_drop_ignore_additive) == 2  # Same operations
    assert operations_drop_with_additive == operations_drop_ignore_additive


def test_ignore_both_destructive_and_additive():
    """Test behavior when both ignore_destructive and ignore_additive are True."""
    schema_differ = SchemaDiffer(
        support_positional_add=True,
        compatible_types={
            exp.DataType.build("INT"): {exp.DataType.build("STRING")},
        },
    )

    current_struct = "STRUCT<id INT, name STRING, age INT>"
    new_struct = "STRUCT<id STRING, age INT, address STRING>"  # DROP name, ADD address, ALTER id

    operations_ignore_both = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_destructive=True,
        ignore_additive=True,
    )
    assert len(operations_ignore_both) == 0


def test_ignore_additive_array_operations():
    """Test ignore_additive with array of struct operations."""
    schema_differ = SchemaDiffer(
        nested_support=NestedSupport.ALL,
        support_positional_add=True,
    )

    current_struct = "STRUCT<id INT, items ARRAY<STRUCT<col_a INT, col_b STRING>>>"
    new_struct = "STRUCT<id INT, items ARRAY<STRUCT<col_a INT, col_b STRING, col_c DOUBLE>>>"

    # With additive operations allowed - should add to array struct
    operations_with_additive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_additive=False,
    )
    assert len(operations_with_additive) == 1  # ADD to array struct
    assert isinstance(operations_with_additive[0], TableAlterAddColumnOperation)

    # With additive operations ignored - should do nothing
    operations_ignore_additive = schema_differ._from_structs(
        exp.DataType.build(current_struct),
        exp.DataType.build(new_struct),
        "apply_to_table",
        ignore_additive=True,
    )
    assert len(operations_ignore_additive) == 0


def test_drop_operation_missing_column_error():
    schema_differ = SchemaDiffer(
        nested_support=NestedSupport.NONE,
        support_positional_add=False,
    )

    # a struct that doesn't contain the column we're going to drop
    current_struct = exp.DataType.build("STRUCT<id INT, name STRING>")

    with pytest.raises(SQLMeshError) as error_message:
        schema_differ._drop_operation(
            columns=[TableAlterColumn.primitive("missing_column")],
            struct=current_struct,
            pos=0,
            root_struct=current_struct,
            table_name="test_table",
        )

    assert (
        str(error_message.value)
        == "Cannot drop column 'missing_column' from table 'test_table' - column not found. This may indicate a mismatch between the expected and actual table schemas."
    )


def test_prepare_columns_for_static_diff_nested_partial_unknown_types():
    differ = get_schema_differ("databricks")

    # 1. Partially-unknown STRUCT: dropping nested field 'b' (or unknown field 'u') is detected
    old_struct = exp.DataType.build("STRUCT<a INT, b STRING, u NULL>", dialect="databricks")
    new_struct_drop_b = exp.DataType.build("STRUCT<a INT, u NULL>", dialect="databricks")
    comp = prepare_columns_for_static_diff({"info": old_struct}, {"info": new_struct_drop_b})
    assert comp is not None
    ops_drop_b = differ.compare_columns("tbl", comp[0], comp[1])
    assert has_drop_alteration(ops_drop_b)
    assert [op.expression.sql("databricks") for op in ops_drop_b] == [
        "ALTER TABLE tbl DROP COLUMN info.b"
    ]

    new_struct_drop_u = exp.DataType.build("STRUCT<a INT, b STRING>", dialect="databricks")
    comp_drop_u = prepare_columns_for_static_diff({"info": old_struct}, {"info": new_struct_drop_u})
    assert comp_drop_u is not None
    ops_drop_u = differ.compare_columns("tbl", comp_drop_u[0], comp_drop_u[1])
    assert has_drop_alteration(ops_drop_u)
    assert [op.expression.sql("databricks") for op in ops_drop_u] == [
        "ALTER TABLE tbl DROP COLUMN info.u"
    ]

    # 2. Partially-unknown STRUCT: adding nested field 'c' in the middle preserves order & quoting
    old_quoted = exp.DataType.build("STRUCT<`MyA` INT, u NULL>", dialect="databricks")
    new_quoted_add = exp.DataType.build(
        "STRUCT<`MyA` INT, `MyB` DOUBLE, u NULL>", dialect="databricks"
    )
    comp_add = prepare_columns_for_static_diff({"info": old_quoted}, {"info": new_quoted_add})
    assert comp_add is not None
    ops_add = differ.compare_columns("tbl", comp_add[0], comp_add[1])
    assert has_additive_alteration(ops_add)
    assert not has_drop_alteration(ops_add)
    assert [op.expression.sql("databricks") for op in ops_add] == [
        "ALTER TABLE tbl ADD COLUMN info.`MyB` DOUBLE AFTER `MyA`"
    ]

    # 3. Partially-unknown STRUCT: incompatible type change on known sibling field 'a' is detected
    old_retype = exp.DataType.build("STRUCT<a STRING, u NULL>", dialect="databricks")
    new_retype = exp.DataType.build("STRUCT<a INT, u NULL>", dialect="databricks")
    comp_retype = prepare_columns_for_static_diff({"info": old_retype}, {"info": new_retype})
    assert comp_retype is not None
    ops_retype = differ.compare_columns("tbl", comp_retype[0], comp_retype[1])
    assert has_drop_alteration(ops_retype)

    # 4. Partially-unknown STRUCT: resolving unknown field (NULL -> STRING) does NOT false-positive
    new_resolved = exp.DataType.build("STRUCT<a STRING, u STRING>", dialect="databricks")
    comp_resolved = prepare_columns_for_static_diff({"info": old_retype}, {"info": new_resolved})
    assert comp_resolved is not None
    assert differ.compare_columns("tbl", comp_resolved[0], comp_resolved[1]) == []

    # 5. Partially-unknown ARRAY<STRUCT> & ARRAY<UNKNOWN>
    old_arr = exp.DataType.build("ARRAY<STRUCT<a INT, b STRING, u UNKNOWN>>", dialect="databricks")
    new_arr_drop = exp.DataType.build("ARRAY<STRUCT<a INT, u UNKNOWN>>", dialect="databricks")
    comp_arr_drop = prepare_columns_for_static_diff({"items": old_arr}, {"items": new_arr_drop})
    assert comp_arr_drop is not None
    ops_arr_drop = differ.compare_columns("tbl", comp_arr_drop[0], comp_arr_drop[1])
    assert has_drop_alteration(ops_arr_drop)
    assert [op.expression.sql("databricks") for op in ops_arr_drop] == [
        "ALTER TABLE tbl DROP COLUMN items.element.b"
    ]

    comp_arr_unk_elem = prepare_columns_for_static_diff(
        {"tags": exp.DataType.build("ARRAY<UNKNOWN>")},
        {"tags": exp.DataType.build("ARRAY<INT>")},
    )
    assert comp_arr_unk_elem is not None
    assert differ.compare_columns("tbl", comp_arr_unk_elem[0], comp_arr_unk_elem[1]) == []

    # 6. Container-vs-primitive mismatch with partially-unknown STRUCT / ARRAY<UNKNOWN>
    comp_prim_to_struct = prepare_columns_for_static_diff(
        {"val": exp.DataType.build("STRING", dialect="databricks")},
        {"val": old_struct},
    )
    assert comp_prim_to_struct is not None
    ops_prim_to_struct = differ.compare_columns(
        "tbl", comp_prim_to_struct[0], comp_prim_to_struct[1]
    )
    assert has_drop_alteration(ops_prim_to_struct)

    comp_prim_to_arr = prepare_columns_for_static_diff(
        {"val": exp.DataType.build("STRING", dialect="databricks")},
        {"val": exp.DataType.build("ARRAY<UNKNOWN>")},
    )
    assert comp_prim_to_arr is not None
    assert has_drop_alteration(
        differ.compare_columns("tbl", comp_prim_to_arr[0], comp_prim_to_arr[1])
    )


def test_prepare_columns_for_static_diff_preserves_positional_column_order():
    old_cols = {
        "a": exp.DataType.build("INT", dialect="databricks"),
        "c": exp.DataType.build("STRING", dialect="databricks"),
    }
    new_cols = {
        "b_first": exp.DataType.build("DOUBLE", dialect="databricks"),
        "a": exp.DataType.build("INT", dialect="databricks"),
        "b_mid": exp.DataType.build("BIGINT", dialect="databricks"),
        "c": exp.DataType.build("STRING", dialect="databricks"),
    }
    comp = prepare_columns_for_static_diff(old_cols, new_cols)
    assert comp is not None
    comparable_old, comparable_new = comp
    assert list(comparable_old.keys()) == ["a", "c"]
    assert list(comparable_new.keys()) == ["b_first", "a", "b_mid", "c"]

    ops = get_schema_differ("databricks").compare_columns("tbl", comparable_old, comparable_new)
    assert [op.expression.sql("databricks") for op in ops] == [
        "ALTER TABLE tbl ADD COLUMN b_first DOUBLE FIRST",
        "ALTER TABLE tbl ADD COLUMN b_mid BIGINT AFTER a",
    ]


def test_treat_alter_data_type_as_destructive_respects_ignore_flags():
    differ = get_schema_differ("postgres", overrides={"treat_alter_data_type_as_destructive": True})
    old_cols = {"val": exp.DataType.build("VARCHAR(10)", dialect="postgres")}
    new_cols = {"val": exp.DataType.build("VARCHAR(50)", dialect="postgres")}

    # When treat_alter_data_type_as_destructive=True, compatible type widening is destructive:
    # ignore_destructive=True must suppress it, while ignore_additive=True must NOT suppress it.
    assert (
        differ.compare_columns(
            "tbl", old_cols, new_cols, ignore_destructive=True, ignore_additive=False
        )
        == []
    )
    ops_keep_destructive = differ.compare_columns(
        "tbl", old_cols, new_cols, ignore_destructive=False, ignore_additive=True
    )
    assert len(ops_keep_destructive) == 1
    assert isinstance(ops_keep_destructive[0], TableAlterChangeColumnTypeOperation)
    assert ops_keep_destructive[0].is_destructive
    assert not ops_keep_destructive[0].is_additive


def test_prepare_columns_for_static_diff_does_not_alias_input_types():
    differ = get_schema_differ("databricks")
    old_cols = {
        "details": exp.DataType.build("STRUCT<a INT, b STRING>", dialect="databricks"),
        "u": exp.DataType.build("NULL"),
    }
    new_cols = {
        "details": exp.DataType.build("STRUCT<a INT>", dialect="databricks"),
        "u": exp.DataType.build("STRING"),
    }
    old_sql = {k: v.sql("databricks") for k, v in old_cols.items()}
    new_sql = {k: v.sql("databricks") for k, v in new_cols.items()}

    for _ in range(2):
        comp = prepare_columns_for_static_diff(old_cols, new_cols)
        assert comp is not None
        ops = differ.compare_columns("tbl", comp[0], comp[1])
        assert [op.expression.sql("databricks") for op in ops] == [
            "ALTER TABLE tbl DROP COLUMN details.b"
        ]
        # Neither the diff nor the normalization may mutate the caller's type trees.
        assert {k: v.sql("databricks") for k, v in old_cols.items()} == old_sql
        assert {k: v.sql("databricks") for k, v in new_cols.items()} == new_sql


def test_prepare_columns_for_static_diff_positional_struct_fields():
    # Positional (unnamed) struct fields, e.g. produced by annotating STRUCT(1, NULL).
    old_struct = exp.DataType(
        this=exp.DataType.Type.STRUCT,
        expressions=[exp.DataType.build("INT"), exp.DataType.build("NULL")],
        nested=True,
    )
    new_struct = exp.DataType(
        this=exp.DataType.Type.STRUCT,
        expressions=[exp.DataType.build("INT"), exp.DataType.build("VARCHAR")],
        nested=True,
    )
    # Positional shapes can't be compared by name, so the comparison is deferred (not a KeyError).
    assert prepare_columns_for_static_diff({"s": old_struct}, {"s": new_struct}) is None
