from __future__ import annotations

import abc
import importlib
import logging
import pkgutil
import typing as t

from sqlglot import __version__ as SQLGLOT_VERSION

from sqlmesh import migrations
from sqlmesh.core.environment import (
    Environment,
    EnvironmentStatements,
    EnvironmentSummary,
)
from sqlmesh.core.snapshot import (
    Snapshot,
    SnapshotId,
    SnapshotIdLike,
    SnapshotIdAndVersionLike,
    SnapshotInfoLike,
    SnapshotNameVersion,
    SnapshotIdAndVersion,
)
from sqlmesh.core.snapshot.definition import Interval, SnapshotIntervals
from sqlmesh.utils import major_minor
from sqlmesh.utils.date import TimeLike
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.pydantic import PydanticModel, field_validator
from sqlmesh.core.state_sync.common import (
    StateStream,
    ExpiredSnapshotBatch,
    PromotionResult,
    ExpiredBatchRange,
)

logger = logging.getLogger(__name__)


class Versions(PydanticModel):
    """Represents the various versions of dependencies in the state sync."""

    schema_version: int = 0
    sqlglot_version: str = "0.0.0"
    sqlmesh_version: str = "0.0.0"

    @property
    def minor_sqlglot_version(self) -> t.Tuple[int, int]:
        return major_minor(self.sqlglot_version)

    @property
    def minor_sqlmesh_version(self) -> t.Tuple[int, int]:
        return major_minor(self.sqlmesh_version)

    @field_validator("sqlglot_version", "sqlmesh_version", mode="before")
    @classmethod
    def _package_version_validator(cls, v: t.Any) -> str:
        return "0.0.0" if v is None else str(v)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_validator(cls, v: t.Any) -> int:
        return 0 if v is None else int(v)


MIN_SCHEMA_VERSION = 60
MIN_SQLMESH_VERSION = "0.134.0"
MIGRATIONS = [
    importlib.import_module(f"sqlmesh.migrations.{migration}")
    for migration in sorted(info.name for info in pkgutil.iter_modules(migrations.__path__))
]
# -1 to account for the baseline script
SCHEMA_VERSION: int = MIN_SCHEMA_VERSION + len(MIGRATIONS) - 1


class StateReader(abc.ABC):
    """Abstract base class for read-only operations on snapshot and environment state."""

    @abc.abstractmethod
    def get_snapshots(
        self, snapshot_ids: t.Iterable[SnapshotIdLike]
    ) -> t.Dict[SnapshotId, Snapshot]:
        """Bulk fetch snapshots given the corresponding snapshot ids.

        Args:
            snapshot_ids: Iterable of snapshot ids to get.

        Returns:
            A dictionary of snapshot ids to snapshots for ones that could be found.
        """

    @abc.abstractmethod
    def get_snapshots_by_names(
        self,
        snapshot_names: t.Iterable[str],
        current_ts: t.Optional[int] = None,
        exclude_expired: bool = True,
    ) -> t.Set[SnapshotIdAndVersion]:
        """Return the snapshot records for all versions of the specified snapshot names.

        Args:
            snapshot_names: Iterable of snapshot names to fetch all snapshot records for
            current_ts: Sets the current time for identifying which snapshots have expired so they can be excluded (only relevant if :exclude_expired=True)
            exclude_expired: Whether or not to return the snapshot id's of expired snapshots in the result

        Returns:
            A set containing all the matched snapshot records. To fetch full snapshots, pass it into StateSync.get_snapshots()
        """

    @abc.abstractmethod
    def snapshots_exist(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> t.Set[SnapshotId]:
        """Checks if multiple snapshots exist in the state sync.

        Args:
            snapshot_ids: Iterable of snapshot ids to bulk check.

        Returns:
            A set of all the existing snapshot ids.
        """

    @abc.abstractmethod
    def refresh_snapshot_intervals(self, snapshots: t.Collection[Snapshot]) -> t.List[Snapshot]:
        """Updates given snapshots with latest intervals from the state.

        Args:
            snapshots: The snapshots to refresh.

        Returns:
            The updated snapshots.
        """

    @abc.abstractmethod
    def nodes_exist(self, names: t.Iterable[str], exclude_external: bool = False) -> t.Set[str]:
        """Returns the node names that exist in the state sync.

        Args:
            names: Iterable of node names to check.
            exclude_external: Whether to exclude external models from the output.

        Returns:
            A set of all the existing node names.
        """

    @abc.abstractmethod
    def get_environment(self, environment: str) -> t.Optional[Environment]:
        """Fetches the environment if it exists.

        Args:
            environment: The environment

        Returns:
            The environment object.
        """

    @abc.abstractmethod
    def get_environments(self) -> t.List[Environment]:
        """Fetches all environments.

        Returns:
            A list of all environments.
        """

    @abc.abstractmethod
    def get_environments_summary(self) -> t.List[EnvironmentSummary]:
        """Fetches all environment names along with expiry datetime.

        Returns:
            A list of all environment summaries.
        """

    @abc.abstractmethod
    def max_interval_end_per_model(
        self,
        environment: str,
        models: t.Optional[t.Set[str]] = None,
        ensure_finalized_snapshots: bool = False,
    ) -> t.Dict[str, int]:
        """Returns the max interval end per model for the given environment.

        Args:
            environment: The target environment.
            models: The models to get the max interval end for. If None, all models are considered.
            ensure_finalized_snapshots: Whether to use snapshots from the latest finalized environment state,
                or to use whatever snapshots are in the current environment state even if the environment is not finalized.

        Returns:
            A dictionary of model FQNs to their respective interval ends in milliseconds since epoch.
        """

    @abc.abstractmethod
    def recycle(self) -> None:
        """Closes all open connections and releases all allocated resources associated with any thread
        except the calling one."""

    @abc.abstractmethod
    def close(self) -> None:
        """Closes all open connections and releases all allocated resources."""

    @abc.abstractmethod
    def state_type(self) -> str:
        """Returns the type of state sync."""

    @abc.abstractmethod
    def update_auto_restatements(
        self, next_auto_restatement_ts: t.Dict[SnapshotNameVersion, t.Optional[int]]
    ) -> None:
        """Updates the next auto restatement timestamp for the snapshots.

        Args:
            next_auto_restatement_ts: A dictionary of snapshot name / version pairs to the next auto restatement timestamp.
        """

    @abc.abstractmethod
    def get_environment_statements(self, environment: str) -> t.List[EnvironmentStatements]:
        """Fetches environment statements from the environment_statements table.

        Returns:
            A list of the Environment Statements.
        """

    def get_versions(self, validate: bool = True) -> Versions:
        """Get the current versions of the SQLMesh schema and libraries.

        Args:
            validate: Whether or not to raise error if the running version is different from what's in state.

        Returns:
            The versions object.
        """
        from sqlmesh._version import __version__ as SQLMESH_VERSION

        versions = self._get_versions()

        if validate:

            def raise_error(
                lib: str,
                local: str | int,
                remote: str | int,
                remote_package_version: t.Optional[str] = None,
                ahead: bool = False,
            ) -> None:
                if ahead:
                    raise SQLMeshError(
                        f"{lib} (local) is using version '{local}' which is ahead of '{remote}' (remote). "
                        "Please run a migration ('sqlmesh migrate' command)."
                    )

                if remote_package_version:
                    upgrade_suggestion = f" Please upgrade {lib} ('pip install --upgrade \"{lib.lower()}=={remote_package_version}\"' command)."
                else:
                    upgrade_suggestion = ""

                raise SQLMeshError(
                    f"{lib} (local) is using version '{local}' which is behind '{remote}' (remote).{upgrade_suggestion}"
                )

            if major_minor(SQLMESH_VERSION) != major_minor(versions.sqlmesh_version):
                raise_error(
                    "SQLMesh",
                    SQLMESH_VERSION,
                    versions.sqlmesh_version,
                    remote_package_version=versions.sqlmesh_version,
                    ahead=major_minor(SQLMESH_VERSION) > major_minor(versions.sqlmesh_version),
                )

            if SCHEMA_VERSION != versions.schema_version:
                raise_error(
                    "SQLMesh",
                    SCHEMA_VERSION,
                    versions.schema_version,
                    remote_package_version=versions.sqlmesh_version,
                    ahead=SCHEMA_VERSION > versions.schema_version,
                )

            if major_minor(SQLGLOT_VERSION) != major_minor(versions.sqlglot_version):
                raise_error(
                    "SQLGlot",
                    SQLGLOT_VERSION,
                    versions.sqlglot_version,
                    remote_package_version=versions.sqlglot_version,
                    ahead=major_minor(SQLGLOT_VERSION) > major_minor(versions.sqlglot_version),
                )

        return versions

    @abc.abstractmethod
    def _get_versions(self) -> Versions:
        """Queries the store to get the current versions of SQLMesh and deps.

        Returns:
            The versions object.
        """

    @abc.abstractmethod
    def export(self, environment_names: t.Optional[t.List[str]] = None) -> StateStream:
        """Export the contents of this StateSync as a StateStream

        Args:
            environment_names: An optional list of environment names to export. If not specified, all environments will be exported.
        """

    @abc.abstractmethod
    def get_expired_snapshots(
        self,
        *,
        batch_range: ExpiredBatchRange,
        current_ts: t.Optional[int] = None,
        ignore_ttl: bool = False,
    ) -> t.Optional[ExpiredSnapshotBatch]:
        """Returns a single batch of expired snapshots ordered by (updated_ts, name, identifier).

        Args:
            current_ts: Timestamp used to evaluate expiration.
            ignore_ttl: If True, include snapshots regardless of TTL (only checks if unreferenced).
            batch_range: The range of the batch to fetch.

        Returns:
            A batch describing expired snapshots or None if no snapshots are pending cleanup.
        """

    @abc.abstractmethod
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


class StateSync(StateReader, abc.ABC):
    """Abstract base class for snapshot and environment state management."""

    @abc.abstractmethod
    def push_snapshots(self, snapshots: t.Iterable[Snapshot]) -> None:
        """Push snapshots into the state sync.

        This method only allows for pushing new snapshots. If existing snapshots are found,
        this method should raise an error.

        Raises:
            SQLMeshError when existing snapshots are pushed.

        Args:
            snapshots: A list of snapshots to save in the state sync.
        """

    @abc.abstractmethod
    def delete_snapshots(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> None:
        """Delete snapshots from the state sync.

        Args:
            snapshot_ids: A list of snapshot like objects to delete.
        """

    @abc.abstractmethod
    def delete_expired_snapshots(
        self,
        batch_range: ExpiredBatchRange,
        ignore_ttl: bool = False,
        current_ts: t.Optional[int] = None,
    ) -> None:
        """Removes expired snapshots.

        Expired snapshots are snapshots that have exceeded their time-to-live
        and are no longer in use within an environment.

        Args:
            batch_range: The range of snapshots to delete in this batch.
            ignore_ttl: Ignore the TTL on the snapshot when considering it expired. This has the effect of deleting
                all snapshots that are not referenced in any environment
            current_ts: Timestamp used to evaluate expiration.
        """

    @abc.abstractmethod
    def invalidate_environment(self, name: str, protect_prod: bool = True) -> None:
        """Invalidates the target environment by setting its expiration timestamp to now.

        Args:
            name: The name of the environment to invalidate.
            protect_prod: If True, prevents invalidation of the production environment.
        """

    @abc.abstractmethod
    def remove_state(self, including_backup: bool = False) -> None:
        """Removes the state store objects."""

    @abc.abstractmethod
    def remove_intervals(
        self,
        snapshot_intervals: t.Sequence[t.Tuple[SnapshotIdAndVersionLike, Interval]],
        remove_shared_versions: bool = False,
    ) -> None:
        """Remove an interval from a list of snapshots and sync it to the store.

        Because multiple snapshots can be pointing to the same version or physical table, this method
        can also grab all snapshots tied to the passed in version.

        Args:
            snapshot_intervals: The snapshot intervals to remove.
            remove_shared_versions: Whether to remove intervals for snapshots that share the same version with the target snapshots.
        """

    @abc.abstractmethod
    def promote(
        self,
        environment: Environment,
        no_gaps_snapshot_names: t.Optional[t.Set[str]] = None,
        environment_statements: t.Optional[t.List[EnvironmentStatements]] = None,
    ) -> PromotionResult:
        """Update the environment to reflect the current state.

        This method verifies that snapshots have been pushed.

        Args:
            environment: The environment to promote.
            no_gaps_snapshot_names: A set of snapshot names to check for data gaps. If None,
                all snapshots will be checked. The data gap check ensures that models that are already a
                part of the target environment have no data gaps when compared against previous
                snapshots for same models.

        Returns:
           A tuple of (added snapshot table infos, removed snapshot table infos)
        """

    @abc.abstractmethod
    def finalize(self, environment: Environment) -> None:
        """Finalize the target environment, indicating that this environment has been
        fully promoted and is ready for use.

        Args:
            environment: The target environment to finalize.
        """

    @abc.abstractmethod
    def delete_expired_environments(
        self, current_ts: t.Optional[int] = None, name: t.Optional[str] = None
    ) -> t.List[EnvironmentSummary]:
        """Removes expired environments.

        Expired environments are environments that have exceeded their time-to-live value.

        Args:
            current_ts: The current timestamp in milliseconds. Defaults to now.
            name: If provided, only the environment with this name is deleted.

        Returns:
            The list of removed environments.
        """

    @abc.abstractmethod
    def unpause_snapshots(
        self, snapshots: t.Collection[SnapshotInfoLike], unpaused_dt: TimeLike
    ) -> None:
        """Unpauses target snapshots.

        Unpaused snapshots are scheduled for evaluation on a recurring basis.
        Once unpaused a snapshot can't be paused again.

        Args:
            snapshots: Target snapshots.
            unpaused_dt: The datetime object which indicates when target snapshots
                were unpaused.
        """

    @abc.abstractmethod
    def compact_intervals(self) -> None:
        """Compacts intervals for all snapshots.

        Compaction process involves merging of existing interval records into new records and
        then deleting the old ones.
        """

    @abc.abstractmethod
    def migrate(
        self,
        skip_backup: bool = False,
        promoted_snapshots_only: bool = True,
    ) -> None:
        """Migrate the state sync to the latest SQLMesh / SQLGlot version."""

    @abc.abstractmethod
    def rollback(self) -> None:
        """Rollback to previous backed up state."""

    @abc.abstractmethod
    def add_snapshots_intervals(self, snapshots_intervals: t.Sequence[SnapshotIntervals]) -> None:
        """Add snapshot intervals to state

        Args:
            snapshots_intervals: The intervals to add.
        """

    def add_interval(
        self,
        snapshot: Snapshot,
        start: TimeLike,
        end: TimeLike,
        is_dev: bool = False,
        last_altered_ts: t.Optional[int] = None,
    ) -> None:
        """Add an interval to a snapshot and sync it to the store.

        Args:
            snapshot: The snapshot like object to add an interval to.
            start: The start of the interval to add.
            end: The end of the interval to add.
            is_dev: Indicates whether the given interval is being added while in development mode
            last_altered_ts: The timestamp of the last modification of the physical table
        """
        start_ts, end_ts = snapshot.inclusive_exclusive(start, end, strict=False, expand=False)
        if not snapshot.version:
            raise SQLMeshError("Snapshot version must be set to add an interval.")
        intervals = [(start_ts, end_ts)]
        snapshot_intervals = SnapshotIntervals(
            name=snapshot.name,
            identifier=snapshot.identifier,
            version=snapshot.version,
            dev_version=snapshot.dev_version,
            intervals=intervals if not is_dev else [],
            dev_intervals=intervals if is_dev else [],
            last_altered_ts=last_altered_ts if not is_dev else None,
            dev_last_altered_ts=last_altered_ts if is_dev else None,
        )
        self.add_snapshots_intervals([snapshot_intervals])

    @abc.abstractmethod
    def import_(self, stream: StateStream, clear: bool = True) -> None:
        """
        Replace the existing state with the state contained in the StateStream

        Args:
            stream: The stream of new state
            clear: Whether or not to clear existing state before inserting state from the stream
        """


class DelegatingStateSync(StateSync):
    def __init__(self, state_sync: StateSync) -> None:
        self.state_sync = state_sync


def _create_delegate_method(name: str) -> t.Callable:
    def delegate(self: t.Any, *args: t.Any, **kwargs: t.Any) -> t.Any:
        return getattr(self.state_sync, name)(*args, **kwargs)

    return delegate


DelegatingStateSync.__abstractmethods__ = frozenset()
for name in StateSync.__abstractmethods__:
    setattr(DelegatingStateSync, name, _create_delegate_method(name))
