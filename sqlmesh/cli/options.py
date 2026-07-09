from __future__ import annotations

import os
import typing as t

import click

paths = click.option(
    "-p",
    "--paths",
    multiple=True,
    default=[os.getcwd()],
    help="Path(s) to the SQLMesh config/project.",
)

config = click.option(
    "--config",
    help="Name of the config object. Only applicable to configuration defined using Python script.",
)

start_time = click.option(
    "-s",
    "--start",
    required=False,
    help="The start datetime of the interval for which this command will be applied.",
)

end_time = click.option(
    "-e",
    "--end",
    required=False,
    help="The end datetime of the interval for which this command will be applied.",
)

execution_time = click.option(
    "--execution-time",
    help="The execution time (defaults to now).",
)

time_zone = click.option(
    "--time-zone",
    help="IANA timezone for interpreting relative --start, --end, and --execution-time values (e.g. America/Los_Angeles). Defaults to UTC.",
)

expand = click.option(
    "--expand",
    multiple=True,
    help="Whether or not to expand materialized models (defaults to False). If True, all referenced models are expanded as raw queries. Multiple model names can also be specified, in which case only they will be expanded as raw queries.",
)

match_pattern = click.option(
    "-k",
    multiple=True,
    help="Only run tests that match the pattern of substring.",
)

verbose = click.option(
    "-v",
    "--verbose",
    count=True,
    help="Verbose output. Use -vv for very verbose output.",
)


def format_options(func: t.Callable) -> t.Callable:
    """Decorator to add common format options to CLI commands."""
    func = click.option(
        "--normalize",
        is_flag=True,
        help="Whether or not to normalize identifiers to lowercase.",
        default=None,
    )(func)
    func = click.option(
        "--pad",
        type=int,
        help="Determines the pad size in a formatted string.",
    )(func)
    func = click.option(
        "--indent",
        type=int,
        help="Determines the indentation size in a formatted string.",
    )(func)
    func = click.option(
        "--normalize-functions",
        type=str,
        help="Whether or not to normalize all function names. Possible values are: 'upper', 'lower'",
    )(func)
    func = click.option(
        "--leading-comma",
        is_flag=True,
        default=None,
        help="Determines whether or not the comma is leading or trailing in select expressions. Default is trailing.",
    )(func)
    func = click.option(
        "--max-text-width",
        type=int,
        help="The max number of characters in a segment before creating new lines in pretty mode.",
    )(func)
    return func
