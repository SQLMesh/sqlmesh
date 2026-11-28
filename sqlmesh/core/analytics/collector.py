from __future__ import annotations

import itertools
import json
import typing as t
from functools import cached_property
from pathlib import Path

from sqlmesh.core import constants as c
from sqlmesh.core.analytics.dispatcher import AsyncEventDispatcher, EventDispatcher
from sqlmesh.utils import random_id
from sqlmesh.utils.date import now_timestamp
from sqlmesh.utils.hashing import md5
from sqlmesh.utils.pydantic import PydanticModel
from sqlmesh.utils.yaml import dump as yaml_dump
from sqlmesh.utils.yaml import load as yaml_load

if t.TYPE_CHECKING:
    from sqlmesh.cicd.config import CICDBotConfig
    from sqlmesh.core.plan import EvaluatablePlan
    from sqlmesh.core.snapshot import Snapshot


class AnalyticsCollector:
    def __init__(
        self,
        dispatcher: t.Optional[EventDispatcher] = None,
        sqlmesh_path: t.Optional[Path] = None,
    ):
        self._dispatcher = dispatcher or AsyncEventDispatcher()
        self._sqlmesh_path = sqlmesh_path or c.SQLMESH_PATH
        self._process_id = random_id()
        self._seq_num = itertools.count()

    def on_cicd_command(
        self,
        *,
        command_name: str,
        command_args: t.Collection[str],
        parent_command_names: t.Collection[str],
        cicd_bot_config: t.Optional[CICDBotConfig],
    ) -> None:
        """Called when a CICD command is executed.

        Args:
            command_name: The name of the command.
            command_args: The arguments of the command.
            parent_command_names: The names of the parent commands.
            cicd_config: The CICD bot configuration.
        """
        additional_args = {}
        if cicd_bot_config is not None and getattr(cicd_bot_config, "FIELDS_FOR_ANALYTICS", None):
            additional_args["cicd_bot_config"] = cicd_bot_config.dict(
                include=cicd_bot_config.FIELDS_FOR_ANALYTICS, mode="json"
            )
        self._on_command(
            "CICD_COMMAND",
            command_name,
            command_args,
            parent_command_names=parent_command_names,
            **additional_args,
        )

    def on_cli_command(
        self,
        *,
        command_name: str,
        command_args: t.Collection[str],
        parent_command_names: t.Collection[str],
    ) -> None:
        """Called when a CLI command is executed.

        Args:
            command_name: The name of the command.
            command_args: The arguments of the command.
            parent_command_names: The names of the parent commands.
        """
        self._on_command(
            "CLI_COMMAND", command_name, command_args, parent_command_names=parent_command_names
        )

    def on_magic_command(self, *, command_name: str, command_args: t.Collection[str]) -> None:
        """Called when a Notebook magic command is executed.

        Args:
            command_name: The name of the command.
            command_args: The arguments of the command.
        """
        self._on_command("MAGIC_COMMAND", command_name, command_args)

    def on_python_api_command(self, *, command_name: str, command_args: t.Collection[str]) -> None:
        """Called when a Python method is called directly.

        Args:
            command_name: The name of the command.
            command_args: The arguments of the command.
        """
        self._on_command("PYTHON_API_COMMAND", command_name, command_args)

    def on_project_loaded(
        self,
        *,
        project_type: str,
        models_count: int,
        audits_count: int,
        standalone_audits_count: int,
        macros_count: int,
        jinja_macros_count: int,
        load_time_sec: float,
        state_sync_fingerprint: str,
        project_name: str,
    ) -> None:
        """Called when a project is loaded.

        Args:
            project_type: The type of the project. Eg. "dbt" or "native".
            models_count: The number of models in the project.
            audits_count: The number of audits in the project.
            standalone_audits_count: The number of standalone audits in the project.
            macros_count: The number of macros in the project.
            jinja_macros_count: The number of Jinja macros in the project.
            load_time_sec: The time it took to load the project in (fractional) seconds.
            state_sync_fingerprint: The fingerprint of the state sync configuration.
            project_name: The name of the project.
        """
        project_type = project_type.lower()
        event_data = {
            "project_type": project_type,
            "models_count": models_count,
            "audits_count": audits_count,
            "standalone_audits_count": standalone_audits_count,
            "macros_count": macros_count,
            "jinja_macros_count": jinja_macros_count,
            "load_time_ms": int(load_time_sec * 1000),
            "state_sync_fingerprint": state_sync_fingerprint,
            "project_name_hash": _anonymize(project_name),
        }

        if project_type in {c.DBT, c.HYBRID}:
            from dbt.version import __version__ as dbt_version

            event_data["dbt_version"] = dbt_version

        self._add_event("PROJECT_LOADED", event_data)

    def on_plan_apply_start(
        self,
        *,
        plan: EvaluatablePlan,
        engine_type: t.Optional[str],
        state_sync_type: t.Optional[str],
        scheduler_type: str,
    ) -> None:
        """Called after the plan application starts.

        Args:
            plan: The plan that is being applied.
            engine_type: The type of the target engine.
            state_sync_type: The type of the engine used to store the SQLMesh state.
            scheduler_type: The type of the scheduler being used. Eg. "builtin".
        """
        self._add_event(
            "PLAN_APPLY_START",
            {
                "plan_id": plan.plan_id,
                "engine_type": engine_type.lower() if engine_type is not None else None,
                "state_sync_type": state_sync_type.lower() if state_sync_type is not None else None,
                "scheduler_type": scheduler_type.lower(),
                "is_dev": plan.is_dev,
                "skip_backfill": plan.skip_backfill,
                "no_gaps": plan.no_gaps,
                "forward_only": plan.forward_only,
                "ensure_finalized_snapshots": plan.ensure_finalized_snapshots,
                "has_restatements": plan.is_restatement,
                "directly_modified_count": len(plan.directly_modified_snapshots),
                "indirectly_modified_count": len(
                    {
                        s_id
                        for s_ids in plan.indirectly_modified_snapshots.values()
                        for s_id in s_ids
                    }
                ),
                "environment_name_hash": _anonymize(plan.environment.name),
            },
        )

    def on_plan_apply_end(self, *, plan_id: str, error: t.Optional[t.Any] = None) -> None:
        """Called after the plan application ends.

        Args:
            plan_id: The ID of the plan that was applied.
            error: The error that occurred during plan application, if any.
        """
        self._add_event(
            "PLAN_APPLY_END",
            {
                "plan_id": plan_id,
                "succeeded": error is None,
                "error": type(error).__name__ if error else None,
            },
        )

    def on_snapshots_created(self, *, new_snapshots: t.Collection[Snapshot], plan_id: str) -> None:
        """Called after new snapshots were created and stored in the SQLMesh state.

        Args:
            new_snapshots: The list of new snapshots.
            plan_id: The ID of the plan that created the snapshots.
        """
        if not new_snapshots:
            return
        snapshots = []
        for snapshot in new_snapshots:
            snapshots.append(
                {
                    "name_hash": _anonymize(snapshot.name),
                    "identifier": snapshot.identifier,
                    "version": snapshot.version,
                    "node_type": snapshot.node_type.lower(),
                    "model_kind": snapshot.model.kind.name.value.lower()
                    if snapshot.is_model
                    else None,
                    "is_sql": snapshot.model.is_sql if snapshot.is_model else None,
                    "change_category": (
                        snapshot.change_category.name.lower() if snapshot.change_category else None
                    ),
                    "dialect": getattr(snapshot.node, "dialect", None),
                    "audits_count": len(snapshot.model.audits) if snapshot.is_model else None,
                    "effective_from_set": snapshot.effective_from is not None,
                }
            )
        self._add_event("SNAPSHOTS_CREATED", {"plan_id": plan_id, "snapshots": snapshots})

    def on_run_start(self, *, engine_type: str, state_sync_type: str) -> str:
        """Called after a run starts.

        Args:
            engine_type: The type of the target engine.
            state_sync_type: The type of the engine used to store the SQLMesh state.

        Returns:
            The run ID.
        """
        run_id = random_id()
        self._add_event(
            "RUN_START",
            {
                "run_id": run_id,
                "engine_type": engine_type.lower(),
                "state_sync_type": state_sync_type.lower(),
            },
        )
        return run_id

    def on_run_end(
        self, *, run_id: str, succeeded: bool, interrupted: bool, error: t.Optional[t.Any] = None
    ) -> None:
        """Called after a run ends.

        Args:
            run_id: The ID of the run.
            succeeded: Whether the run succeeded.
            interrupted: Whether the run was interrupted.
            error: The error that occurred during the run, if any.
        """
        self._add_event(
            "RUN_END",
            {
                "run_id": run_id,
                "succeeded": succeeded,
                "interrupted": interrupted,
                "error": type(error).__name__ if error else None,
            },
        )

    def on_migration_end(
        self,
        *,
        from_sqlmesh_version: str,
        state_sync_type: str,
        migration_time_sec: float,
        error: t.Optional[t.Any] = None,
    ) -> None:
        """Called after the migration of the SQLMesh state ends.

        Args:
            from_sqlmesh_version: The version of SQLMesh from which the migration started.
            state_sync_type: The type of the engine used to store the SQLMesh state.
            migration_time_sec: The time it took to migrate the SQLMesh state in (fractional) seconds.
            error: The error that occurred during the migration, if any.
        """
        self._add_event(
            "MIGRATION_END",
            {
                "from_sqlmesh_version": from_sqlmesh_version,
                "state_sync_type": state_sync_type,
                "succeeded": error is None,
                "error": type(error).__name__ if error else None,
                "migration_time_ms": int(migration_time_sec * 1000),
            },
        )

    def flush(self) -> None:
        """Flushes the events to the dispatcher."""
        self._dispatcher.flush()

    def shutdown(self, flush: bool = True) -> None:
        """Shuts down the collector."""
        self._dispatcher.shutdown(flush=flush)

    def _on_command(
        self,
        event_type: str,
        command_name: str,
        command_args: t.Collection[str],
        **kwargs: t.Any,
    ) -> None:
        event = {
            "command_name": command_name,
            "command_args": list(command_args),
            **kwargs,
        }
        self._add_event(event_type, event)

    def _add_event(self, event_type: str, event: t.Dict[str, t.Any]) -> None:
        self._dispatcher.add_event(
            {
                "user_id": self._user.id,
                "process_id": self._process_id,
                "seq_num": next(self._seq_num),
                "event_type": event_type,
                "client_ts": now_timestamp(),
                "event": json.dumps(event),
            }
        )

    @cached_property
    def _user(self) -> User:
        return User.load_or_create(self._sqlmesh_path)


class User(PydanticModel):
    id: str

    @classmethod
    def load_or_create(cls, sqlmesh_path: Path) -> User:
        if not sqlmesh_path.exists():
            sqlmesh_path.mkdir(parents=True, exist_ok=True)

        user_path = sqlmesh_path / "user.yaml"
        if user_path.exists():
            raw_user = yaml_load(user_path, raise_if_empty=False)
            if raw_user:
                return cls.parse_obj(raw_user)

        user = User(id=random_id())
        with user_path.open("w") as fd:
            yaml_dump(user.dict(mode="json"), stream=fd)
        return user


def _anonymize(value: str) -> str:
    return md5([value])
