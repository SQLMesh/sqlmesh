"""
# ContextDiff

ContextDiff encapsulates the differences between two environments. The two environments can be the local
environment and a remote environment, or two remote environments. ContextDiff is an important part of
SQLMesh. SQLMesh plans use ContextDiff to determine what nodes were changed between two environments.
The SQLMesh CLI diff command uses ContextDiff to determine what to visualize.

When creating a ContextDiff object, SQLMesh will compare the snapshots from one environment with those of
another remote environment and determine if nodes have been added, removed, or modified.
"""

from __future__ import annotations

import sys
import typing as t
from difflib import ndiff, unified_diff
from functools import cached_property
from sqlmesh.core import constants as c
from sqlmesh.core.console import get_console
from sqlmesh.core.macros import RuntimeStage
from sqlmesh.core.model.common import sorted_python_env_payloads
from sqlmesh.core.snapshot import Snapshot, SnapshotId, SnapshotTableInfo
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.pydantic import PydanticModel

if sys.version_info >= (3, 12):
    from importlib import metadata
else:
    import importlib_metadata as metadata  # type: ignore


if t.TYPE_CHECKING:
    from sqlmesh.core.state_sync import StateReader

from sqlmesh.utils.metaprogramming import Executable  # noqa
from sqlmesh.core.environment import EnvironmentStatements

IGNORED_PACKAGES = {"sqlmesh", "sqlglot", "sqlglotc"}


class ContextDiff(PydanticModel):
    """ContextDiff is an object representing the difference between two environments.

    The two environments can be the local environment and a remote environment, or two remote
    environments.
    """

    environment: str
    """The environment to diff."""
    is_new_environment: bool
    """Whether the target environment is new."""
    is_unfinalized_environment: bool
    """Whether the currently stored environment record is in unfinalized state."""
    normalize_environment_name: bool
    """Whether the environment name should be normalized."""
    previous_gateway_managed_virtual_layer: bool
    """Whether the previous environment's virtual layer's views were created by the model specified gateways."""
    gateway_managed_virtual_layer: bool
    """Whether the virtual layer's views will be created by the model specified gateways."""
    create_from: str
    """The name of the environment the target environment will be created from if new."""
    create_from_env_exists: bool
    """Whether the create_from environment already exists at plan time."""
    added: t.Set[SnapshotId]
    """New nodes."""
    removed_snapshots: t.Dict[SnapshotId, SnapshotTableInfo]
    """Deleted nodes."""
    modified_snapshots: t.Dict[str, t.Tuple[Snapshot, Snapshot]]
    """Modified snapshots."""
    snapshots: t.Dict[SnapshotId, Snapshot]
    """Merged snapshots."""
    new_snapshots: t.Dict[SnapshotId, Snapshot]
    """New snapshots."""
    previous_plan_id: t.Optional[str]
    """Previous plan id."""
    previously_promoted_snapshot_ids: t.Set[SnapshotId]
    """Snapshot IDs that were promoted by the previous plan."""
    previous_finalized_snapshots: t.Optional[t.List[SnapshotTableInfo]]
    """Snapshots from the previous finalized state."""
    previous_requirements: t.Dict[str, str] = {}
    """Previous requirements."""
    requirements: t.Dict[str, str] = {}
    """Python dependencies."""
    previous_environment_statements: t.List[EnvironmentStatements] = []
    """Previous environment statements."""
    environment_statements: t.List[EnvironmentStatements]
    """Environment statements."""
    diff_rendered: bool = False
    """Whether the diff should compare raw vs rendered models"""

    @classmethod
    def create(
        cls,
        environment: str,
        snapshots: t.Dict[str, Snapshot],
        create_from: str,
        state_reader: StateReader,
        ensure_finalized_snapshots: bool = False,
        provided_requirements: t.Optional[t.Dict[str, str]] = None,
        excluded_requirements: t.Optional[t.Set[str]] = None,
        diff_rendered: bool = False,
        environment_statements: t.Optional[t.List[EnvironmentStatements]] = [],
        gateway_managed_virtual_layer: bool = False,
        infer_python_dependencies: bool = True,
        always_recreate_environment: bool = False,
    ) -> ContextDiff:
        """Create a ContextDiff object.

        Args:
            environment: The remote environment to diff.
            snapshots: The snapshots of the current environment.
            create_from: The environment to create the target environment from if it
                doesn't exist.
            state_reader: StateReader to access the remote environment to diff.
            ensure_finalized_snapshots: Whether to compare against snapshots from the latest finalized
                environment state, or to use whatever snapshots are in the current environment state even if
                the environment is not finalized.
            provided_requirements: Python dependencies sourced from the lock file.
            excluded_requirements: Python dependencies to exclude.
            diff_rendered: Whether to compute the diff of the rendered version of the compared expressions.
            environment_statements: A list of `before_all` or `after_all` statements associated with the environment.
            gateway_managed_virtual_layer: Whether the models' views in the virtual layer are created by the
                model-specific gateway rather than the default gateway.
            infer_python_dependencies: Whether to statically analyze Python code to automatically infer Python
                package requirements.

        Returns:
            The ContextDiff object.
        """
        environment = environment.lower()
        existing_env = state_reader.get_environment(environment)
        create_from_env_exists = False

        recreate_environment = always_recreate_environment and not environment == create_from

        if existing_env is None or existing_env.expired or recreate_environment:
            env = state_reader.get_environment(create_from.lower())

            if not env and create_from != c.PROD:
                get_console().log_warning(
                    f"The environment name '{create_from}' was passed to the `plan` command's `--create-from` argument, but '{create_from}' does not exist. Initializing new environment '{environment}' from scratch."
                )

            is_new_environment = True
            create_from_env_exists = env is not None
            previously_promoted_snapshot_ids = set()
        else:
            env = existing_env
            is_new_environment = False
            previously_promoted_snapshot_ids = {s.snapshot_id for s in env.promoted_snapshots}

        environment_snapshot_infos = []
        if env:
            environment_snapshot_infos = (
                env.snapshots
                if not ensure_finalized_snapshots
                else env.finalized_or_current_snapshots
            )
        remote_snapshot_name_to_info = {
            snapshot_info.name: snapshot_info for snapshot_info in environment_snapshot_infos
        }
        removed = {
            snapshot_table_info.snapshot_id: snapshot_table_info
            for snapshot_table_info in environment_snapshot_infos
            if snapshot_table_info.name not in snapshots
        }
        added = {
            snapshot.snapshot_id
            for snapshot in snapshots.values()
            if snapshot.name not in remote_snapshot_name_to_info
        }
        modified_snapshot_name_to_snapshot_info = {
            snapshot.name: remote_snapshot_name_to_info[snapshot.name]
            for snapshot in snapshots.values()
            if snapshot.snapshot_id not in added
            and snapshot.fingerprint != remote_snapshot_name_to_info[snapshot.name].fingerprint
        }

        stored = state_reader.get_snapshots(
            [*snapshots.values(), *modified_snapshot_name_to_snapshot_info.values()]
        )

        merged_snapshots = {}
        modified_snapshots = {}
        new_snapshots = {}

        for snapshot in snapshots.values():
            s_id = snapshot.snapshot_id
            modified_snapshot_info = modified_snapshot_name_to_snapshot_info.get(snapshot.name)
            existing_snapshot = stored.get(s_id)

            if modified_snapshot_info and snapshot.node_type != modified_snapshot_info.node_type:
                added.add(snapshot.snapshot_id)
                removed[modified_snapshot_info.snapshot_id] = modified_snapshot_info
                modified_snapshot_name_to_snapshot_info.pop(snapshot.name)
            elif existing_snapshot:
                # Keep the original node instance to preserve the query cache.
                existing_snapshot.node = snapshot.node

                merged_snapshots[s_id] = existing_snapshot.copy()
                if modified_snapshot_info:
                    modified_snapshots[s_id.name] = (
                        existing_snapshot,
                        stored[modified_snapshot_info.snapshot_id],
                    )
            else:
                snapshot = snapshot.copy()
                merged_snapshots[s_id] = snapshot
                new_snapshots[snapshot.snapshot_id] = snapshot
                if modified_snapshot_info:
                    snapshot.previous_versions = modified_snapshot_info.all_versions
                    modified_snapshots[s_id.name] = (
                        snapshot,
                        stored[modified_snapshot_info.snapshot_id],
                    )

        requirements = _build_requirements(
            provided_requirements or {},
            excluded_requirements or set(),
            snapshots.values(),
            infer_python_dependencies=infer_python_dependencies,
        )

        previous_environment_statements = (
            state_reader.get_environment_statements(env.name) if env else []
        )

        if existing_env and always_recreate_environment:
            previous_plan_id: t.Optional[str] = existing_env.plan_id
        else:
            previous_plan_id = env.plan_id if env and not is_new_environment else None

        return ContextDiff(
            environment=environment,
            is_new_environment=is_new_environment,
            is_unfinalized_environment=bool(env and not env.finalized_ts),
            normalize_environment_name=is_new_environment or bool(env and env.normalize_name),
            create_from=create_from,
            create_from_env_exists=create_from_env_exists,
            added=added,
            removed_snapshots=removed,
            modified_snapshots=modified_snapshots,
            snapshots=merged_snapshots,
            new_snapshots=new_snapshots,
            previous_plan_id=previous_plan_id,
            previously_promoted_snapshot_ids=previously_promoted_snapshot_ids,
            previous_finalized_snapshots=env.previous_finalized_snapshots if env else None,
            previous_requirements=env.requirements if env else {},
            requirements=requirements,
            diff_rendered=diff_rendered,
            previous_environment_statements=previous_environment_statements,
            environment_statements=environment_statements,
            previous_gateway_managed_virtual_layer=env.gateway_managed if env else False,
            gateway_managed_virtual_layer=gateway_managed_virtual_layer,
        )

    @classmethod
    def create_no_diff(cls, environment: str, state_reader: StateReader) -> ContextDiff:
        """Create a no-op ContextDiff object.

        Args:
            environment: The target environment.
            state_reader: StateReader to access the remote environment record.

        Returns:
            The ContextDiff object.
        """
        env = state_reader.get_environment(environment.lower())
        if not env:
            raise SQLMeshError(f"Environment '{environment}' must exist for this operation.")

        environment_statements = state_reader.get_environment_statements(environment)
        snapshots = state_reader.get_snapshots(env.snapshots)

        return ContextDiff(
            environment=env.name,
            is_new_environment=False,
            is_unfinalized_environment=False,
            normalize_environment_name=env.normalize_name,
            create_from="",
            create_from_env_exists=False,
            added=set(),
            removed_snapshots={},
            modified_snapshots={},
            snapshots=snapshots,
            new_snapshots={},
            previous_plan_id=env.plan_id,
            previously_promoted_snapshot_ids={s.snapshot_id for s in env.promoted_snapshots},
            previous_finalized_snapshots=env.previous_finalized_snapshots,
            previous_requirements=env.requirements,
            requirements=env.requirements,
            previous_environment_statements=environment_statements,
            environment_statements=environment_statements,
            previous_gateway_managed_virtual_layer=env.gateway_managed,
            gateway_managed_virtual_layer=env.gateway_managed,
        )

    @property
    def has_changes(self) -> bool:
        return (
            self.has_snapshot_changes
            or self.is_new_environment
            or self.is_unfinalized_environment
            or self.has_requirement_changes
            or self.has_environment_statements_changes
            or self.previous_gateway_managed_virtual_layer != self.gateway_managed_virtual_layer
        )

    @property
    def has_requirement_changes(self) -> bool:
        return self.previous_requirements != self.requirements

    @property
    def has_environment_statements_changes(self) -> bool:
        return sorted(self.environment_statements, key=lambda s: s.project or "") != sorted(
            self.previous_environment_statements, key=lambda s: s.project or ""
        )

    @property
    def has_snapshot_changes(self) -> bool:
        return bool(self.added or self.removed_snapshots or self.modified_snapshots)

    @property
    def added_materialized_snapshot_ids(self) -> t.Set[SnapshotId]:
        """Returns the set of added internal snapshot ids."""
        return {
            s_id
            for s_id in self.added
            if self.snapshots[s_id].model_kind_name
            and self.snapshots[s_id].model_kind_name.is_materialized  # type: ignore
        }

    @property
    def promotable_snapshot_ids(self) -> t.Set[SnapshotId]:
        """The set of snapshot ids that have to be promoted in the target environment."""
        return {
            *self.previously_promoted_snapshot_ids,
            *self.added,
            *self.current_modified_snapshot_ids,
        } - set(self.removed_snapshots)

    @property
    def unpromoted_models(self) -> t.Set[SnapshotId]:
        """The set of snapshot IDs that have not yet been promoted in the target environment."""
        return set(self.snapshots) - self.previously_promoted_snapshot_ids

    @property
    def current_modified_snapshot_ids(self) -> t.Set[SnapshotId]:
        return {current.snapshot_id for current, _ in self.modified_snapshots.values()}

    @cached_property
    def snapshots_by_name(self) -> t.Dict[str, Snapshot]:
        return {x.name: x for x in self.snapshots.values()}

    def requirements_diff(self) -> str:
        return "    " + "\n    ".join(
            ndiff(
                [
                    f"{k}=={self.previous_requirements[k]}"
                    for k in sorted(self.previous_requirements)
                ],
                [f"{k}=={self.requirements[k]}" for k in sorted(self.requirements)],
            )
        )

    def environment_statements_diff(
        self, include_python_env: bool = False
    ) -> t.List[t.Tuple[str, str]]:
        def extract_statements(statements: t.List[EnvironmentStatements], attr: str) -> t.List[str]:
            return [
                string
                for statement in statements
                for expr in (
                    sorted_python_env_payloads(statement.python_env)
                    if attr == "python_env"
                    else getattr(statement, attr)
                )
                for string in expr.split("\n")
            ]

        def compute_diff(attribute: str) -> t.Optional[t.Tuple[str, str]]:
            previous = extract_statements(self.previous_environment_statements, attribute)
            current = extract_statements(self.environment_statements, attribute)

            if previous == current:
                return None

            diff_text = attribute if not attribute == "python_env" else "dependencies"
            diff_text += ":\n"
            if attribute == "python_env":
                diff = list(unified_diff(previous, current))
                diff_text += "\n".join(diff[2:] if len(diff) > 1 else diff)
                return "python", diff_text + "\n"

            diff_lines = list(ndiff(previous, current))
            if any(line.startswith(("-", "+")) for line in diff_lines):
                diff_text += "  " + "\n  ".join(diff_lines) + "\n"
            return "sql", diff_text

        return [
            diff
            for attribute in [
                RuntimeStage.BEFORE_ALL.value,
                RuntimeStage.AFTER_ALL.value,
                *(["python_env"] if include_python_env else []),
            ]
            if (diff := compute_diff(attribute)) is not None
        ]

    @property
    def environment_snapshots(self) -> t.List[SnapshotTableInfo]:
        """Returns current snapshots in the environment."""
        return [
            *self.removed_snapshots.values(),
            *(old.table_info for _, old in self.modified_snapshots.values()),
            *[
                s.table_info
                for s_id, s in self.snapshots.items()
                if s_id not in self.added and s.name not in self.modified_snapshots
            ],
        ]

    def directly_modified(self, name: str) -> bool:
        """Returns whether or not a node was directly modified in this context.

        Args:
            name: The snapshot name to check.

        Returns:
            Whether or not the node was directly modified.
        """

        if name not in self.modified_snapshots:
            return False

        current, previous = self.modified_snapshots[name]
        return current.is_directly_modified(previous)

    def indirectly_modified(self, name: str) -> bool:
        """Returns whether or not a node was indirectly modified in this context.

        Args:
            name: The snapshot name to check.

        Returns:
            Whether or not the node was indirectly modified.
        """

        if name not in self.modified_snapshots:
            return False

        current, previous = self.modified_snapshots[name]
        return current.is_indirectly_modified(previous)

    def metadata_updated(self, name: str) -> bool:
        """Returns whether or not the given node's metadata has been updated.

        Args:
            name: The node to check.

        Returns:
            Whether or not the node's metadata has been updated.
        """

        if name not in self.modified_snapshots:
            return False

        current, previous = self.modified_snapshots[name]
        return current.is_metadata_updated(previous)

    def text_diff(self, name: str) -> str:
        """Finds the difference of a node between the current and remote environment.

        Args:
            name: The Snapshot name.

        Returns:
            A unified text diff of the node.
        """
        if name not in self.snapshots_by_name:
            raise SQLMeshError(f"`{name}` does not exist.")
        if name not in self.modified_snapshots:
            return ""

        new, old = self.modified_snapshots[name]
        try:
            return old.node.text_diff(new.node, rendered=self.diff_rendered)
        except SQLMeshError as e:
            get_console().log_warning(f"Failed to diff model '{name}': {str(e)}.")
            return ""


def _build_requirements(
    provided_requirements: t.Dict[str, str],
    excluded_requirements: t.Set[str],
    snapshots: t.Collection[Snapshot],
    infer_python_dependencies: bool = True,
) -> t.Dict[str, str]:
    requirements = {
        k: v for k, v in provided_requirements.items() if k not in excluded_requirements
    }

    if not infer_python_dependencies:
        return requirements

    distributions = metadata.packages_distributions()

    for snapshot in snapshots:
        if not snapshot.is_model:
            continue

        for executable in snapshot.model.python_env.values():
            if executable.kind != "import":
                continue

            try:
                start = "from " if executable.payload.startswith("from ") else "import "
                lib = executable.payload.split(start)[1].split()[0].split(".")[0]
                if lib not in distributions:
                    continue

                for dist in distributions[lib]:
                    if (
                        dist not in requirements
                        and dist not in IGNORED_PACKAGES
                        and dist not in excluded_requirements
                    ):
                        requirements[dist] = metadata.version(dist)
            except metadata.PackageNotFoundError:
                from sqlmesh.core.console import get_console

                get_console().log_warning(f"Failed to find package for {lib}.")

    return requirements
