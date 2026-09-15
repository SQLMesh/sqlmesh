from __future__ import annotations

import typing as t

from pydantic import Field

from sqlmesh.core.config.base import UpdateStrategy
from sqlmesh.dbt.column import ColumnConfig
from sqlmesh.dbt.common import GeneralConfig
from sqlmesh.dbt.relation import RelationType
from sqlmesh.dbt.util import DBT_VERSION
from sqlmesh.utils import AttributeDict
from sqlmesh.utils.errors import ConfigError

if t.TYPE_CHECKING:
    from sqlmesh.dbt.context import DbtContext


class SourceConfig(GeneralConfig):
    """
    Args:
        name: The name of the table
        source_name: The name of the source that defines the table
        database: Name of the database where the table is stored. By default, the project's target database is used.
        schema: The scehma name as stored in the database. If not specified, the source name is used.
        identifier: The table name as stored in the database. If not specified, the source table name is used
        loader: Describes the tool that loads the source into the warehouse
        overrides: Override a source defined in the specified package
        freshness: Dictionary specifying maximum time, since the most recent record, to consider the source fresh
        loaded_at_field: Column name or expression that returns a timestamp indicating freshness
        quoting: Define which components of the qualified name (database, schema, identifier) to quote when resolving the source() method
        external: Dictionary of metadata properties specific to sources that point to external tables
        columns: Columns within the source
    """

    # DBT configuration fields
    name: str = ""
    source_name_: str = Field("", alias="source_name")
    fqn_: t.List[str] = Field(default_factory=list, alias="fqn")
    database: t.Optional[str] = None
    schema_: t.Optional[str] = Field(None, alias="schema")
    identifier: t.Optional[str] = None
    loader: t.Optional[str] = None
    overrides: t.Optional[str] = None
    freshness: t.Optional[t.Dict[str, t.Any]] = {}
    loaded_at_field: t.Optional[str] = None
    quoting: t.Dict[str, t.Optional[bool]] = {}
    external: t.Optional[t.Dict[str, t.Any]] = {}
    source_meta: t.Optional[t.Dict[str, t.Any]] = {}
    columns: t.Dict[str, ColumnConfig] = {}
    event_time: t.Optional[str] = None

    _canonical_name: t.Optional[str] = None

    _FIELD_UPDATE_STRATEGY: t.ClassVar[t.Dict[str, UpdateStrategy]] = {
        **GeneralConfig._FIELD_UPDATE_STRATEGY,
        **{"columns": UpdateStrategy.KEY_EXTEND},
    }

    @property
    def table_name(self) -> t.Optional[str]:
        return self.identifier or self.name

    @property
    def config_name(self) -> str:
        return f"{self.source_name_}.{self.name}"

    @property
    def fqn(self) -> str:
        return ".".join(self.fqn_)

    def canonical_name(self, context: DbtContext) -> str:
        if self._canonical_name is None:
            source = context.get_callable_macro("source")
            if not source:
                raise ConfigError("'source' macro not found.")

            try:
                relation = source(self.source_name_, self.name)
            except Exception as e:
                raise ConfigError(
                    f"'source' macro failed for '{self.config_name}' with exception '{e}'."
                )

            identifier = relation.identifier or ""
            needs_identifier_quoting = "." in identifier or " " in identifier
            relation = relation.quote(
                database=False,
                schema=False,
                identifier=needs_identifier_quoting,
            )
            if relation.database == context.target.database:
                relation = relation.include(database=False)
            self._canonical_name = relation.render()
        return self._canonical_name

    @property
    def relation_info(self) -> AttributeDict:
        extras = {}
        external_location = (
            self.source_meta.get("external_location", None) if self.source_meta else None
        )
        if external_location:
            extras["external"] = external_location.replace("{name}", self.table_name)

        if DBT_VERSION >= (1, 9, 0) and self.event_time:
            extras["event_time_filter"] = {
                "field_name": self.event_time,
            }

        return AttributeDict(
            {
                "database": self.database,
                "schema": self.schema_,
                "identifier": self.table_name,
                "type": RelationType.External.value,
                "quote_policy": AttributeDict(self.quoting),
                **extras,
            }
        )
