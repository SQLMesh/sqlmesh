from __future__ import annotations

import json
import re
import typing as t

from pydantic import Field

from sqlmesh.core import constants as c
from sqlmesh.core.config import EnvironmentSuffixTarget
from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.macros import RuntimeStage
from sqlmesh.core.renderer import render_statements
from sqlmesh.core.snapshot import SnapshotId, SnapshotTableInfo, Snapshot
from sqlmesh.utils import word_characters_only
from sqlmesh.utils.date import TimeLike, now_timestamp
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.jinja import JinjaMacroRegistry
from sqlmesh.utils.metaprogramming import Executable
from sqlmesh.utils.pydantic import PydanticModel, field_validator, ValidationInfo

T = t.TypeVar("T", bound="EnvironmentNamingInfo")
PydanticType = t.TypeVar("PydanticType", bound="PydanticModel")


class EnvironmentNamingInfo(PydanticModel):
    """
    Information required for creating an object within an environment

    Args:
        name: The name of the environment.
        suffix_target: Indicates whether to append the environment name to the schema or table name.
        catalog_name_override: The name of the catalog to use for this environment if an override was provided
        normalize_name: Indicates whether the environment's name will be normalized. For example, if it's
            `dev`, then it will become `DEV` when targeting Snowflake.
        gateway_managed: Determines whether the virtual layer's views are created by the model-specific
            gateways, otherwise the default gateway is used. Default: False.
    """

    name: str = c.PROD
    suffix_target: EnvironmentSuffixTarget = Field(default=EnvironmentSuffixTarget.SCHEMA)
    catalog_name_override: t.Optional[str] = None
    normalize_name: bool = True
    gateway_managed: bool = False

    @property
    def is_dev(self) -> bool:
        return self.name.lower() != c.PROD

    @field_validator("name", mode="before")
    @classmethod
    def _sanitize_name(cls, v: str) -> str:
        return word_characters_only(v).lower()

    @field_validator("normalize_name", "gateway_managed", mode="before")
    @classmethod
    def _validate_boolean_field(cls, v: t.Any, info: ValidationInfo) -> bool:
        if v is None:
            # Pydantic 2.13+ sets field_name to None during model_validate_json()
            return (info.field_name or "") == "normalize_name"
        return bool(v)

    @t.overload
    @classmethod
    def sanitize_name(cls, v: str) -> str: ...

    @t.overload
    @classmethod
    def sanitize_name(cls, v: Environment) -> Environment: ...

    @classmethod
    def sanitize_name(cls, v: str | Environment) -> str | Environment:
        """
        Sanitizes the environment name so we create names that are valid names for database objects.
        This means alphanumeric and underscores only. Invalid characters are replaced with underscores.
        """
        if isinstance(v, Environment):
            return v
        if not isinstance(v, str):
            raise TypeError(f"Expected str or Environment, got {type(v).__name__}")
        return cls._sanitize_name(v)

    @classmethod
    def sanitize_names(cls, values: t.Iterable[str]) -> t.Set[str]:
        return {cls.sanitize_name(value) for value in values}

    @classmethod
    def from_environment_catalog_mapping(
        cls: t.Type[T],
        environment_catalog_mapping: t.Dict[re.Pattern, str],
        name: str = c.PROD,
        **kwargs: t.Any,
    ) -> T:
        construction_kwargs = dict(name=name, **kwargs)
        for re_pattern, catalog_name in environment_catalog_mapping.items():
            if re.match(re_pattern, name):
                return cls(
                    catalog_name_override=catalog_name,
                    **construction_kwargs,
                )
        return cls(**construction_kwargs)


class EnvironmentSummary(PydanticModel):
    """Represents summary information of an isolated environment.

    Args:
        name: The name of the environment.
        start_at: The start time of the environment.
        end_at: The end time of the environment.
        plan_id: The ID of the plan that last updated this environment.
        previous_plan_id: The ID of the previous plan that updated this environment.
        expiration_ts: The timestamp when this environment will expire.
        finalized_ts: The timestamp when this environment was finalized.
    """

    name: str
    start_at: TimeLike
    end_at: t.Optional[TimeLike] = None
    plan_id: str
    previous_plan_id: t.Optional[str] = None
    expiration_ts: t.Optional[int] = None
    finalized_ts: t.Optional[int] = None

    @property
    def expired(self) -> bool:
        return self.expiration_ts is not None and self.expiration_ts <= now_timestamp()


class Environment(EnvironmentNamingInfo, EnvironmentSummary):
    """Represents an isolated environment.

    Environments are isolated workspaces that hold pointers to physical tables.

    Args:
        snapshots: The snapshots that are part of this environment.
        promoted_snapshot_ids: The IDs of the snapshots that are promoted in this environment
            (i.e. for which the views are created). If not specified, all snapshots are promoted.
        previous_finalized_snapshots: Snapshots that were part of this environment last time it was finalized.
        requirements: A mapping of library versions for all the snapshots in this environment.
    """

    snapshots_: t.List[t.Any] = Field(alias="snapshots")
    promoted_snapshot_ids_: t.Optional[t.List[t.Any]] = Field(
        default=None, alias="promoted_snapshot_ids"
    )
    previous_finalized_snapshots_: t.Optional[t.List[t.Any]] = Field(
        default=None, alias="previous_finalized_snapshots"
    )
    requirements: t.Dict[str, str] = {}

    @field_validator("snapshots_", "previous_finalized_snapshots_", mode="before")
    @classmethod
    def _load_snapshots(cls, v: str | t.List[t.Any] | None) -> t.List[t.Any] | None:
        if isinstance(v, str):
            return json.loads(v)
        if v and not isinstance(next(iter(v)), (dict, SnapshotTableInfo)):
            raise ValueError("Must be a list of SnapshotTableInfo dicts or objects")
        return v

    @field_validator("promoted_snapshot_ids_", mode="before")
    @classmethod
    def _load_snapshot_ids(cls, v: str | t.List[t.Any] | None) -> t.List[t.Any] | None:
        if isinstance(v, str):
            return json.loads(v)
        if v and not isinstance(next(iter(v)), (dict, SnapshotId)):
            raise ValueError("Must be a list of SnapshotId dicts or objects")
        return v

    @field_validator("requirements", mode="before")
    def _load_requirements(cls, v: t.Any) -> t.Any:
        if isinstance(v, str):
            v = json.loads(v)
        return v or {}

    @property
    def snapshots(self) -> t.List[SnapshotTableInfo]:
        return self._convert_list_to_models_and_store("snapshots_", SnapshotTableInfo) or []

    def snapshot_dicts(self) -> t.List[dict]:
        return self._convert_list_to_dicts(self.snapshots_)

    @property
    def promoted_snapshot_ids(self) -> t.Optional[t.List[SnapshotId]]:
        return self._convert_list_to_models_and_store("promoted_snapshot_ids_", SnapshotId)

    def promoted_snapshot_id_dicts(self) -> t.List[dict]:
        return self._convert_list_to_dicts(self.promoted_snapshot_ids_)

    @property
    def promoted_snapshots(self) -> t.List[SnapshotTableInfo]:
        if self.promoted_snapshot_ids is None:
            return self.snapshots

        promoted_snapshot_ids = set(self.promoted_snapshot_ids)
        return [s for s in self.snapshots if s.snapshot_id in promoted_snapshot_ids]

    @property
    def previous_finalized_snapshots(self) -> t.Optional[t.List[SnapshotTableInfo]]:
        return self._convert_list_to_models_and_store(
            "previous_finalized_snapshots_", SnapshotTableInfo
        )

    def previous_finalized_snapshot_dicts(self) -> t.List[dict]:
        return self._convert_list_to_dicts(self.previous_finalized_snapshots_)

    @property
    def finalized_or_current_snapshots(self) -> t.List[SnapshotTableInfo]:
        return (
            self.snapshots
            if self.finalized_ts
            else self.previous_finalized_snapshots or self.snapshots
        )

    @property
    def naming_info(self) -> EnvironmentNamingInfo:
        return EnvironmentNamingInfo(
            name=self.name,
            suffix_target=self.suffix_target,
            catalog_name_override=self.catalog_name_override,
            normalize_name=self.normalize_name,
            gateway_managed=self.gateway_managed,
        )

    @property
    def summary(self) -> EnvironmentSummary:
        return EnvironmentSummary(
            name=self.name,
            start_at=self.start_at,
            end_at=self.end_at,
            plan_id=self.plan_id,
            previous_plan_id=self.previous_plan_id,
            expiration_ts=self.expiration_ts,
            finalized_ts=self.finalized_ts,
        )

    def can_partially_promote(self, existing_environment: Environment) -> bool:
        """Returns True if the existing environment can be partially promoted to the current environment.

        Partial promotion means that we don't need to re-create views for snapshots that are already promoted in the
        target environment.
        """
        return (
            bool(existing_environment.finalized_ts)
            and not existing_environment.expired
            and existing_environment.gateway_managed == self.gateway_managed
            and existing_environment.name == c.PROD
        )

    def _convert_list_to_models_and_store(
        self, field: str, type_: t.Type[PydanticType]
    ) -> t.Optional[t.List[PydanticType]]:
        value = getattr(self, field)
        if value and not isinstance(value[0], type_):
            value = [type_.parse_obj(obj) for obj in value]
            setattr(self, field, value)
        return value

    def _convert_list_to_dicts(self, value: t.Optional[t.List[t.Any]]) -> t.List[dict]:
        if not value:
            return []
        return value if isinstance(value[0], dict) else [v.dict() for v in value]


class EnvironmentStatements(PydanticModel):
    before_all: t.List[str]
    after_all: t.List[str]
    python_env: t.Dict[str, Executable]
    jinja_macros: t.Optional[JinjaMacroRegistry] = None
    project: t.Optional[str] = None

    def render_before_all(
        self,
        dialect: str,
        default_catalog: t.Optional[str] = None,
        **render_kwargs: t.Any,
    ) -> t.List[str]:
        return self.render(RuntimeStage.BEFORE_ALL, dialect, default_catalog, **render_kwargs)

    def render_after_all(
        self,
        dialect: str,
        default_catalog: t.Optional[str] = None,
        **render_kwargs: t.Any,
    ) -> t.List[str]:
        return self.render(RuntimeStage.AFTER_ALL, dialect, default_catalog, **render_kwargs)

    def render(
        self,
        runtime_stage: RuntimeStage,
        dialect: str,
        default_catalog: t.Optional[str] = None,
        **render_kwargs: t.Any,
    ) -> t.List[str]:
        return render_statements(
            statements=getattr(self, runtime_stage.value),
            dialect=dialect,
            default_catalog=default_catalog,
            python_env=self.python_env,
            jinja_macros=self.jinja_macros,
            runtime_stage=runtime_stage,
            **render_kwargs,
        )


def execute_environment_statements(
    adapter: EngineAdapter,
    environment_statements: t.List[EnvironmentStatements],
    runtime_stage: RuntimeStage,
    environment_naming_info: EnvironmentNamingInfo,
    default_catalog: t.Optional[str] = None,
    snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
    start: t.Optional[TimeLike] = None,
    end: t.Optional[TimeLike] = None,
    execution_time: t.Optional[TimeLike] = None,
    selected_models: t.Optional[t.Set[str]] = None,
) -> None:
    try:
        rendered_expressions = [
            expr
            for statements in environment_statements
            for expr in statements.render(
                runtime_stage=runtime_stage,
                dialect=adapter.dialect,
                default_catalog=default_catalog,
                snapshots=snapshots,
                start=start,
                end=end,
                execution_time=execution_time,
                environment_naming_info=environment_naming_info,
                engine_adapter=adapter,
                selected_models=selected_models,
            )
        ]
    except Exception as e:
        raise SQLMeshError(
            f"An error occurred during rendering of the '{runtime_stage.value}' statements:\n\n{e}"
        )
    if rendered_expressions:
        with adapter.transaction():
            for expr in rendered_expressions:
                try:
                    adapter.execute(expr)
                except Exception as e:
                    raise SQLMeshError(
                        f"An error occurred during execution of the following '{runtime_stage.value}' statement:\n\n{expr}\n\n{e}"
                    )
