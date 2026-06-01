from __future__ import annotations

import typing as t
import json
import logging
from sqlglot import exp

from sqlmesh.core import constants as c
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.state_sync.db.utils import (
    fetchall,
    fetchone,
)
from sqlmesh.core.environment import Environment, EnvironmentStatements, EnvironmentSummary
from sqlmesh.utils.migration import index_text_type, blob_text_type
from sqlmesh.utils.date import now_timestamp, time_like_to_str
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    import pandas as pd


logger = logging.getLogger(__name__)


class EnvironmentState:
    def __init__(
        self,
        engine_adapter: EngineAdapter,
        schema: t.Optional[str] = None,
    ):
        self.engine_adapter = engine_adapter
        self.environments_table = exp.table_("_environments", db=schema)
        self.environment_statements_table = exp.table_("_environment_statements", db=schema)

        index_type = index_text_type(engine_adapter.dialect)
        blob_type = blob_text_type(engine_adapter.dialect)

        self._environment_columns_to_types = {
            "name": exp.DataType.build(index_type),
            "snapshots": exp.DataType.build(blob_type),
            "start_at": exp.DataType.build("text"),
            "end_at": exp.DataType.build("text"),
            "plan_id": exp.DataType.build("text"),
            "previous_plan_id": exp.DataType.build("text"),
            "expiration_ts": exp.DataType.build("bigint"),
            "finalized_ts": exp.DataType.build("bigint"),
            "promoted_snapshot_ids": exp.DataType.build(blob_type),
            "suffix_target": exp.DataType.build("text"),
            "catalog_name_override": exp.DataType.build("text"),
            "previous_finalized_snapshots": exp.DataType.build(blob_type),
            "normalize_name": exp.DataType.build("boolean"),
            "gateway_managed": exp.DataType.build("boolean"),
            "requirements": exp.DataType.build(blob_type),
        }

        self._environment_statements_columns_to_types = {
            "environment_name": exp.DataType.build(index_type),
            "plan_id": exp.DataType.build("text"),
            "environment_statements": exp.DataType.build(blob_type),
        }

    def update_environment(self, environment: Environment) -> None:
        """Updates the environment.

        Args:
            environment: The environment
        """
        self.engine_adapter.delete_from(
            self.environments_table,
            where=exp.EQ(
                this=exp.column("name"),
                expression=exp.Literal.string(environment.name),
            ),
        )

        self.engine_adapter.insert_append(
            self.environments_table,
            _environment_to_df(environment),
            target_columns_to_types=self._environment_columns_to_types,
            track_rows_processed=False,
        )

    def update_environment_statements(
        self,
        environment_name: str,
        plan_id: str,
        environment_statements: t.List[EnvironmentStatements],
    ) -> None:
        """Updates the environment's statements.

        Args:
            environment_name: The environment name
            plan_id: The environment's plan ID
            environment_statements: The environment statements

        """

        self.engine_adapter.delete_from(
            self.environment_statements_table,
            where=exp.EQ(
                this=exp.column("environment_name"),
                expression=exp.Literal.string(environment_name),
            ),
        )

        if environment_statements:
            self.engine_adapter.insert_append(
                self.environment_statements_table,
                _environment_statements_to_df(environment_name, plan_id, environment_statements),
                target_columns_to_types=self._environment_statements_columns_to_types,
                track_rows_processed=False,
            )

    def invalidate_environment(self, name: str, protect_prod: bool = True) -> None:
        """Invalidates the environment.

        Args:
            name: The name of the environment
            protect_prod: If True, prevents invalidation of the production environment.
        """
        name = name.lower()
        if protect_prod and name == c.PROD:
            raise SQLMeshError("Cannot invalidate the production environment.")

        filter_expr = exp.column("name").eq(name)

        self.engine_adapter.update_table(
            self.environments_table,
            {"expiration_ts": now_timestamp()},
            where=filter_expr,
        )

    def finalize(self, environment: Environment) -> None:
        """Finalize the target environment, indicating that this environment has been
        fully promoted and is ready for use.

        Args:
            environment: The target environment to finalize.
        """
        logger.info("Finalizing environment '%s'", environment.name)

        environment_filter = exp.column("name").eq(exp.Literal.string(environment.name))

        stored_plan_id_query = (
            exp.select("plan_id")
            .from_(self.environments_table)
            .where(environment_filter, copy=False)
            .lock(copy=False)
        )
        stored_plan_id_row = fetchone(self.engine_adapter, stored_plan_id_query)

        if not stored_plan_id_row:
            raise SQLMeshError(f"Missing environment '{environment.name}' can't be finalized")

        stored_plan_id = stored_plan_id_row[0]
        if stored_plan_id != environment.plan_id:
            raise SQLMeshError(
                f"Another plan ({stored_plan_id}) was applied to the target environment '{environment.name}' while your current plan "
                f"({environment.plan_id}) was still in progress, interrupting it. Please re-apply your plan to resolve this error."
            )

        environment.finalized_ts = now_timestamp()
        self.engine_adapter.update_table(
            self.environments_table,
            {"finalized_ts": environment.finalized_ts},
            where=environment_filter,
        )

    def get_expired_environments(
        self, current_ts: int, name: t.Optional[str] = None
    ) -> t.List[EnvironmentSummary]:
        """Returns the expired environments.

        Expired environments are environments that have exceeded their time-to-live value.

        Args:
            current_ts: The current timestamp in milliseconds used to determine expiration.
            name: If provided, only the environment with this name is considered.

        Returns:
            The list of environment summaries to remove.
        """
        return self._fetch_environment_summaries(
            where=self._create_expiration_filter_expr(current_ts, name=name)
        )

    def delete_expired_environments(
        self, current_ts: t.Optional[int] = None, name: t.Optional[str] = None
    ) -> t.List[EnvironmentSummary]:
        """Deletes expired environments.

        Args:
            current_ts: The current timestamp in milliseconds. Defaults to now.
            name: If provided, only the environment with this name is deleted.

        Returns:
            A list of deleted environments.
        """
        current_ts = current_ts or now_timestamp()
        expired_environments = self.get_expired_environments(current_ts=current_ts, name=name)

        self.engine_adapter.delete_from(
            self.environments_table,
            where=self._create_expiration_filter_expr(current_ts, name=name),
        )

        # Delete the expired environments' corresponding environment statements
        if expired_environments_exprs := [
            exp.EQ(this=exp.column("environment_name"), expression=exp.Literal.string(env.name))
            for env in expired_environments
        ]:
            self.engine_adapter.delete_from(
                self.environment_statements_table,
                where=exp.or_(*expired_environments_exprs),
            )

        return expired_environments

    def get_environments(self) -> t.List[Environment]:
        """Fetches all environments.

        Returns:
            A list of all environments.
        """
        return [
            self._environment_from_row(row)
            for row in fetchall(self.engine_adapter, self._environments_query())
        ]

    def get_environments_summary(self) -> t.List[EnvironmentSummary]:
        """Fetches summaries for all environments.

        Returns:
            A list of all environment summaries.
        """
        return self._fetch_environment_summaries()

    def get_environment(
        self, environment: str, lock_for_update: bool = False
    ) -> t.Optional[Environment]:
        """Fetches the environment if it exists.

        Args:
            environment: The environment
            lock_for_update: Lock the snapshot rows for future update

        Returns:
            The environment object.
        """
        row = fetchone(
            self.engine_adapter,
            self._environments_query(
                where=exp.EQ(
                    this=exp.column("name"),
                    expression=exp.Literal.string(environment),
                ),
                lock_for_update=lock_for_update,
            ),
        )

        if not row:
            return None

        env = self._environment_from_row(row)
        return env

    def get_environment_statements(self, environment: str) -> t.List[EnvironmentStatements]:
        """Fetches the environment's statements from the environment_statements table.
        Args:
            environment: The environment name

        Returns:
            A list of the environment statements.

        """
        query = (
            exp.select(
                exp.to_identifier("environment_statements"),
            )
            .from_(self.environment_statements_table)
            .where(
                exp.EQ(
                    this=exp.column("environment_name"),
                    expression=exp.Literal.string(environment),
                )
            )
        )
        result = fetchone(engine_adapter=self.engine_adapter, query=query)
        if result and (statements := json.loads(result[0])):
            return [
                EnvironmentStatements.parse_obj(environment_statements)
                for environment_statements in statements
            ]

        return []

    def _environment_from_row(self, row: t.Tuple[str, ...]) -> Environment:
        return Environment(
            **{field: row[i] for i, field in enumerate(sorted(Environment.all_fields()))}
        )

    def _environment_summmary_from_row(self, row: t.Tuple[str, ...]) -> EnvironmentSummary:
        return EnvironmentSummary(
            **{field: row[i] for i, field in enumerate(sorted(EnvironmentSummary.all_fields()))}
        )

    def _environments_query(
        self,
        where: t.Optional[str | exp.Expr] = None,
        lock_for_update: bool = False,
        required_fields: t.Optional[t.List[str]] = None,
    ) -> exp.Select:
        query_fields = required_fields if required_fields else sorted(Environment.all_fields())
        query = (
            exp.select(*(exp.to_identifier(field) for field in query_fields))
            .from_(self.environments_table)
            .where(where)
        )
        if lock_for_update:
            return query.lock(copy=False)
        return query

    def _create_expiration_filter_expr(
        self, current_ts: int, name: t.Optional[str] = None
    ) -> exp.Expr:
        """Creates a SQLGlot filter expression to find expired environments.

        Args:
            current_ts: The current timestamp.
            name: If provided, adds an equality filter on the environment name.
        """
        where: exp.Expr = exp.LTE(
            this=exp.column("expiration_ts"),
            expression=exp.Literal.number(current_ts),
        )
        if name is not None:
            where = exp.and_(t.cast(exp.Condition, where), exp.column("name").eq(name))
        return where

    def _fetch_environment_summaries(
        self, where: t.Optional[str | exp.Expr] = None
    ) -> t.List[EnvironmentSummary]:
        return [
            self._environment_summmary_from_row(row)
            for row in fetchall(
                self.engine_adapter,
                self._environments_query(
                    where=where,
                    required_fields=sorted(EnvironmentSummary.all_fields()),
                ),
            )
        ]


def _environment_to_df(environment: Environment) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "name": environment.name,
                "snapshots": json.dumps(environment.snapshot_dicts()),
                "start_at": time_like_to_str(environment.start_at),
                "end_at": time_like_to_str(environment.end_at) if environment.end_at else None,
                "plan_id": environment.plan_id,
                "previous_plan_id": environment.previous_plan_id,
                "expiration_ts": environment.expiration_ts,
                "finalized_ts": environment.finalized_ts,
                "promoted_snapshot_ids": (
                    json.dumps(environment.promoted_snapshot_id_dicts())
                    if environment.promoted_snapshot_ids is not None
                    else None
                ),
                "suffix_target": environment.suffix_target.value,
                "catalog_name_override": environment.catalog_name_override,
                "previous_finalized_snapshots": (
                    json.dumps(environment.previous_finalized_snapshot_dicts())
                    if environment.previous_finalized_snapshots is not None
                    else None
                ),
                "normalize_name": environment.normalize_name,
                "gateway_managed": environment.gateway_managed,
                "requirements": json.dumps(environment.requirements),
            }
        ]
    )


def _environment_statements_to_df(
    environment_name: str, plan_id: str, environment_statements: t.List[EnvironmentStatements]
) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "environment_name": environment_name,
                "plan_id": plan_id,
                "environment_statements": json.dumps([e.dict() for e in environment_statements]),
            }
        ]
    )
