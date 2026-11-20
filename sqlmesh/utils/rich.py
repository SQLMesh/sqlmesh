from __future__ import annotations

import typing as t

import re

from rich.console import Console
from rich.progress import Column, ProgressColumn, Task, Text
from rich.theme import Theme
from rich.table import Table
from rich.align import Align

if t.TYPE_CHECKING:
    import pandas as pd

theme = Theme(
    {
        "added": "green",
        "removed": "red",
        "direct": "magenta",  # directly modified
        "indirect": "yellow",  # indirectly modified
        "metadata": "cyan",  # metadata updated
    }
)

console = Console(theme=theme)


class BatchColumn(ProgressColumn):
    """Renders completed count/total, "pending".

    Space pads the completed count so that progress length does not change as task progresses
    past powers of 10.

    Source: https://rich.readthedocs.io/en/stable/reference/progress.html#rich.progress.MofNCompleteColumn

    Args:
        separator: Text to separate completed and total values. Defaults to "/".
    """

    def __init__(self, separator: str = "/", table_column: t.Optional[Column] = None):
        self.separator = separator
        super().__init__(table_column=table_column)

    def render(self, task: Task) -> Text:
        """Show completed count/total, "pending"."""
        total = int(task.total) if task.total is not None else "?"
        completed = int(task.completed)

        if completed == 0 and task.total is not None and task.total > 0:
            return Text("pending", style="progress.download")

        total_width = len(str(total))
        return Text(
            f"{completed:{total_width}d}{self.separator}{total}",
            style="progress.download",
        )


def strip_ansi_codes(text: str) -> str:
    """Strip ANSI color codes and styling from text."""
    ansi_escape = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
    return ansi_escape.sub("", text).strip()


def df_to_table(
    header: str,
    df: pd.DataFrame,
    show_index: bool = True,
    index_name: str = "Row",
) -> Table:
    """Convert a pandas.DataFrame obj into a rich.Table obj.
    Args:
        df (DataFrame): A Pandas DataFrame to be converted to a rich Table.
        rich_table (Table): A rich Table that should be populated by the DataFrame values.
        show_index (bool): Add a column with a row count to the table. Defaults to True.
        index_name (str, optional): The column name to give to the index column. Defaults to None, showing no value.
    Returns:
        Table: The rich Table instance passed, populated with the DataFrame values."""

    rich_table = Table(title=f"[bold red]{header}[/bold red]", show_lines=True, min_width=60)
    if show_index:
        index_name = str(index_name) if index_name else ""
        rich_table.add_column(Align.center(index_name))

    for column in df.columns:
        column_name = column if isinstance(column, str) else ": ".join(str(col) for col in column)

        # Color coding unit test columns (expected/actual), can be removed or refactored if df_to_table is used elswhere too
        lower = column_name.lower()
        if "expected" in lower:
            column_name = f"[green]{column_name}[/green]"
        elif "actual" in lower:
            column_name = f"[red]{column_name}[/red]"

        rich_table.add_column(Align.center(column_name))

    for index, value_list in zip(df.index, df.values.tolist()):
        row = [str(index)] if show_index else []
        row += [str(x) for x in value_list]
        center = [Align.center(x) for x in row]
        rich_table.add_row(*center)

    return rich_table
