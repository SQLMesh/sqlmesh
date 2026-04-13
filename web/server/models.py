from __future__ import annotations

import enum
import pathlib
import typing as t

import pydantic
from pydantic import Field
from sqlglot import exp
from watchfiles import Change

from sqlmesh.core.context import Context
from sqlmesh.core.environment import Environment, EnvironmentNamingInfo
from sqlmesh.core.node import IntervalUnit, NodeType
from sqlmesh.core.plan.definition import Plan
from sqlmesh.core.snapshot.definition import (
    Snapshot,
    SnapshotChangeCategory,
    SnapshotId,
)
from sqlmesh.utils.date import TimeLike, now_timestamp
from sqlmesh.utils.pydantic import PydanticModel, ValidationInfo, field_validator, validation_data

SUPPORTED_EXTENSIONS = {".py", ".sql", ".yaml", ".yml", ".csv"}


class Mode(str, enum.Enum):
    IDE = "ide"  # Allow all modules
    DOCS = "docs"  # Only docs module
    CATALOG = "catalog"  # Only docs module
    PLAN = "plan"  # Allow plan


class EventName(str, enum.Enum):
    """An enumeration of possible SSE names."""

    PING = "ping"
    ERRORS = "errors"
    WARNINGS = "warnings"
    FILE = "file"
    FORMAT_FILE = "format-file"
    MODELS = "models"
    TESTS = "tests"
    PLAN_APPLY = "plan-apply"
    PLAN_OVERVIEW = "plan-overview"
    PLAN_CANCEL = "plan-cancel"


class Modules(str, enum.Enum):
    EDITOR = "editor"  # include ability to edit files and run queries
    FILES = "files"  # include projects files
    DATA_CATALOG = "data-catalog"  # include data-catalog
    PLANS = "plans"  # include ability to run/apply plans
    TESTS = "tests"  # include ability to run tests
    AUDITS = "audits"  # include ability to run audits
    ERRORS = "errors"  # include ability to see errors
    DATA = "data"  # include ability to query data
    LINEAGE = "lineage"  # include lineage


class ModelType(str, enum.Enum):
    PYTHON = "python"
    SQL = "sql"
    SEED = "seed"
    EXTERNAL = "external"
    SOURCE = "source"


class ArtifactType(str, enum.Enum):
    file = "file"
    directory = "directory"


class FileType(str, enum.Enum):
    """An enumeration of possible file types."""

    audit = "audit"
    macros = "macros"
    model = "model"
    tests = "tests"


class ApplyType(str, enum.Enum):
    """An enumeration of possible apply types."""

    virtual = "virtual"
    backfill = "backfill"


class Status(str, enum.Enum):
    """An enumeration of statuses."""

    INIT = "init"
    SUCCESS = "success"
    FAIL = "fail"


class PlanStage(str, enum.Enum):
    """An enumeration of plan apply stages."""

    validation = "validation"
    changes = "changes"
    backfills = "backfills"
    creation = "creation"
    restate = "restate"
    backfill = "backfill"
    promote = "promote"
    cancel = "cancel"


class File(PydanticModel):
    name: str
    path: str
    extension: str = ""
    content: t.Optional[str] = None
    model_config = pydantic.ConfigDict(validate_default=True)  # type: ignore

    @field_validator("extension", mode="before")
    def default_extension(cls, v: str, info: ValidationInfo) -> str:
        data = validation_data(info)
        if "name" in data:
            return pathlib.Path(data["name"]).suffix
        return v


class Directory(PydanticModel):
    name: str
    path: str
    directories: t.List[Directory] = []
    files: t.List[File] = []


class Meta(PydanticModel):
    version: str
    has_running_task: bool = False


class Reference(PydanticModel):
    name: str
    expression: str
    unique: bool


class ModelDetails(PydanticModel):
    owner: t.Optional[str] = None
    kind: t.Optional[str] = None
    batch_size: t.Optional[int] = None
    cron: t.Optional[str] = None
    stamp: t.Optional[TimeLike] = None
    start: t.Optional[TimeLike] = None
    retention: t.Optional[int] = None
    table_format: t.Optional[str] = None
    storage_format: t.Optional[str] = None
    time_column: t.Optional[str] = None
    tags: t.Optional[str] = None
    references: t.List[Reference] = []
    partitioned_by: t.Optional[str] = None
    clustered_by: t.Optional[str] = None
    lookback: t.Optional[int] = None
    cron_prev: t.Optional[TimeLike] = None
    cron_next: t.Optional[TimeLike] = None
    interval_unit: t.Optional[IntervalUnit] = None
    annotated: t.Optional[bool] = None


class Column(PydanticModel):
    name: str
    type: str
    description: t.Optional[str] = None


class Model(PydanticModel):
    name: str
    fqn: str
    path: t.Optional[str] = None
    full_path: t.Optional[str] = None
    """
    As opposed to path, which is relative to the project root, full_path is the absolute path to the model file.
    """
    dialect: str
    type: ModelType
    columns: t.List[Column]
    description: t.Optional[str] = None
    details: t.Optional[ModelDetails] = None
    sql: t.Optional[str] = None
    definition: t.Optional[str] = None
    default_catalog: t.Optional[str] = None
    hash: str


class ChangeDisplay(PydanticModel):
    name: str
    view_name: str
    node_type: NodeType = NodeType.MODEL
    parents: t.Set[str] = set()

    @staticmethod
    def get_view_name(
        snapshots: t.Dict[SnapshotId, Snapshot],
        snapshot_id: SnapshotId,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> str:
        return (
            snapshots[snapshot_id].display_name(environment_naming_info, default_catalog)
            if snapshot_id in snapshots
            else snapshot_id.name
        )

    @staticmethod
    def get_node_type(snapshots: t.Dict[SnapshotId, Snapshot], snapshot_id: SnapshotId) -> NodeType:
        return snapshots[snapshot_id].node_type


class ChangeDirect(ChangeDisplay):
    diff: str
    indirect: t.List[ChangeDisplay] = []
    direct: t.List[ChangeDisplay] = []
    change_category: t.Optional[SnapshotChangeCategory] = None


class ChangeIndirect(ChangeDisplay):
    pass


class ModelsDiff(PydanticModel):
    direct: t.List[ChangeDirect] = []
    indirect: t.List[ChangeIndirect] = []
    metadata: t.List[ChangeDisplay] = []

    @classmethod
    def get_modified_snapshots(
        cls,
        context: Context,
        plan: Plan,
    ) -> ModelsDiff:
        """Get the modified snapshots for a environment."""
        modified_snapshots = plan.context_diff.modified_snapshots.items()
        default_catalog = context.default_catalog
        environment_naming_info = plan.environment_naming_info

        def _get_parents(
            current: Snapshot, visited: t.Optional[t.Dict[str, t.Set[str]]] = None
        ) -> t.Set[str]:
            visited = visited or {current.name: {p.name for p in current.parents}}
            parents: t.Set[str] = set()
            for parent in current.parents:
                if parent.name not in plan.context_diff.modified_snapshots:
                    continue
                (snapshot, _) = plan.context_diff.modified_snapshots[parent.name]
                parents = (
                    parents
                    | {parent.name}
                    | visited.get(parent.name, _get_parents(snapshot, visited))
                )
            return parents

        metadata: t.List[ChangeDisplay] = []
        direct: t.List[ChangeDirect] = []
        indirect: t.List[ChangeIndirect] = []

        for name, (current, _) in modified_snapshots:
            if plan.context_diff.directly_modified(name):
                direct.append(
                    ChangeDirect(
                        name=name,
                        view_name=current.display_name(environment_naming_info, default_catalog),
                        node_type=current.node_type,
                        diff=plan.context_diff.text_diff(name),
                        change_category=current.change_category,
                        parents=_get_parents(current),
                    )
                )
            elif plan.context_diff.indirectly_modified(name):
                indirect.append(
                    ChangeIndirect(
                        name=name,
                        view_name=current.display_name(environment_naming_info, default_catalog),
                        node_type=current.node_type,
                        parents=_get_parents(current),
                    )
                )
            elif plan.context_diff.metadata_updated(name):
                metadata.append(
                    ChangeDisplay(
                        name=name,
                        view_name=current.display_name(environment_naming_info, default_catalog),
                        node_type=current.node_type,
                    )
                )

        for c in direct:
            c.indirect = [change for change in indirect if c.name in change.parents]
            c.direct = [change for change in direct if c.name in change.parents]

        return ModelsDiff(
            direct=direct,
            indirect=indirect,
            metadata=metadata,
        )


class Environments(PydanticModel):
    environments: t.Dict[str, Environment] = {}
    pinned_environments: t.Set[str] = set()
    default_target_environment: str = ""


class EvaluateInput(PydanticModel):
    model: str
    start: TimeLike
    end: TimeLike
    execution_time: TimeLike
    limit: int = 1000


class FetchdfInput(PydanticModel):
    sql: str
    limit: int = 1000


class RenderInput(PydanticModel):
    model: str
    start: t.Optional[TimeLike] = None
    end: t.Optional[TimeLike] = None
    execution_time: t.Optional[TimeLike] = None
    expand: t.Union[bool, t.Iterable[str]] = False
    pretty: bool = True
    dialect: t.Optional[str] = None


class PlanDates(PydanticModel):
    start: t.Optional[TimeLike] = None
    end: t.Optional[TimeLike] = None


class PlanOptions(PydanticModel):
    skip_tests: bool = False
    skip_backfill: bool = False
    no_gaps: bool = False
    forward_only: bool = False
    no_auto_categorization: bool = False
    include_unmodified: bool = False
    create_from: t.Optional[str] = None
    restate_models: t.Optional[str] = None
    auto_apply: bool = False

    @field_validator("restate_models")
    @classmethod
    def validate_restate_models(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return v.split(",")
        return v


class LineageColumn(PydanticModel):
    source: t.Optional[str] = None
    expression: t.Optional[str] = None
    models: t.Dict[str, t.Set[str]]


class Query(PydanticModel):
    sql: str


class ApiExceptionPayload(PydanticModel):
    timestamp: int
    message: str
    origin: str
    status: t.Optional[int] = None
    trigger: t.Optional[str] = None
    type: t.Optional[str] = None
    description: t.Optional[str] = None
    traceback: t.Optional[str] = None
    stack: t.Optional[t.List[str]] = None


class SchemaDiff(PydanticModel):
    source: str
    target: str
    source_schema: t.Dict[str, str]
    target_schema: t.Dict[str, str]
    added: t.Dict[str, str]
    removed: t.Dict[str, str]
    modified: t.Dict[str, str]

    @field_validator(
        "source_schema", "target_schema", "added", "removed", "modified", mode="before"
    )
    @classmethod
    def validate_schema(
        cls,
        v: t.Union[
            t.Dict[str, exp.DataType],
            t.List[t.Tuple[str, exp.DataType]],
            t.Dict[str, t.Tuple[exp.DataType, exp.DataType]],
            t.Dict[str, str],
        ],
        info: ValidationInfo,
    ) -> t.Dict[str, str]:
        if isinstance(v, dict):
            # Handle modified field which has tuples of (source_type, target_type)
            if info.field_name == "modified" and any(isinstance(val, tuple) for val in v.values()):
                return {
                    k: f"{str(val[0])} → {str(val[1])}"
                    for k, val in v.items()
                    if isinstance(val, tuple)
                    and isinstance(val[0], exp.DataType)
                    and isinstance(val[1], exp.DataType)
                }
            return {k: str(val) for k, val in v.items()}
        if isinstance(v, list):
            return {k: str(val) for k, val in v}
        return v


class ProcessedSampleData(PydanticModel):
    column_differences: t.List[t.Dict[str, t.Any]]
    source_only: t.List[t.Dict[str, t.Any]]
    target_only: t.List[t.Dict[str, t.Any]]


class RowDiff(PydanticModel):
    source: str
    target: str
    stats: t.Dict[str, float]
    sample: t.Dict[str, t.Any]
    joined_sample: t.Dict[str, t.Any]
    s_sample: t.Dict[str, t.Any]
    t_sample: t.Dict[str, t.Any]
    column_stats: t.Dict[str, t.Any]
    source_count: int
    target_count: int
    count_pct_change: float
    decimals: int
    processed_sample_data: t.Optional[ProcessedSampleData] = None


class TableDiff(PydanticModel):
    schema_diff: SchemaDiff
    row_diff: RowDiff
    on: t.List[t.List[str]]


class TestCase(PydanticModel):
    name: str
    path: str


class TestErrorOrFailure(TestCase):
    tb: str


class TestSkipped(TestCase):
    reason: str


class TestResult(PydanticModel):
    tests_run: int
    failures: t.List[TestErrorOrFailure]
    errors: t.List[TestErrorOrFailure]
    skipped: t.List[TestSkipped]
    successes: t.List[TestCase]


class ArtifactChange(PydanticModel):
    change: Change
    path: str
    type: t.Optional[ArtifactType] = None
    file: t.Optional[File] = None


class ReportTestsResult(PydanticModel):
    message: str


class ReportTestDetails(ReportTestsResult):
    details: str


class ReportTestsFailure(ReportTestsResult):
    total: int
    failures: int
    errors: int
    successful: int
    dialect: str
    details: t.List[ReportTestDetails]
    traceback: str


class BackfillDetails(ChangeDisplay):
    interval: t.List[str]
    batches: int


class BackfillTask(ChangeDisplay):
    completed: int
    total: int
    start: int
    end: t.Optional[int] = None
    interval: t.Optional[t.List[str]] = None


class TrackableMeta(PydanticModel):
    status: Status = Status.INIT
    start: int = Field(default_factory=now_timestamp)
    end: t.Optional[int] = None
    done: bool = False

    @property
    def duration(self) -> int | None:
        return self.end - self.start if self.start and self.end else None

    def dict(self, *args: t.Any, **kwargs: t.Any) -> t.Dict[str, t.Any]:
        data = super().dict(*args, **kwargs)
        data["duration"] = self.duration
        return data


class Trackable(PydanticModel):
    meta: TrackableMeta = Field(default_factory=TrackableMeta)

    def stop(self, success: bool = True) -> None:
        if success:
            self.meta.status = Status.SUCCESS
        else:
            self.meta.status = Status.FAIL

        self.meta.end = now_timestamp()
        self.meta.done = bool(self.meta.start and self.meta.end)

    def update(self, data: t.Dict[str, t.Any]) -> None:
        for k, v in data.items():
            setattr(self, k, v)


class PlanStageValidation(Trackable):
    pass


class PlanStageCancel(Trackable):
    pass


class PlanChanges(PydanticModel):
    # can't have a set of pydantic models: https://github.com/pydantic/pydantic/issues/1090
    added: t.Optional[t.List[ChangeDisplay]] = None
    removed: t.Optional[t.List[ChangeDisplay]] = None
    modified: t.Optional[ModelsDiff] = None


class PlanStageChanges(Trackable, PlanChanges):
    pass


class PlanStageBackfills(Trackable):
    models: t.Optional[t.List[BackfillDetails]] = None


class PlanStageCreation(Trackable):
    total_tasks: int
    num_tasks: int


class PlanStageRestate(Trackable):
    pass


class PlanStageBackfill(Trackable):
    queue: t.Set[str] = set()
    tasks: t.Dict[str, BackfillTask] = {}


class PlanStagePromote(Trackable):
    total_tasks: int
    num_tasks: int
    target_environment: str


class PlanStageTracker(Trackable, PlanDates):
    environment: t.Optional[str] = None
    plan_options: t.Optional[PlanOptions] = None

    def add_stage(self, stage: PlanStage, data: Trackable) -> None:
        setattr(self, stage, data)


class PlanOverviewStageTracker(PlanStageTracker):
    validation: t.Optional[PlanStageValidation] = None
    changes: t.Optional[PlanStageChanges] = None
    backfills: t.Optional[PlanStageBackfills] = None


class PlanApplyStageTracker(PlanStageTracker):
    creation: t.Optional[PlanStageCreation] = None
    restate: t.Optional[PlanStageRestate] = None
    backfill: t.Optional[PlanStageBackfill] = None
    promote: t.Optional[PlanStagePromote] = None


class PlanCancelStageTracker(PlanStageTracker):
    cancel: t.Optional[PlanStageCancel] = None


class FormatFileStatus(PydanticModel):
    path: str
    status: Status = Status.INIT
