from __future__ import annotations

import typing as t
import logging

from sqlglot import exp
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.snapshot import SnapshotIdLike, SnapshotNameVersionLike


logger = logging.getLogger(__name__)

try:
    # We can't import directly from the root package due to circular dependency
    from sqlmesh._version import __version__ as SQLMESH_VERSION  # noqa
except ImportError:
    logger.error(
        'Unable to set __version__, run "pip install -e ." or "python setup.py develop" first.'
    )


T = t.TypeVar("T")


def snapshot_name_filter(
    snapshot_names: t.Iterable[str],
    batch_size: int,
    alias: t.Optional[str] = None,
) -> t.Iterator[exp.Condition]:
    names = sorted(snapshot_names)

    if not names:
        yield exp.false()
    else:
        batches = create_batches(names, batch_size=batch_size)
        for names in batches:
            yield exp.column("name", table=alias).isin(*names)


def snapshot_id_filter(
    engine_adapter: EngineAdapter,
    snapshot_ids: t.Iterable[SnapshotIdLike],
    batch_size: int,
    alias: t.Optional[str] = None,
) -> t.Iterator[exp.Condition]:
    name_identifiers = sorted(
        {(snapshot_id.name, snapshot_id.identifier) for snapshot_id in snapshot_ids}
    )
    batches = create_batches(name_identifiers, batch_size=batch_size)

    if not name_identifiers:
        yield exp.false()
    elif engine_adapter.SUPPORTS_TUPLE_IN:
        for identifiers in batches:
            yield t.cast(
                exp.Tuple,
                exp.convert(
                    (
                        exp.column("name", table=alias),
                        exp.column("identifier", table=alias),
                    )
                ),
            ).isin(*identifiers)
    else:
        for identifiers in batches:
            yield exp.or_(
                *[
                    exp.and_(
                        exp.column("name", table=alias).eq(name),
                        exp.column("identifier", table=alias).eq(identifier),
                    )
                    for name, identifier in identifiers
                ]
            )


def snapshot_name_version_filter(
    engine_adapter: EngineAdapter,
    snapshot_name_versions: t.Iterable[SnapshotNameVersionLike],
    batch_size: int,
    version_column_name: str = "version",
    alias: t.Optional[str] = "snapshots",
    column_prefix: t.Optional[str] = None,
) -> t.Iterator[exp.Condition]:
    name_versions = sorted({(s.name, s.version) for s in snapshot_name_versions})
    batches = create_batches(name_versions, batch_size=batch_size)

    name_column_name = "name"
    if column_prefix:
        name_column_name = f"{column_prefix}_{name_column_name}"
        version_column_name = f"{column_prefix}_{version_column_name}"

    name_column = exp.column(name_column_name, table=alias)
    version_column = exp.column(version_column_name, table=alias)

    if not name_versions:
        yield exp.false()
    elif engine_adapter.SUPPORTS_TUPLE_IN:
        for versions in batches:
            yield t.cast(
                exp.Tuple,
                exp.convert(
                    (
                        name_column,
                        version_column,
                    )
                ),
            ).isin(*versions)
    else:
        for versions in batches:
            yield exp.or_(
                *[
                    exp.and_(
                        name_column.eq(name),
                        version_column.eq(version),
                    )
                    for name, version in versions
                ]
            )


def create_batches(l: t.List[T], batch_size: int) -> t.List[t.List[T]]:
    return [l[i : i + batch_size] for i in range(0, len(l), batch_size)]


def fetchone(engine_adapter: EngineAdapter, query: t.Union[exp.Expr, str]) -> t.Optional[t.Tuple]:
    return engine_adapter.fetchone(query, ignore_unsupported_errors=True, quote_identifiers=True)


def fetchall(engine_adapter: EngineAdapter, query: t.Union[exp.Expr, str]) -> t.List[t.Tuple]:
    return engine_adapter.fetchall(query, ignore_unsupported_errors=True, quote_identifiers=True)
