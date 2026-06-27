from __future__ import annotations

import json
import logging
import time
import typing as t
from copy import deepcopy

from sqlglot import __version__ as SQLGLOT_VERSION
from sqlglot import exp

from sqlmesh.core import analytics
from sqlmesh.core import constants as c
from sqlmesh.core.console import Console, get_console
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.environment import Environment
from sqlmesh.core.snapshot import (
    Node,
    Snapshot,
    SnapshotFingerprint,
    SnapshotId,
    SnapshotTableInfo,
    fingerprint_from_node,
)
from sqlmesh.core.snapshot.definition import (
    _parents_from_node,
)
from sqlmesh.core.state_sync.base import (
    MIGRATIONS,
    MIN_SCHEMA_VERSION,
    MIN_SQLMESH_VERSION,
)
from sqlmesh.core.state_sync.db.environment import EnvironmentState
from sqlmesh.core.state_sync.db.interval import IntervalState
from sqlmesh.core.state_sync.db.snapshot import SnapshotState
from sqlmesh.core.state_sync.db.version import VersionState
from sqlmesh.core.state_sync.db.utils import (
    SQLMESH_VERSION,
    snapshot_id_filter,
    fetchall,
)
from sqlmesh.utils import major_minor
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import now_timestamp
from sqlmesh.utils.errors import SQLMeshError, StateMigrationError

logger = logging.getLogger(__name__)


if t.TYPE_CHECKING:
    from sqlmesh.core._typing import TableName


class StateMigrator:
    SNAPSHOT_BATCH_SIZE = 1000
    SNAPSHOT_MIGRATION_BATCH_SIZE = 500

    def __init__(
        self,
        engine_adapter: EngineAdapter,
        version_state: VersionState,
        snapshot_state: SnapshotState,
        environment_state: EnvironmentState,
        interval_state: IntervalState,
        console: t.Optional[Console] = None,
    ):
        self.engine_adapter = engine_adapter
        self.console = console or get_console()
        self.version_state = version_state
        self.snapshot_state = snapshot_state
        self.environment_state = environment_state
        self.interval_state = interval_state

        self._state_tables = [
            self.snapshot_state.snapshots_table,
            self.environment_state.environments_table,
            self.version_state.versions_table,
        ]
        self._optional_state_tables = [
            self.interval_state.intervals_table,
            self.snapshot_state.auto_restatements_table,
            self.environment_state.environment_statements_table,
        ]

    def migrate(
        self,
        schema: t.Optional[str],
        skip_backup: bool = False,
        promoted_snapshots_only: bool = True,
    ) -> None:
        """Migrate the state sync to the latest SQLMesh / SQLGlot version."""
        versions = self.version_state.get_versions()
        migration_start_ts = time.perf_counter()

        try:
            migrate_rows = self._apply_migrations(schema, skip_backup)

            if not migrate_rows and major_minor(SQLMESH_VERSION) == versions.minor_sqlmesh_version:
                return

            if migrate_rows:
                self._migrate_rows(promoted_snapshots_only)
            self.version_state.update_versions()

            analytics.collector.on_migration_end(
                from_sqlmesh_version=versions.sqlmesh_version,
                state_sync_type=self.engine_adapter.dialect,
                migration_time_sec=time.perf_counter() - migration_start_ts,
            )
        except Exception as e:
            if skip_backup:
                logger.error("Backup was skipped so no rollback was attempted.")
            else:
                self.rollback()

            analytics.collector.on_migration_end(
                from_sqlmesh_version=versions.sqlmesh_version,
                state_sync_type=self.engine_adapter.dialect,
                migration_time_sec=time.perf_counter() - migration_start_ts,
                error=e,
            )

            self.console.log_migration_status(success=False)
            if isinstance(e, StateMigrationError):
                raise
            raise SQLMeshError("SQLMesh migration failed.") from e

        self.console.log_migration_status()

    def rollback(self) -> None:
        """Rollback to the previous migration."""
        logger.info("Starting migration rollback.")
        versions = self.version_state.get_versions()
        if versions.schema_version == 0:
            # Clean up state tables
            for table in self._state_tables + self._optional_state_tables:
                self.engine_adapter.drop_table(table)
        else:
            if not all(
                self.engine_adapter.table_exists(_backup_table_name(table))
                for table in self._state_tables
            ):
                raise SQLMeshError("There are no prior migrations to roll back to.")
            for table in self._state_tables:
                self._restore_table(table, _backup_table_name(table))

            for optional_table in self._optional_state_tables:
                if self.engine_adapter.table_exists(_backup_table_name(optional_table)):
                    self._restore_table(optional_table, _backup_table_name(optional_table))

        logger.info("Migration rollback successful.")

    def _apply_migrations(
        self,
        schema: t.Optional[str],
        skip_backup: bool,
    ) -> bool:
        versions = self.version_state.get_versions()
        first_script_index = 0
        if versions.schema_version and versions.schema_version < MIN_SCHEMA_VERSION:
            raise StateMigrationError(
                "The current state belongs to an old version of SQLMesh that is no longer supported. "
                f"Please upgrade to {MIN_SQLMESH_VERSION} first before upgrading to {SQLMESH_VERSION}."
            )
        elif versions.schema_version > 0:
            # -1 to skip the baseline migration script
            first_script_index = versions.schema_version - (MIN_SCHEMA_VERSION - 1)

        migrations = MIGRATIONS[first_script_index:]
        should_backup = any(
            [
                migrations,
                major_minor(SQLGLOT_VERSION) != versions.minor_sqlglot_version,
                major_minor(SQLMESH_VERSION) != versions.minor_sqlmesh_version,
            ]
        )
        if not skip_backup and should_backup:
            self._backup_state()

        snapshot_count_before = self.snapshot_state.count() if versions.schema_version else None

        state_table_exist = any(self.engine_adapter.table_exists(t) for t in self._state_tables)

        for migration in migrations:
            logger.info(f"Applying migration {migration}")
            migration.migrate_schemas(engine_adapter=self.engine_adapter, schema=schema)
            if state_table_exist:
                # No need to run DML for the initial migration since all tables are empty
                migration.migrate_rows(engine_adapter=self.engine_adapter, schema=schema)

        snapshot_count_after = self.snapshot_state.count()

        if snapshot_count_before is not None and snapshot_count_before != snapshot_count_after:
            scripts = f"{versions.schema_version} - {versions.schema_version + len(migrations)}"
            raise SQLMeshError(
                f"Number of snapshots before ({snapshot_count_before}) and after "
                f"({snapshot_count_after}) applying migration scripts {scripts} does not match. "
                "Please file an issue issue at https://github.com/SQLMesh/sqlmesh/issues/new."
            )

        migrate_snapshots_and_environments = (
            bool(migrations) or major_minor(SQLGLOT_VERSION) != versions.minor_sqlglot_version
        )
        return migrate_snapshots_and_environments

    def _migrate_rows(self, promoted_snapshots_only: bool) -> None:
        logger.info("Fetching environments")
        environments = self.environment_state.get_environments()
        # Only migrate snapshots that are part of at least one environment.
        snapshots_to_migrate = (
            {s.snapshot_id for e in environments for s in e.snapshots}
            if promoted_snapshots_only
            else None
        )
        snapshot_mapping = self._migrate_snapshot_rows(snapshots_to_migrate)
        if not snapshot_mapping:
            logger.info("No changes to snapshots detected")
            return
        self._migrate_environment_rows(environments, snapshot_mapping)

    def _migrate_snapshot_rows(
        self, snapshots: t.Optional[t.Set[SnapshotId]]
    ) -> t.Dict[SnapshotId, SnapshotTableInfo]:
        logger.info("Migrating snapshot rows...")
        raw_snapshots = {
            SnapshotId(name=name, identifier=identifier): {
                **json.loads(raw_snapshot),
                "updated_ts": updated_ts,
                "unpaused_ts": unpaused_ts,
                "unrestorable": unrestorable,
                "forward_only": forward_only,
            }
            for where in (
                snapshot_id_filter(
                    self.engine_adapter, snapshots, batch_size=self.SNAPSHOT_BATCH_SIZE
                )
                if snapshots is not None
                else [None]
            )
            for name, identifier, raw_snapshot, updated_ts, unpaused_ts, unrestorable, forward_only in fetchall(
                self.engine_adapter,
                exp.select(
                    "name",
                    "identifier",
                    "snapshot",
                    "updated_ts",
                    "unpaused_ts",
                    "unrestorable",
                    "forward_only",
                )
                .from_(self.snapshot_state.snapshots_table)
                .where(where)
                .lock(),
            )
        }
        if not raw_snapshots:
            return {}

        dag: DAG[SnapshotId] = DAG()
        for snapshot_id, raw_snapshot in raw_snapshots.items():
            parent_ids = [SnapshotId.parse_obj(p_id) for p_id in raw_snapshot.get("parents", [])]
            dag.add(snapshot_id, [p_id for p_id in parent_ids if p_id in raw_snapshots])

        reversed_dag_raw = dag.reversed.graph

        self.console.start_snapshot_migration_progress(len(raw_snapshots))

        parsed_snapshots = LazilyParsedSnapshots(raw_snapshots)
        all_snapshot_mapping: t.Dict[SnapshotId, SnapshotTableInfo] = {}
        snapshot_id_mapping: t.Dict[SnapshotId, SnapshotId] = {}
        new_snapshots: t.Dict[SnapshotId, Snapshot] = {}
        visited: t.Set[SnapshotId] = set()

        def _push_new_snapshots() -> None:
            all_snapshot_mapping.update(
                {
                    from_id: new_snapshots[to_id].table_info
                    for from_id, to_id in snapshot_id_mapping.items()
                }
            )

            existing_new_snapshots = self.snapshot_state.snapshots_exist(new_snapshots)
            new_snapshots_to_push = [
                s for s in new_snapshots.values() if s.snapshot_id not in existing_new_snapshots
            ]
            if new_snapshots_to_push:
                logger.info("Pushing %s migrated snapshots", len(new_snapshots_to_push))
                self._push_snapshots(new_snapshots_to_push)
            new_snapshots.clear()
            snapshot_id_mapping.clear()

        def _visit(
            snapshot_id: SnapshotId, fingerprint_cache: t.Dict[str, SnapshotFingerprint]
        ) -> None:
            if snapshot_id in visited or snapshot_id not in raw_snapshots:
                return
            visited.add(snapshot_id)

            snapshot = parsed_snapshots[snapshot_id]
            node = snapshot.node

            node_seen = set()
            node_queue = {snapshot_id}
            nodes: t.Dict[str, Node] = {}
            while node_queue:
                next_snapshot_id = node_queue.pop()
                next_snapshot = parsed_snapshots.get(next_snapshot_id)

                if next_snapshot_id in node_seen or not next_snapshot:
                    continue

                node_seen.add(next_snapshot_id)
                node_queue.update(next_snapshot.parents)
                nodes[next_snapshot.name] = next_snapshot.node

            new_snapshot = deepcopy(snapshot)
            try:
                new_snapshot.fingerprint = fingerprint_from_node(
                    node,
                    nodes=nodes,
                    cache=fingerprint_cache,
                )
                new_snapshot.parents = tuple(
                    SnapshotId(
                        name=parent_node.fqn,
                        identifier=fingerprint_from_node(
                            parent_node,
                            nodes=nodes,
                            cache=fingerprint_cache,
                        ).to_identifier(),
                    )
                    for parent_node in _parents_from_node(node, nodes).values()
                )
            except Exception:
                logger.exception("Could not compute fingerprint for %s", snapshot.snapshot_id)
                return

            # Reset the effective_from date for the new snapshot to avoid unexpected backfills.
            new_snapshot.effective_from = None
            new_snapshot.previous_versions = snapshot.all_versions
            new_snapshot.migrated = True
            if not new_snapshot.dev_version_:
                new_snapshot.dev_version_ = snapshot.dev_version

            self.console.update_snapshot_migration_progress(1)

            # Visit children and evict them from the parsed_snapshots cache after.
            for child in reversed_dag_raw.get(snapshot_id, []):
                # Make sure to copy the fingerprint cache to avoid sharing it between different child snapshots with the same name.
                _visit(child, fingerprint_cache.copy())
                parsed_snapshots.evict(child)

            if new_snapshot.fingerprint == snapshot.fingerprint:
                logger.debug(f"{new_snapshot.snapshot_id} is unchanged.")
                return

            new_snapshot_id = new_snapshot.snapshot_id

            if new_snapshot_id in raw_snapshots:
                # Mapped to an existing snapshot.
                new_snapshots[new_snapshot_id] = parsed_snapshots[new_snapshot_id]
                logger.debug("Migrated snapshot %s already exists", new_snapshot_id)
            elif (
                new_snapshot_id not in new_snapshots
                or new_snapshot.updated_ts > new_snapshots[new_snapshot_id].updated_ts
            ):
                new_snapshots[new_snapshot_id] = new_snapshot

            snapshot_id_mapping[snapshot.snapshot_id] = new_snapshot_id
            logger.debug("%s mapped to %s", snapshot.snapshot_id, new_snapshot_id)

            if len(new_snapshots) >= self.SNAPSHOT_MIGRATION_BATCH_SIZE:
                _push_new_snapshots()

        for root_snapshot_id in dag.roots:
            _visit(root_snapshot_id, {})

        if new_snapshots:
            _push_new_snapshots()

        self.console.stop_snapshot_migration_progress()
        return all_snapshot_mapping

    def _migrate_environment_rows(
        self,
        environments: t.List[Environment],
        snapshot_mapping: t.Dict[SnapshotId, SnapshotTableInfo],
    ) -> None:
        logger.info("Migrating environment rows...")

        updated_prod_environment: t.Optional[Environment] = None
        updated_environments = []
        for environment in environments:
            snapshots = [
                (
                    snapshot_mapping[info.snapshot_id]
                    if info.snapshot_id in snapshot_mapping
                    else info
                )
                for info in environment.snapshots
            ]

            if snapshots != environment.snapshots:
                environment.snapshots_ = snapshots
                updated_environments.append(environment)
                if environment.name == c.PROD:
                    updated_prod_environment = environment
        self.console.start_env_migration_progress(len(updated_environments))

        for environment in updated_environments:
            self._update_environment(environment)
            self.console.update_env_migration_progress(1)

        if updated_prod_environment:
            try:
                self.snapshot_state.unpause_snapshots(
                    updated_prod_environment.snapshots, now_timestamp()
                )
            except Exception:
                logger.warning("Failed to unpause migrated snapshots", exc_info=True)

        self.console.stop_env_migration_progress()

    def _backup_state(self) -> None:
        for table in [
            *self._state_tables,
            *self._optional_state_tables,
        ]:
            if self.engine_adapter.table_exists(table):
                with self.engine_adapter.transaction():
                    backup_name = _backup_table_name(table)
                    self.engine_adapter.drop_table(backup_name)
                    self.engine_adapter.create_table_like(backup_name, table)
                    self.engine_adapter.insert_append(
                        backup_name, exp.select("*").from_(table), track_rows_processed=False
                    )

    def _restore_table(
        self,
        table_name: TableName,
        backup_table_name: TableName,
    ) -> None:
        self.engine_adapter.drop_table(table_name)
        self.engine_adapter.rename_table(
            old_table_name=backup_table_name,
            new_table_name=table_name,
        )

    def _update_environment(self, environment: Environment) -> None:
        self.environment_state.update_environment(environment)

    def _push_snapshots(self, snapshots: t.Iterable[Snapshot]) -> None:
        self.snapshot_state.push_snapshots(snapshots)


def _backup_table_name(table_name: TableName) -> exp.Table:
    table = exp.to_table(table_name).copy()
    table.set("this", exp.to_identifier(table.name + "_backup"))
    return table


class LazilyParsedSnapshots:
    def __init__(self, raw_snapshots: t.Dict[SnapshotId, t.Dict[str, t.Any]]):
        self._raw_snapshots = raw_snapshots
        self._parsed_snapshots: t.Dict[SnapshotId, t.Optional[Snapshot]] = {}

    def get(self, snapshot_id: SnapshotId) -> t.Optional[Snapshot]:
        if snapshot_id not in self._parsed_snapshots:
            raw_snapshot = self._raw_snapshots.get(snapshot_id)
            if raw_snapshot:
                self._parsed_snapshots[snapshot_id] = Snapshot.parse_obj(raw_snapshot)
            else:
                self._parsed_snapshots[snapshot_id] = None
        return self._parsed_snapshots[snapshot_id]

    def evict(self, snapshot_id: SnapshotId) -> None:
        self._parsed_snapshots.pop(snapshot_id, None)

    def __getitem__(self, snapshot_id: SnapshotId) -> Snapshot:
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        return snapshot
