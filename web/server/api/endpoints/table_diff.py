from __future__ import annotations

import typing as t

from fastapi import APIRouter, Depends
from sqlglot import exp

from sqlmesh.core.context import Context
from web.server.models import ProcessedSampleData, RowDiff, SchemaDiff, TableDiff
from web.server.settings import get_loaded_context

router = APIRouter()


def _cells_match(x: t.Any, y: t.Any) -> bool:
    # lazily import pandas and numpy as we do in core
    import pandas as pd
    import numpy as np

    def _normalize(val: t.Any) -> t.Any:
        if pd.isnull(val):
            val = None
        return list(val) if isinstance(val, (pd.Series, np.ndarray)) else val

    return _normalize(x) == _normalize(y)


def _process_sample_data(
    row_diff: t.Any, source_name: str, target_name: str
) -> ProcessedSampleData:
    import pandas as pd

    if row_diff.joined_sample.shape[0] == 0:
        return ProcessedSampleData(
            column_differences=[],
            source_only=row_diff.s_sample.replace({pd.NA: None}).to_dict("records")
            if row_diff.s_sample.shape[0] > 0
            else [],
            target_only=row_diff.t_sample.replace({pd.NA: None}).to_dict("records")
            if row_diff.t_sample.shape[0] > 0
            else [],
        )

    keys: list[str] = []
    columns: dict[str, list[str]] = {}

    # todo: to be refactored to the diff module itself since it is similar to console
    source_prefix, source_display = (
        (f"{source_name}__", source_name.upper())
        if source_name.lower() != row_diff.source.lower()
        else ("s__", "SOURCE")
    )
    target_prefix, target_display = (
        (f"{target_name}__", target_name.upper())
        if target_name.lower() != row_diff.target.lower()
        else ("t__", "TARGET")
    )

    for column in row_diff.joined_sample.columns:
        if column.lower().startswith(source_prefix.lower()):
            column_name = column[len(source_prefix) :]

            target_column = None
            for col in row_diff.joined_sample.columns:
                if col.lower() == (target_prefix + column_name).lower():
                    target_column = col
                    break

            if target_column:
                columns[column_name] = [column, target_column]
        elif not column.lower().startswith(target_prefix.lower()):
            keys.append(column)

    column_differences = []
    for column_name, (source_column, target_column) in columns.items():
        column_table = row_diff.joined_sample[keys + [source_column, target_column]]

        # Filter to retain non identical-valued rows
        column_table = column_table[
            column_table.apply(
                lambda row: not _cells_match(row[source_column], row[target_column]),
                axis=1,
            )
        ]

        # Rename the column headers for readability
        column_table = column_table.rename(
            columns={
                source_column: source_display,
                target_column: target_display,
            }
        )

        if len(column_table) > 0:
            for row in column_table.replace({pd.NA: None}).to_dict("records"):
                row["__column_name__"] = column_name
                row["__source_name__"] = source_display
                row["__target_name__"] = target_display
                column_differences.append(row)

    return ProcessedSampleData(
        column_differences=column_differences,
        source_only=row_diff.s_sample.replace({pd.NA: None}).to_dict("records")
        if row_diff.s_sample.shape[0] > 0
        else [],
        target_only=row_diff.t_sample.replace({pd.NA: None}).to_dict("records")
        if row_diff.t_sample.shape[0] > 0
        else [],
    )


@router.get("")
def get_table_diff(
    source: str,
    target: str,
    on: t.Optional[str] = None,
    model_or_snapshot: t.Optional[str] = None,
    where: t.Optional[str] = None,
    temp_schema: t.Optional[str] = None,
    limit: int = 20,
    context: Context = Depends(get_loaded_context),
) -> t.Optional[TableDiff]:
    """Calculate differences between tables, taking into account schema and row level differences."""
    import numpy as np

    table_diffs = context.table_diff(
        source=source,
        target=target,
        on=t.cast(exp.Condition, exp.condition(on)) if on else None,
        select_models={model_or_snapshot} if model_or_snapshot else None,
        where=where,
        limit=limit,
        show=False,
    )

    if not table_diffs:
        return None
    diff = table_diffs[0] if isinstance(table_diffs, list) else table_diffs

    _schema_diff = diff.schema_diff()
    _row_diff = diff.row_diff(temp_schema=temp_schema)
    schema_diff = SchemaDiff(
        source=_schema_diff.source,
        target=_schema_diff.target,
        source_schema=_schema_diff.source_schema,
        target_schema=_schema_diff.target_schema,
        added=_schema_diff.added,
        removed=_schema_diff.removed,
        modified=_schema_diff.modified,
    )

    # create a readable column-centric sample data structure
    processed_sample_data = _process_sample_data(_row_diff, source, target)

    row_diff = RowDiff(
        source=_row_diff.source,
        target=_row_diff.target,
        stats=_row_diff.stats,
        sample=_row_diff.sample.replace({np.nan: None}).to_dict(),
        joined_sample=_row_diff.joined_sample.replace({np.nan: None}).to_dict(),
        s_sample=_row_diff.s_sample.replace({np.nan: None}).to_dict(),
        t_sample=_row_diff.t_sample.replace({np.nan: None}).to_dict(),
        column_stats=_row_diff.column_stats.replace({np.nan: None}).to_dict(),
        source_count=_row_diff.source_count,
        target_count=_row_diff.target_count,
        count_pct_change=_row_diff.count_pct_change,
        decimals=getattr(_row_diff, "decimals", 3),
        processed_sample_data=processed_sample_data,
    )

    s_index, t_index, _ = diff.key_columns
    return TableDiff(
        schema_diff=schema_diff,
        row_diff=row_diff,
        on=[(s.name, t.name) for s, t in zip(s_index, t_index)],
    )
