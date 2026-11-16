from sqlglot import exp
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.dialect import parse


def test_convert_to_time_column_case_insensitive_columns_to_types():
    expressions = parse(
        """
        MODEL (
            name my_schema.my_model,
            dialect 'tsql,normalization_strategy=case_insensitive',
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column CreatedAtUtc
            )
        );
        SELECT 1 AS id, CAST('2024-01-01' AS TIMESTAMP) AS createdatutc
        """
    )
    model = load_sql_based_model(expressions)
    result = model.convert_to_time_column(
        "2024-01-02",
        columns_to_types={"ID": exp.DataType.build("INT"), "CREATEDATUTC": exp.DataType.build("TIMESTAMP")},
    )
    assert isinstance(result, exp.Expr)
