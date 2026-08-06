from __future__ import annotations

import abc
import datetime
import typing as t
import unittest
import uuid
import logging
import textwrap
from humanize import metric, naturalsize
from itertools import zip_longest
from pathlib import Path
from hyperscript import h
from rich.console import Console as RichConsole
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree
from sqlglot import exp

from sqlmesh.core.schema_diff import TableAlterOperation
from sqlmesh.core.test.result import ModelTextTestResult
from sqlmesh.core.environment import EnvironmentNamingInfo, EnvironmentSummary
from sqlmesh.core.linter.rule import RuleViolation
from sqlmesh.core.model import Model
from sqlmesh.core.snapshot import (
    Snapshot,
    SnapshotChangeCategory,
    SnapshotId,
    SnapshotInfoLike,
)
from sqlmesh.core.snapshot.definition import Interval, Intervals, SnapshotTableInfo
from sqlmesh.core.snapshot.execution_tracker import QueryExecutionStats
from sqlmesh.core.test import ModelTest
from sqlmesh.utils import rich as srich
from sqlmesh.utils import Verbosity
from sqlmesh.utils.concurrency import NodeExecutionFailedError
from sqlmesh.utils.date import time_like_to_str, to_date, yesterday_ds, to_ds, make_inclusive
from sqlmesh.utils.errors import (
    PythonModelEvalError,
    NodeAuditsErrors,
    format_destructive_change_msg,
    format_additive_change_msg,
)
from sqlmesh.utils.rich import strip_ansi_codes

if t.TYPE_CHECKING:
    import ipywidgets as widgets

    from sqlglot import exp
    from sqlglot.dialects.dialect import DialectType
    from sqlmesh.core.context_diff import ContextDiff
    from sqlmesh.core.plan import Plan, EvaluatablePlan, PlanBuilder, SnapshotIntervals
    from sqlmesh.core.table_diff import TableDiff, RowDiff, SchemaDiff
    from sqlmesh.core.config.connection import ConnectionConfig
    from sqlmesh.core.state_sync import Versions

    LayoutWidget = t.TypeVar("LayoutWidget", bound=t.Union[widgets.VBox, widgets.HBox])


logger = logging.getLogger(__name__)


SNAPSHOT_CHANGE_CATEGORY_STR = {
    None: "Unknown",
    SnapshotChangeCategory.BREAKING: "Breaking",
    SnapshotChangeCategory.NON_BREAKING: "Non-breaking",
    SnapshotChangeCategory.FORWARD_ONLY: "Forward-only",
    SnapshotChangeCategory.INDIRECT_BREAKING: "Indirect Breaking",
    SnapshotChangeCategory.INDIRECT_NON_BREAKING: "Indirect Non-breaking",
    SnapshotChangeCategory.METADATA: "Metadata",
}

PROGRESS_BAR_WIDTH = 40
LINE_WRAP_WIDTH = 100


class LinterConsole(abc.ABC):
    """Console for displaying linter violations"""

    @abc.abstractmethod
    def show_linter_violations(
        self, violations: t.List[RuleViolation], model: Model, is_error: bool = False
    ) -> None:
        """Prints all linter violations depending on their severity"""


class StateExporterConsole(abc.ABC):
    """Console for describing a state export"""

    @abc.abstractmethod
    def start_state_export(
        self,
        output_file: Path,
        gateway: t.Optional[str] = None,
        state_connection_config: t.Optional[ConnectionConfig] = None,
        environment_names: t.Optional[t.List[str]] = None,
        local_only: bool = False,
        confirm: bool = True,
    ) -> bool:
        """State a state export"""

    @abc.abstractmethod
    def update_state_export_progress(
        self,
        version_count: t.Optional[int] = None,
        versions_complete: bool = False,
        snapshot_count: t.Optional[int] = None,
        snapshots_complete: bool = False,
        environment_count: t.Optional[int] = None,
        environments_complete: bool = False,
    ) -> None:
        """Update the state export progress"""

    @abc.abstractmethod
    def stop_state_export(self, success: bool, output_file: Path) -> None:
        """Finish a state export"""


class StateImporterConsole(abc.ABC):
    """Console for describing a state import"""

    @abc.abstractmethod
    def start_state_import(
        self,
        input_file: Path,
        gateway: str,
        state_connection_config: ConnectionConfig,
        clear: bool = False,
        confirm: bool = True,
    ) -> bool:
        """Start a state import"""

    @abc.abstractmethod
    def update_state_import_progress(
        self,
        timestamp: t.Optional[str] = None,
        state_file_version: t.Optional[int] = None,
        versions: t.Optional[Versions] = None,
        snapshot_count: t.Optional[int] = None,
        snapshots_complete: bool = False,
        environment_count: t.Optional[int] = None,
        environments_complete: bool = False,
    ) -> None:
        """Update the state import process"""

    @abc.abstractmethod
    def stop_state_import(self, success: bool, input_file: Path) -> None:
        """Finish a state import"""


class JanitorConsole(abc.ABC):
    """Console for describing a janitor / snapshot cleanup run"""

    @abc.abstractmethod
    def start_cleanup(self, ignore_ttl: bool) -> bool:
        """Start a janitor / snapshot cleanup run.

        Args:
            ignore_ttl: Indicates that the user wants to ignore the snapshot TTL and clean up everything not promoted to an environment

        Returns:
            Whether or not the cleanup run should proceed
        """

    @abc.abstractmethod
    def update_cleanup_progress(self, object_name: str) -> None:
        """Update the snapshot cleanup progress."""

    @abc.abstractmethod
    def stop_cleanup(self, success: bool = True) -> None:
        """Indicates the janitor / snapshot cleanup run has ended

        Args:
            success: Whether or not the cleanup completed successfully
        """


class DestroyConsole(abc.ABC):
    """Console for describing a destroy operation"""

    @abc.abstractmethod
    def start_destroy(
        self,
        schemas_to_delete: t.Optional[t.Set[str]] = None,
        views_to_delete: t.Optional[t.Set[str]] = None,
        tables_to_delete: t.Optional[t.Set[str]] = None,
    ) -> bool:
        """Start a destroy operation.

        Args:
            schemas_to_delete: Set of schemas that will be deleted
            views_to_delete: Set of views that will be deleted
            tables_to_delete: Set of tables that will be deleted

        Returns:
            Whether or not the destroy operation should proceed
        """

    @abc.abstractmethod
    def stop_destroy(self, success: bool = True) -> None:
        """Indicates the destroy operation has ended

        Args:
            success: Whether or not the cleanup completed successfully
        """


class EnvironmentsConsole(abc.ABC):
    """Console for displaying environments"""

    @abc.abstractmethod
    def print_environments(self, environments_summary: t.List[EnvironmentSummary]) -> None:
        """Prints all environment names along with expiry datetime."""

    @abc.abstractmethod
    def show_intervals(self, snapshot_intervals: t.Dict[Snapshot, SnapshotIntervals]) -> None:
        """Show ready intervals"""


class DifferenceConsole(abc.ABC):
    """Console for displaying environment differences"""

    @abc.abstractmethod
    def show_environment_difference_summary(
        self,
        context_diff: ContextDiff,
        no_diff: bool = True,
    ) -> None:
        """Displays a summary of differences for the environment."""

    @abc.abstractmethod
    def show_model_difference_summary(
        self,
        context_diff: ContextDiff,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        no_diff: bool = True,
    ) -> None:
        """Displays a summary of differences for the given models."""


class TableDiffConsole(abc.ABC):
    """Console for displaying table differences"""

    @abc.abstractmethod
    def show_table_diff(
        self,
        table_diffs: t.List[TableDiff],
        show_sample: bool = True,
        skip_grain_check: bool = False,
        temp_schema: t.Optional[str] = None,
    ) -> None:
        """Display the table diff between two or multiple tables."""

    @abc.abstractmethod
    def update_table_diff_progress(self, model: str) -> None:
        """Update table diff progress bar"""

    @abc.abstractmethod
    def start_table_diff_progress(self, models_to_diff: int) -> None:
        """Start table diff progress bar"""

    @abc.abstractmethod
    def start_table_diff_model_progress(self, model: str) -> None:
        """Start table diff model progress"""

    @abc.abstractmethod
    def stop_table_diff_progress(self, success: bool) -> None:
        """Stop table diff progress bar"""

    @abc.abstractmethod
    def show_table_diff_details(
        self,
        models_to_diff: t.List[str],
    ) -> None:
        """Display information about which tables are going to be diffed"""

    @abc.abstractmethod
    def show_table_diff_summary(self, table_diff: TableDiff) -> None:
        """Display information about the tables being diffed and how they are being joined"""

    @abc.abstractmethod
    def show_schema_diff(self, schema_diff: SchemaDiff) -> None:
        """Show table schema diff."""

    @abc.abstractmethod
    def show_row_diff(
        self, row_diff: RowDiff, show_sample: bool = True, skip_grain_check: bool = False
    ) -> None:
        """Show table summary diff."""


class BaseConsole(abc.ABC):
    @abc.abstractmethod
    def log_error(self, message: str, *args: t.Any, **kwargs: t.Any) -> None:
        """Display error info to the user."""

    @abc.abstractmethod
    def log_warning(
        self,
        short_message: str,
        long_message: t.Optional[str] = None,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> None:
        """Display warning info to the user.

        Args:
            short_message: The warning message to print to console.
            long_message: The warning message to log to file. If not provided, `short_message` is used.
        """

    @abc.abstractmethod
    def log_success(self, message: str) -> None:
        """Display a general successful message to the user."""


class PlanBuilderConsole(BaseConsole, abc.ABC):
    @abc.abstractmethod
    def log_destructive_change(
        self,
        snapshot_name: str,
        alter_operations: t.List[TableAlterOperation],
        dialect: str,
        error: bool = True,
    ) -> None:
        """Display a destructive change error or warning to the user."""

    @abc.abstractmethod
    def log_additive_change(
        self,
        snapshot_name: str,
        alter_operations: t.List[TableAlterOperation],
        dialect: str,
        error: bool = True,
    ) -> None:
        """Display an additive change error or warning to the user."""


class UnitTestConsole(abc.ABC):
    @abc.abstractmethod
    def log_test_results(self, result: ModelTextTestResult, target_dialect: str) -> None:
        """Display the test result and output.

        Args:
            result: The unittest test result that contains metrics like num success, fails, ect.
            target_dialect: The dialect that tests were run against. Assumes all tests run against the same dialect.
        """


class SignalConsole(abc.ABC):
    @abc.abstractmethod
    def start_signal_progress(
        self,
        snapshot: Snapshot,
        default_catalog: t.Optional[str],
        environment_naming_info: EnvironmentNamingInfo,
    ) -> None:
        """Indicates that signal checking has begun for a snapshot."""

    @abc.abstractmethod
    def update_signal_progress(
        self,
        snapshot: Snapshot,
        signal_name: str,
        signal_idx: int,
        total_signals: int,
        ready_intervals: Intervals,
        check_intervals: Intervals,
        duration: float,
    ) -> None:
        """Updates the signal checking progress."""

    @abc.abstractmethod
    def stop_signal_progress(self) -> None:
        """Indicates that signal checking has completed for a snapshot."""


class Console(
    SignalConsole,
    PlanBuilderConsole,
    LinterConsole,
    StateExporterConsole,
    StateImporterConsole,
    JanitorConsole,
    DestroyConsole,
    EnvironmentsConsole,
    DifferenceConsole,
    TableDiffConsole,
    BaseConsole,
    UnitTestConsole,
    abc.ABC,
):
    """Abstract base class for defining classes used for displaying information to the user and also interact
    with them when their input is needed."""

    INDIRECTLY_MODIFIED_DISPLAY_THRESHOLD = 10

    @abc.abstractmethod
    def start_plan_evaluation(self, plan: EvaluatablePlan) -> None:
        """Indicates that a new evaluation has begun."""

    @abc.abstractmethod
    def stop_plan_evaluation(self) -> None:
        """Indicates that the evaluation has ended."""

    @abc.abstractmethod
    def start_evaluation_progress(
        self,
        batched_intervals: t.Dict[Snapshot, Intervals],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        audit_only: bool = False,
    ) -> None:
        """Indicates that a new snapshot evaluation/auditing progress has begun."""

    @abc.abstractmethod
    def start_snapshot_evaluation_progress(
        self, snapshot: Snapshot, audit_only: bool = False
    ) -> None:
        """Starts the snapshot evaluation progress."""

    @abc.abstractmethod
    def update_snapshot_evaluation_progress(
        self,
        snapshot: Snapshot,
        interval: Interval,
        batch_idx: int,
        duration_ms: t.Optional[int],
        num_audits_passed: int,
        num_audits_failed: int,
        audit_only: bool = False,
        execution_stats: t.Optional[QueryExecutionStats] = None,
        auto_restatement_triggers: t.Optional[t.List[SnapshotId]] = None,
    ) -> None:
        """Updates the snapshot evaluation progress."""

    @abc.abstractmethod
    def stop_evaluation_progress(self, success: bool = True) -> None:
        """Stops the snapshot evaluation progress."""

    @abc.abstractmethod
    def start_creation_progress(
        self,
        snapshots: t.List[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        """Indicates that a new snapshot creation progress has begun."""

    @abc.abstractmethod
    def update_creation_progress(self, snapshot: SnapshotInfoLike) -> None:
        """Update the snapshot creation progress."""

    @abc.abstractmethod
    def stop_creation_progress(self, success: bool = True) -> None:
        """Stop the snapshot creation progress."""

    @abc.abstractmethod
    def start_promotion_progress(
        self,
        snapshots: t.List[SnapshotTableInfo],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        """Indicates that a new snapshot promotion progress has begun."""

    @abc.abstractmethod
    def update_promotion_progress(self, snapshot: SnapshotInfoLike, promoted: bool) -> None:
        """Update the snapshot promotion progress."""

    @abc.abstractmethod
    def stop_promotion_progress(self, success: bool = True) -> None:
        """Stop the snapshot promotion progress."""

    @abc.abstractmethod
    def start_snapshot_migration_progress(self, total_tasks: int) -> None:
        """Indicates that a new snapshot migration progress has begun."""

    @abc.abstractmethod
    def update_snapshot_migration_progress(self, num_tasks: int) -> None:
        """Update the snapshot migration progress."""

    @abc.abstractmethod
    def log_migration_status(self, success: bool = True) -> None:
        """Log the finished migration status."""

    @abc.abstractmethod
    def stop_snapshot_migration_progress(self, success: bool = True) -> None:
        """Stop the snapshot migration progress."""

    @abc.abstractmethod
    def start_env_migration_progress(self, total_tasks: int) -> None:
        """Indicates that a new environment migration progress has begun."""

    @abc.abstractmethod
    def update_env_migration_progress(self, num_tasks: int) -> None:
        """Update the environment migration progress."""

    @abc.abstractmethod
    def stop_env_migration_progress(self, success: bool = True) -> None:
        """Stop the environment migration progress."""

    @abc.abstractmethod
    def plan(
        self,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        default_catalog: t.Optional[str],
        no_diff: bool = False,
        no_prompts: bool = False,
    ) -> None:
        """The main plan flow.

        The console should present the user with choices on how to backfill and version the snapshots
        of a plan.

        Args:
            plan: The plan to make choices for.
            auto_apply: Whether to automatically apply the plan after all choices have been made.
            no_diff: Hide text differences for changed models.
            no_prompts: Whether to disable interactive prompts for the backfill time range. Please note that
                if this flag is set to true and there are uncategorized changes the plan creation will
                fail. Default: False
        """

    @abc.abstractmethod
    def show_sql(self, sql: str) -> None:
        """Display to the user SQL."""

    @abc.abstractmethod
    def log_status_update(self, message: str) -> None:
        """Display general status update to the user."""

    @abc.abstractmethod
    def log_skipped_models(self, snapshot_names: t.Set[str]) -> None:
        """Display list of models skipped during evaluation to the user."""

    @abc.abstractmethod
    def log_failed_models(self, errors: t.List[NodeExecutionFailedError]) -> None:
        """Display list of models that failed during evaluation to the user."""

    @abc.abstractmethod
    def log_models_updated_during_restatement(
        self,
        snapshots: t.List[t.Tuple[SnapshotTableInfo, SnapshotTableInfo]],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        """Display a list of models where new versions got deployed to the specified :environment while we were restating data the old versions

        Args:
            snapshots: a list of (snapshot_we_restated, snapshot_it_got_replaced_with_during_restatement) tuples
            environment: which environment got updated while we were restating models
            environment_naming_info: how snapshots are named in that :environment (for display name purposes)
            default_catalog: the configured default catalog (for display name purposes)
        """

    @abc.abstractmethod
    def loading_start(self, message: t.Optional[str] = None) -> uuid.UUID:
        """Starts loading and returns a unique ID that can be used to stop the loading. Optionally can display a message."""

    @abc.abstractmethod
    def loading_stop(self, id: uuid.UUID) -> None:
        """Stop loading for the given id."""


class NoopConsole(Console):
    def start_plan_evaluation(self, plan: EvaluatablePlan) -> None:
        pass

    def stop_plan_evaluation(self) -> None:
        pass

    def start_evaluation_progress(
        self,
        batched_intervals: t.Dict[Snapshot, Intervals],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        audit_only: bool = False,
    ) -> None:
        pass

    def start_snapshot_evaluation_progress(
        self, snapshot: Snapshot, audit_only: bool = False
    ) -> None:
        pass

    def update_snapshot_evaluation_progress(
        self,
        snapshot: Snapshot,
        interval: Interval,
        batch_idx: int,
        duration_ms: t.Optional[int],
        num_audits_passed: int,
        num_audits_failed: int,
        audit_only: bool = False,
        execution_stats: t.Optional[QueryExecutionStats] = None,
        auto_restatement_triggers: t.Optional[t.List[SnapshotId]] = None,
    ) -> None:
        pass

    def stop_evaluation_progress(self, success: bool = True) -> None:
        pass

    def start_signal_progress(
        self,
        snapshot: Snapshot,
        default_catalog: t.Optional[str],
        environment_naming_info: EnvironmentNamingInfo,
    ) -> None:
        pass

    def update_signal_progress(
        self,
        snapshot: Snapshot,
        signal_name: str,
        signal_idx: int,
        total_signals: int,
        ready_intervals: Intervals,
        check_intervals: Intervals,
        duration: float,
    ) -> None:
        pass

    def stop_signal_progress(self) -> None:
        pass

    def start_creation_progress(
        self,
        snapshots: t.List[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        pass

    def update_creation_progress(self, snapshot: SnapshotInfoLike) -> None:
        pass

    def stop_creation_progress(self, success: bool = True) -> None:
        pass

    def start_cleanup(self, ignore_ttl: bool) -> bool:
        return True

    def update_cleanup_progress(self, object_name: str) -> None:
        pass

    def stop_cleanup(self, success: bool = True) -> None:
        pass

    def start_promotion_progress(
        self,
        snapshots: t.List[SnapshotTableInfo],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        pass

    def update_promotion_progress(self, snapshot: SnapshotInfoLike, promoted: bool) -> None:
        pass

    def stop_promotion_progress(self, success: bool = True) -> None:
        pass

    def start_snapshot_migration_progress(self, total_tasks: int) -> None:
        pass

    def update_snapshot_migration_progress(self, num_tasks: int) -> None:
        pass

    def log_migration_status(self, success: bool = True) -> None:
        pass

    def stop_snapshot_migration_progress(self, success: bool = True) -> None:
        pass

    def start_env_migration_progress(self, total_tasks: int) -> None:
        pass

    def update_env_migration_progress(self, num_tasks: int) -> None:
        pass

    def stop_env_migration_progress(self, success: bool = True) -> None:
        pass

    def start_state_export(
        self,
        output_file: Path,
        gateway: t.Optional[str] = None,
        state_connection_config: t.Optional[ConnectionConfig] = None,
        environment_names: t.Optional[t.List[str]] = None,
        local_only: bool = False,
        confirm: bool = True,
    ) -> bool:
        return confirm

    def update_state_export_progress(
        self,
        version_count: t.Optional[int] = None,
        versions_complete: bool = False,
        snapshot_count: t.Optional[int] = None,
        snapshots_complete: bool = False,
        environment_count: t.Optional[int] = None,
        environments_complete: bool = False,
    ) -> None:
        pass

    def stop_state_export(self, success: bool, output_file: Path) -> None:
        pass

    def start_state_import(
        self,
        input_file: Path,
        gateway: str,
        state_connection_config: ConnectionConfig,
        clear: bool = False,
        confirm: bool = True,
    ) -> bool:
        return confirm

    def update_state_import_progress(
        self,
        timestamp: t.Optional[str] = None,
        state_file_version: t.Optional[int] = None,
        versions: t.Optional[Versions] = None,
        snapshot_count: t.Optional[int] = None,
        snapshots_complete: bool = False,
        environment_count: t.Optional[int] = None,
        environments_complete: bool = False,
    ) -> None:
        pass

    def stop_state_import(self, success: bool, input_file: Path) -> None:
        pass

    def show_environment_difference_summary(
        self,
        context_diff: ContextDiff,
        no_diff: bool = True,
    ) -> None:
        pass

    def show_model_difference_summary(
        self,
        context_diff: ContextDiff,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        no_diff: bool = True,
    ) -> None:
        pass

    def plan(
        self,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        default_catalog: t.Optional[str],
        no_diff: bool = False,
        no_prompts: bool = False,
    ) -> None:
        if auto_apply:
            plan_builder.apply()

    def log_test_results(self, result: ModelTextTestResult, target_dialect: str) -> None:
        pass

    def show_sql(self, sql: str) -> None:
        pass

    def log_status_update(self, message: str) -> None:
        pass

    def log_skipped_models(self, snapshot_names: t.Set[str]) -> None:
        pass

    def log_failed_models(self, errors: t.List[NodeExecutionFailedError]) -> None:
        pass

    def log_models_updated_during_restatement(
        self,
        snapshots: t.List[t.Tuple[SnapshotTableInfo, SnapshotTableInfo]],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        pass

    def log_destructive_change(
        self,
        snapshot_name: str,
        alter_operations: t.List[TableAlterOperation],
        dialect: str,
        error: bool = True,
    ) -> None:
        pass

    def log_additive_change(
        self,
        snapshot_name: str,
        alter_operations: t.List[TableAlterOperation],
        dialect: str,
        error: bool = True,
    ) -> None:
        pass

    def log_error(self, message: str) -> None:
        pass

    def log_warning(self, short_message: str, long_message: t.Optional[str] = None) -> None:
        logger.warning(long_message or short_message)

    def log_success(self, message: str) -> None:
        pass

    def loading_start(self, message: t.Optional[str] = None) -> uuid.UUID:
        return uuid.uuid4()

    def loading_stop(self, id: uuid.UUID) -> None:
        pass

    def show_table_diff(
        self,
        table_diffs: t.List[TableDiff],
        show_sample: bool = True,
        skip_grain_check: bool = False,
        temp_schema: t.Optional[str] = None,
    ) -> None:
        for table_diff in table_diffs:
            self.show_table_diff_summary(table_diff)
            self.show_schema_diff(table_diff.schema_diff())
            self.show_row_diff(
                table_diff.row_diff(temp_schema=temp_schema, skip_grain_check=skip_grain_check),
                show_sample=show_sample,
                skip_grain_check=skip_grain_check,
            )

    def update_table_diff_progress(self, model: str) -> None:
        pass

    def start_table_diff_progress(self, models_to_diff: int) -> None:
        pass

    def start_table_diff_model_progress(self, model: str) -> None:
        pass

    def stop_table_diff_progress(self, success: bool) -> None:
        pass

    def show_table_diff_details(
        self,
        models_to_diff: t.List[str],
    ) -> None:
        pass

    def show_table_diff_summary(self, table_diff: TableDiff) -> None:
        pass

    def show_schema_diff(self, schema_diff: SchemaDiff) -> None:
        pass

    def show_row_diff(
        self, row_diff: RowDiff, show_sample: bool = True, skip_grain_check: bool = False
    ) -> None:
        pass

    def print_environments(self, environments_summary: t.List[EnvironmentSummary]) -> None:
        pass

    def show_intervals(self, snapshot_intervals: t.Dict[Snapshot, SnapshotIntervals]) -> None:
        pass

    def show_linter_violations(
        self, violations: t.List[RuleViolation], model: Model, is_error: bool = False
    ) -> None:
        pass

    def print_connection_config(
        self, config: ConnectionConfig, title: t.Optional[str] = "Connection"
    ) -> None:
        pass

    def start_destroy(
        self,
        schemas_to_delete: t.Optional[t.Set[str]] = None,
        views_to_delete: t.Optional[t.Set[str]] = None,
        tables_to_delete: t.Optional[t.Set[str]] = None,
    ) -> bool:
        return True

    def stop_destroy(self, success: bool = True) -> None:
        pass


def make_progress_bar(
    message: str,
    console: t.Optional[RichConsole] = None,
    justify: t.Literal["default", "left", "center", "right", "full"] = "right",
) -> Progress:
    return Progress(
        TextColumn(f"[bold blue]{message}", justify=justify),
        BarColumn(bar_width=PROGRESS_BAR_WIDTH),
        "[progress.percentage]{task.percentage:>3.1f}%",
        "•",
        srich.BatchColumn(),
        "•",
        TimeElapsedColumn(),
        console=console,
    )


class TerminalConsole(Console):
    """A rich based implementation of the console."""

    TABLE_DIFF_SOURCE_BLUE = "#0248ff"
    TABLE_DIFF_TARGET_GREEN = "green"
    AUDIT_PASS_MARK = "\u2714"
    GREEN_AUDIT_PASS_MARK = f"[green]{AUDIT_PASS_MARK}[/green]"
    AUDIT_FAIL_MARK = "\u274c"
    AUDIT_PADDING = 0
    CHECK_MARK = f"{AUDIT_PASS_MARK} "

    def __init__(
        self,
        console: t.Optional[RichConsole] = None,
        verbosity: Verbosity = Verbosity.DEFAULT,
        dialect: DialectType = None,
        ignore_warnings: bool = False,
        **kwargs: t.Any,
    ) -> None:
        self.console: RichConsole = console or srich.console

        self.evaluation_progress_live: t.Optional[Live] = None
        self.evaluation_total_progress: t.Optional[Progress] = None
        self.evaluation_total_task: t.Optional[TaskID] = None
        self.evaluation_model_progress: t.Optional[Progress] = None
        self.evaluation_model_tasks: t.Dict[str, TaskID] = {}
        self.evaluation_model_batch_sizes: t.Dict[Snapshot, int] = {}
        self.evaluation_column_widths: t.Dict[str, int] = {}

        # Put in temporary values that are replaced when evaluating
        self.environment_naming_info = EnvironmentNamingInfo()
        self.default_catalog: t.Optional[str] = None

        self.creation_progress: t.Optional[Progress] = None
        self.creation_column_widths: t.Dict[str, int] = {}
        self.creation_task: t.Optional[TaskID] = None

        self.promotion_progress: t.Optional[Progress] = None
        self.promotion_column_widths: t.Dict[str, int] = {}
        self.promotion_task: t.Optional[TaskID] = None

        self.migration_progress: t.Optional[Progress] = None
        self.migration_task: t.Optional[TaskID] = None

        self.env_migration_progress: t.Optional[Progress] = None
        self.env_migration_task: t.Optional[TaskID] = None

        self.loading_status: t.Dict[uuid.UUID, Status] = {}

        self.state_export_progress: t.Optional[Progress] = None
        self.state_export_version_task: t.Optional[TaskID] = None
        self.state_export_snapshot_task: t.Optional[TaskID] = None
        self.state_export_environment_task: t.Optional[TaskID] = None

        self.state_import_progress: t.Optional[Progress] = None
        self.state_import_version_task: t.Optional[TaskID] = None
        self.state_import_snapshot_task: t.Optional[TaskID] = None
        self.state_import_environment_task: t.Optional[TaskID] = None

        self.table_diff_progress: t.Optional[Progress] = None
        self.table_diff_model_progress: t.Optional[Progress] = None
        self.table_diff_model_tasks: t.Dict[str, TaskID] = {}
        self.table_diff_progress_live: t.Optional[Live] = None

        self.signal_progress_logged = False
        self.signal_status_tree: t.Optional[Tree] = None

        self.verbosity = verbosity
        self.dialect = dialect
        self.ignore_warnings = ignore_warnings

    def _limit_model_names(self, tree: Tree, verbosity: Verbosity = Verbosity.DEFAULT) -> Tree:
        """Trim long indirectly modified model lists below threshold."""
        modified_length = len(tree.children)
        if (
            verbosity < Verbosity.VERY_VERBOSE
            and modified_length > self.INDIRECTLY_MODIFIED_DISPLAY_THRESHOLD
        ):
            tree.children = [
                tree.children[0],
                Tree(f".... {modified_length - 2} more ...."),
                tree.children[-1],
            ]
        return tree

    def _print(self, value: t.Any, **kwargs: t.Any) -> None:
        self.console.print(value, **kwargs)

    def _prompt(self, message: str, **kwargs: t.Any) -> t.Any:
        return Prompt.ask(message, console=self.console, **kwargs)

    def _confirm(self, message: str, **kwargs: t.Any) -> bool:
        return Confirm.ask(message, console=self.console, **kwargs)

    def start_plan_evaluation(self, plan: EvaluatablePlan) -> None:
        pass

    def stop_plan_evaluation(self) -> None:
        pass

    def start_evaluation_progress(
        self,
        batched_intervals: t.Dict[Snapshot, Intervals],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        audit_only: bool = False,
    ) -> None:
        """Indicates that a new snapshot evaluation/auditing progress has begun."""
        # Add a newline to separate signal checking from evaluation
        if self.signal_progress_logged:
            self._print("")

        if not self.evaluation_progress_live:
            self.evaluation_total_progress = make_progress_bar(
                "Executing model batches" if not audit_only else "Auditing models", self.console
            )

            self.evaluation_model_progress = Progress(
                TextColumn("{task.fields[view_name]}", justify="right"),
                SpinnerColumn(spinner_name="simpleDots"),
                console=self.console,
            )

            progress_table = Table.grid()
            progress_table.add_row(self.evaluation_total_progress)
            progress_table.add_row(self.evaluation_model_progress)

            self.evaluation_progress_live = Live(
                progress_table, console=self.console, refresh_per_second=10
            )
            self.evaluation_progress_live.start()

            batch_sizes = {
                snapshot: len(intervals) for snapshot, intervals in batched_intervals.items()
            }
            message = "Executing" if not audit_only else "Auditing"
            self.evaluation_total_task = self.evaluation_total_progress.add_task(
                f"{message} models...", total=sum(batch_sizes.values())
            )

            # determine column widths
            self.evaluation_column_widths["annotation"] = (
                _calculate_annotation_str_len(
                    batched_intervals, self.AUDIT_PADDING, len(" (123.4m rows, 123.4 KiB)")
                )
                + 3  # brackets and opening escape backslash
            )
            self.evaluation_column_widths["name"] = max(
                len(
                    snapshot.display_name(
                        environment_naming_info,
                        default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                        dialect=self.dialect,
                    )
                )
                for snapshot in batched_intervals
            )
            largest_batch_size = max(batch_sizes.values())
            self.evaluation_column_widths["batch"] = len(str(largest_batch_size)) * 2 + 3  # [X/X]
            self.evaluation_column_widths["duration"] = 8

            self.evaluation_model_batch_sizes = batch_sizes
            self.environment_naming_info = environment_naming_info
            self.default_catalog = default_catalog

    def start_snapshot_evaluation_progress(
        self, snapshot: Snapshot, audit_only: bool = False
    ) -> None:
        if self.evaluation_model_progress and snapshot.name not in self.evaluation_model_tasks:
            display_name = snapshot.display_name(
                self.environment_naming_info,
                self.default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                dialect=self.dialect,
            )
            self.evaluation_model_tasks[snapshot.name] = self.evaluation_model_progress.add_task(
                f"{'Evaluating' if not audit_only else 'Auditing'} {display_name}...",
                view_name=display_name,
                total=self.evaluation_model_batch_sizes[snapshot],
            )

    def update_snapshot_evaluation_progress(
        self,
        snapshot: Snapshot,
        interval: Interval,
        batch_idx: int,
        duration_ms: t.Optional[int],
        num_audits_passed: int,
        num_audits_failed: int,
        audit_only: bool = False,
        execution_stats: t.Optional[QueryExecutionStats] = None,
        auto_restatement_triggers: t.Optional[t.List[SnapshotId]] = None,
    ) -> None:
        """Update the snapshot evaluation progress."""
        if (
            self.evaluation_total_progress
            and self.evaluation_model_progress
            and self.evaluation_progress_live
        ):
            total_batches = self.evaluation_model_batch_sizes[snapshot]
            batch_num = str(batch_idx + 1).rjust(len(str(total_batches)))
            batch = f"[{batch_num}/{total_batches}]".ljust(self.evaluation_column_widths["batch"])

            if duration_ms:
                display_name = snapshot.display_name(
                    self.environment_naming_info,
                    self.default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                    dialect=self.dialect,
                ).ljust(self.evaluation_column_widths["name"])

                annotation = _create_evaluation_model_annotation(
                    snapshot, _format_evaluation_model_interval(snapshot, interval), execution_stats
                )
                audits_str = ""
                if num_audits_passed:
                    audits_str += f" {self.AUDIT_PASS_MARK}{num_audits_passed}"
                if num_audits_failed:
                    audits_str += f" {self.AUDIT_FAIL_MARK}{num_audits_failed}"
                audits_str = f", audits{audits_str}" if audits_str else ""
                annotation_len = self.evaluation_column_widths["annotation"]
                # don't adjust the annotation_len if we're using AUDIT_PADDING
                annotation = f"\\[{annotation + audits_str}]".ljust(
                    annotation_len - 1
                    if num_audits_failed and self.AUDIT_PADDING == 0
                    else annotation_len
                )

                duration = f"{(duration_ms / 1000.0):.2f}s".ljust(
                    self.evaluation_column_widths["duration"]
                )

                msg = f"{f'{batch} ' if not audit_only else ''}{display_name}   {annotation}   {duration}".replace(
                    self.AUDIT_PASS_MARK, self.GREEN_AUDIT_PASS_MARK
                )

                self.evaluation_progress_live.console.print(msg)

            self.evaluation_total_progress.update(
                self.evaluation_total_task or TaskID(0), refresh=True, advance=1
            )

            model_task_id = self.evaluation_model_tasks[snapshot.name]
            self.evaluation_model_progress.update(model_task_id, refresh=True, advance=1)
            if (
                self.evaluation_model_progress._tasks[model_task_id].completed >= total_batches
                or audit_only
            ):
                self.evaluation_model_progress.remove_task(model_task_id)

    def stop_evaluation_progress(self, success: bool = True) -> None:
        """Stop the snapshot evaluation progress."""
        if self.evaluation_progress_live:
            self.evaluation_progress_live.stop()
            if success:
                self.log_success(f"{self.CHECK_MARK}Model batches executed")

        self.evaluation_progress_live = None
        self.evaluation_total_progress = None
        self.evaluation_total_task = None
        self.evaluation_model_progress = None
        self.evaluation_model_tasks = {}
        self.evaluation_model_batch_sizes = {}
        self.evaluation_column_widths = {}
        self.environment_naming_info = EnvironmentNamingInfo()
        self.default_catalog = None

    def start_signal_progress(
        self,
        snapshot: Snapshot,
        default_catalog: t.Optional[str],
        environment_naming_info: EnvironmentNamingInfo,
    ) -> None:
        """Indicates that signal checking has begun for a snapshot."""
        display_name = snapshot.display_name(
            environment_naming_info,
            default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
            dialect=self.dialect,
        )
        self.signal_status_tree = Tree(f"Checking signals for {display_name}")

    def update_signal_progress(
        self,
        snapshot: Snapshot,
        signal_name: str,
        signal_idx: int,
        total_signals: int,
        ready_intervals: Intervals,
        check_intervals: Intervals,
        duration: float,
    ) -> None:
        """Updates the signal checking progress."""
        tree = Tree(f"[{signal_idx + 1}/{total_signals}] {signal_name} {duration:.2f}s")

        formatted_check_intervals = [_format_signal_interval(snapshot, i) for i in check_intervals]
        formatted_ready_intervals = [_format_signal_interval(snapshot, i) for i in ready_intervals]

        if not formatted_check_intervals:
            formatted_check_intervals = ["no intervals"]
        if not formatted_ready_intervals:
            formatted_ready_intervals = ["no intervals"]

        # Color coding to help detect partial interval ranges quickly
        if ready_intervals == check_intervals:
            msg = "All ready"
            color = "green"
        elif ready_intervals:
            msg = "Some ready"
            color = "yellow"
        else:
            msg = "None ready"
            color = "red"

        if self.verbosity < Verbosity.VERY_VERBOSE:
            num_check_intervals = len(formatted_check_intervals)
            if num_check_intervals > 3:
                formatted_check_intervals = formatted_check_intervals[:3]
                formatted_check_intervals.append(f"... and {num_check_intervals - 3} more")

            num_ready_intervals = len(formatted_ready_intervals)
            if num_ready_intervals > 3:
                formatted_ready_intervals = formatted_ready_intervals[:3]
                formatted_ready_intervals.append(f"... and {num_ready_intervals - 3} more")

            check = ", ".join(formatted_check_intervals)
            tree.add(f"Check: {check}")

            ready = ", ".join(formatted_ready_intervals)
            tree.add(f"[{color}]{msg}: {ready}[/{color}]")
        else:
            check_tree = Tree("Check")
            tree.add(check_tree)
            for interval in formatted_check_intervals:
                check_tree.add(interval)

            ready_tree = Tree(f"[{color}]{msg}[/{color}]")
            tree.add(ready_tree)
            for interval in formatted_ready_intervals:
                ready_tree.add(f"[{color}]{interval}[/{color}]")

        if self.signal_status_tree is not None:
            self.signal_status_tree.add(tree)

    def stop_signal_progress(self) -> None:
        """Indicates that signal checking has completed for a snapshot."""
        if self.signal_status_tree is not None:
            self._print(self.signal_status_tree)
            self.signal_status_tree = None
            self.signal_progress_logged = True

    def start_creation_progress(
        self,
        snapshots: t.List[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        """Indicates that a new creation progress has begun."""
        if self.creation_progress is None:
            self.creation_progress = make_progress_bar("Updating physical layer", self.console)

            self._print("")
            self.creation_progress.start()
            self.creation_task = self.creation_progress.add_task(
                "Updating physical layer...",
                total=len(snapshots),
            )

            # determine name column widths if we're printing name
            if self.verbosity >= Verbosity.VERBOSE:
                self.creation_column_widths["name"] = max(
                    len(
                        snapshot.display_name(
                            environment_naming_info,
                            default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                            dialect=self.dialect,
                        )
                    )
                    for snapshot in snapshots
                )

            self.environment_naming_info = environment_naming_info
            self.default_catalog = default_catalog

    def update_creation_progress(self, snapshot: SnapshotInfoLike) -> None:
        """Update the snapshot creation progress."""
        if self.creation_progress is not None and self.creation_task is not None:
            if self.verbosity >= Verbosity.VERBOSE:
                msg = snapshot.display_name(
                    self.environment_naming_info,
                    self.default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                    dialect=self.dialect,
                ).ljust(self.creation_column_widths["name"])
                self.creation_progress.live.console.print(msg + "  [green]created[/green]")
            self.creation_progress.update(self.creation_task, refresh=True, advance=1)

    def stop_creation_progress(self, success: bool = True) -> None:
        """Stop the snapshot creation progress."""
        self.creation_task = None
        if self.creation_progress is not None:
            self.creation_progress.stop()
            self.creation_progress = None
            if success:
                self.log_success(f"\n{self.CHECK_MARK}Physical layer updated")

        self.environment_naming_info = EnvironmentNamingInfo()
        self.default_catalog = None
        self.creation_column_widths = {}

    def start_cleanup(self, ignore_ttl: bool) -> bool:
        if ignore_ttl:
            self._print(
                "Are you sure you want to delete all snapshots that are not referenced in any environment?"
            )
            self._print(
                "Note that this may cause a race condition if there are any concurrently running plans."
            )
            self._print(
                "It may also confuse users who were expecting to be able to rollback changes in their development environments."
            )
            if not self._confirm("Proceed?"):
                self.log_error("Cleanup aborted")
                return False
        return True

    def update_cleanup_progress(self, object_name: str) -> None:
        """Update the snapshot cleanup progress."""
        self._print(f"Deleted object {object_name}")

    def stop_cleanup(self, success: bool = False) -> None:
        if success:
            self.log_success("Cleanup complete.")
        else:
            self.log_error("Cleanup failed!")

    def start_destroy(
        self,
        schemas_to_delete: t.Optional[t.Set[str]] = None,
        views_to_delete: t.Optional[t.Set[str]] = None,
        tables_to_delete: t.Optional[t.Set[str]] = None,
    ) -> bool:
        self.log_warning(
            "This will permanently delete all engine-managed objects, state tables and SQLMesh cache.\n"
            "The operation may disrupt any currently running or scheduled plans.\n"
        )

        if schemas_to_delete or views_to_delete or tables_to_delete:
            if schemas_to_delete:
                self.log_error("Schemas to be deleted:")
                for schema in sorted(schemas_to_delete):
                    self.log_error(f"  • {schema}")

            if views_to_delete:
                self.log_error("\nEnvironment views to be deleted:")
                for view in sorted(views_to_delete):
                    self.log_error(f"  • {view}")

            if tables_to_delete:
                self.log_error("\nSnapshot tables to be deleted:")
                for table in sorted(tables_to_delete):
                    self.log_error(f"  • {table}")

            self.log_error(
                "\nThis action will DELETE ALL the above resources managed by SQLMesh AND\n"
                "potentially external resources created by other tools in these schemas.\n"
            )

        if not self._confirm("Are you ABSOLUTELY SURE you want to proceed with deletion?"):
            self.log_error("Destroy operation cancelled.")
            return False
        return True

    def stop_destroy(self, success: bool = False) -> None:
        if success:
            self.log_success("Destroy completed successfully.")
        else:
            self.log_error("Destroy failed!")

    def start_promotion_progress(
        self,
        snapshots: t.List[SnapshotTableInfo],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        """Indicates that a new snapshot promotion progress has begun."""
        if snapshots and self.promotion_progress is None:
            self.promotion_progress = make_progress_bar(
                "Updating virtual layer ", self.console, justify="left"
            )

            snapshots_with_virtual_views = [
                s for s in snapshots if s.is_model and not s.is_symbolic
            ]
            self.promotion_progress.start()
            self.promotion_task = self.promotion_progress.add_task(
                f"Virtually updating {environment_naming_info.name}...",
                total=len(snapshots_with_virtual_views),
            )

            # determine name column widths if we're printing names
            if self.verbosity >= Verbosity.VERBOSE:
                self.promotion_column_widths["name"] = max(
                    len(
                        snapshot.display_name(
                            environment_naming_info,
                            default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                            dialect=self.dialect,
                        )
                    )
                    for snapshot in snapshots_with_virtual_views
                )

            self.environment_naming_info = environment_naming_info
            self.default_catalog = default_catalog

    def update_promotion_progress(self, snapshot: SnapshotInfoLike, promoted: bool) -> None:
        """Update the snapshot promotion progress."""
        if (
            self.promotion_progress is not None
            and self.promotion_task is not None
            and snapshot.is_model
            and not snapshot.is_symbolic
        ):
            if self.verbosity >= Verbosity.VERBOSE:
                display_name = snapshot.display_name(
                    self.environment_naming_info,
                    self.default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                    dialect=self.dialect,
                ).ljust(self.promotion_column_widths["name"])
                action_str = ""
                if promoted:
                    action_str = (
                        "[yellow]updated[/yellow]"
                        if snapshot.previous_version
                        else "[green]created[/green]"
                    )
                action_str = action_str or "[red]dropped[/red]"
                self.promotion_progress.live.console.print(f"{display_name}  {action_str}")
            self.promotion_progress.update(self.promotion_task, refresh=True, advance=1)

    def stop_promotion_progress(self, success: bool = True) -> None:
        """Stop the snapshot promotion progress."""
        self.promotion_task = None
        if self.promotion_progress is not None:
            self.promotion_progress.stop()
            self.promotion_progress = None
            if success:
                self.log_success(f"\n{self.CHECK_MARK}Virtual layer updated")

        self.environment_naming_info = EnvironmentNamingInfo()
        self.default_catalog = None
        self.promotion_column_widths = {}

    def start_snapshot_migration_progress(self, total_tasks: int) -> None:
        """Indicates that a new snapshot migration progress has begun."""
        if self.migration_progress is None:
            self.migration_progress = make_progress_bar("Migrating snapshots", self.console)

            self.migration_progress.start()
            self.migration_task = self.migration_progress.add_task(
                "Migrating snapshots...",
                total=total_tasks,
            )

    def update_snapshot_migration_progress(self, num_tasks: int) -> None:
        """Update the migration progress."""
        if self.migration_progress is not None and self.migration_task is not None:
            self.migration_progress.update(self.migration_task, refresh=True, advance=num_tasks)

    def log_migration_status(self, success: bool = True) -> None:
        """Log the migration status."""
        if self.migration_progress is not None:
            self.migration_progress = None
            if success:
                self.log_success("Migration completed successfully")

    def stop_snapshot_migration_progress(self, success: bool = True) -> None:
        """Stop the migration progress."""
        self.migration_task = None
        if self.migration_progress is not None:
            self.migration_progress.stop()
            if success:
                self.log_success("Snapshots migrated successfully")

    def start_env_migration_progress(self, total_tasks: int) -> None:
        """Indicates that a new environment migration has begun."""
        if self.env_migration_progress is None:
            self.env_migration_progress = make_progress_bar("Migrating environments", self.console)
            self.env_migration_progress.start()
            self.env_migration_task = self.env_migration_progress.add_task(
                "Migrating environments...",
                total=total_tasks,
            )

    def update_env_migration_progress(self, num_tasks: int) -> None:
        """Update the environment migration progress."""
        if self.env_migration_progress is not None and self.env_migration_task is not None:
            self.env_migration_progress.update(
                self.env_migration_task, refresh=True, advance=num_tasks
            )

    def stop_env_migration_progress(self, success: bool = True) -> None:
        """Stop the environment migration progress."""
        self.env_migration_task = None
        if self.env_migration_progress is not None:
            self.env_migration_progress.stop()
            self.env_migration_progress = None
            if success:
                self.log_success("Environments migrated successfully")

    def start_state_export(
        self,
        output_file: Path,
        gateway: t.Optional[str] = None,
        state_connection_config: t.Optional[ConnectionConfig] = None,
        environment_names: t.Optional[t.List[str]] = None,
        local_only: bool = False,
        confirm: bool = True,
    ) -> bool:
        self.state_export_progress = None

        if local_only:
            self.log_status_update(f"Exporting [b]local[/b] state to '{output_file.as_posix()}'\n")
            self.log_warning(
                "Local state exports just contain the model versions in your local context. Therefore, the resulting file cannot be imported."
            )
        else:
            self.log_status_update(
                f"Exporting state to '{output_file.as_posix()}' from the following connection:\n"
            )
            if gateway:
                self.log_status_update(f"[b]Gateway[/b]: [green]{gateway}[/green]")
            if state_connection_config:
                self.print_connection_config(state_connection_config, title="State Connection")
            if environment_names:
                heading = "Environments" if len(environment_names) > 1 else "Environment"
                self.log_status_update(
                    f"[b]{heading}[/b]: [yellow]{', '.join(environment_names)}[/yellow]"
                )

        should_continue = True
        if confirm:
            should_continue = self._confirm("\nContinue?")
            self.log_status_update("")

        if should_continue:
            self.state_export_progress = make_progress_bar("{task.description}", self.console)
            assert isinstance(self.state_export_progress, Progress)

            self.state_export_version_task = self.state_export_progress.add_task(
                "Exporting versions", start=False
            )
            self.state_export_snapshot_task = self.state_export_progress.add_task(
                "Exporting snapshots", start=False
            )
            self.state_export_environment_task = self.state_export_progress.add_task(
                "Exporting environments", start=False
            )

            self.state_export_progress.start()

        return should_continue

    def update_state_export_progress(
        self,
        version_count: t.Optional[int] = None,
        versions_complete: bool = False,
        snapshot_count: t.Optional[int] = None,
        snapshots_complete: bool = False,
        environment_count: t.Optional[int] = None,
        environments_complete: bool = False,
    ) -> None:
        if self.state_export_progress:
            if self.state_export_version_task is not None:
                if version_count is not None:
                    self.state_export_progress.start_task(self.state_export_version_task)
                    self.state_export_progress.update(
                        self.state_export_version_task,
                        total=version_count,
                        completed=version_count,
                        refresh=True,
                    )
                if versions_complete:
                    self.state_export_progress.stop_task(self.state_export_version_task)

            if self.state_export_snapshot_task is not None:
                if snapshot_count is not None:
                    self.state_export_progress.start_task(self.state_export_snapshot_task)
                    self.state_export_progress.update(
                        self.state_export_snapshot_task,
                        total=snapshot_count,
                        completed=snapshot_count,
                        refresh=True,
                    )
                if snapshots_complete:
                    self.state_export_progress.stop_task(self.state_export_snapshot_task)

            if self.state_export_environment_task is not None:
                if environment_count is not None:
                    self.state_export_progress.start_task(self.state_export_environment_task)
                    self.state_export_progress.update(
                        self.state_export_environment_task,
                        total=environment_count,
                        completed=environment_count,
                        refresh=True,
                    )
                if environments_complete:
                    self.state_export_progress.stop_task(self.state_export_environment_task)

    def stop_state_export(self, success: bool, output_file: Path) -> None:
        if self.state_export_progress:
            self.state_export_progress.stop()
            self.state_export_progress = None

            self.log_status_update("")

            if success:
                self.log_success(f"State exported successfully to '{output_file.as_posix()}'")
            else:
                self.log_error("State export failed!")

    def start_state_import(
        self,
        input_file: Path,
        gateway: str,
        state_connection_config: ConnectionConfig,
        clear: bool = False,
        confirm: bool = True,
    ) -> bool:
        self.log_status_update(
            f"Loading state from '{input_file.as_posix()}' into the following connection:\n"
        )
        self.log_status_update(f"[b]Gateway[/b]: [green]{gateway}[/green]")
        self.print_connection_config(state_connection_config, title="State Connection")
        self.log_status_update("")

        if clear:
            self.log_warning(
                f"This [b]destructive[/b] operation will delete all existing state against the '{gateway}' gateway \n"
                f"and replace it with what's in the '{input_file.as_posix()}' file.\n"
            )
        else:
            self.log_warning(
                f"This operation will [b]merge[/b] the contents of the state file to the state located at the '{gateway}' gateway.\n"
                "Matching snapshots or environments will be replaced.\n"
                "Non-matching snapshots or environments will be ignored.\n"
            )

        should_continue = True
        if confirm:
            should_continue = self._confirm("[red]Are you sure?[/red]")
            self.log_status_update("")

        if should_continue:
            self.state_import_progress = make_progress_bar("{task.description}", self.console)

            self.state_import_info = Tree("[bold]State File Information:")

            self.state_import_version_task = self.state_import_progress.add_task(
                "Importing versions", start=False
            )
            self.state_import_snapshot_task = self.state_import_progress.add_task(
                "Importing snapshots", start=False
            )
            self.state_import_environment_task = self.state_import_progress.add_task(
                "Importing environments", start=False
            )

            self.state_import_progress.start()

        return should_continue

    def update_state_import_progress(
        self,
        timestamp: t.Optional[str] = None,
        state_file_version: t.Optional[int] = None,
        versions: t.Optional[Versions] = None,
        snapshot_count: t.Optional[int] = None,
        snapshots_complete: bool = False,
        environment_count: t.Optional[int] = None,
        environments_complete: bool = False,
    ) -> None:
        if self.state_import_progress:
            if self.state_import_info:
                if timestamp:
                    self.state_import_info.add(f"Creation Timestamp: {timestamp}")
                if state_file_version:
                    self.state_import_info.add(f"File Version: {state_file_version}")
                if versions:
                    self.state_import_info.add(f"SQLMesh version: {versions.sqlmesh_version}")
                    self.state_import_info.add(
                        f"SQLMesh migration version: {versions.schema_version}"
                    )
                    self.state_import_info.add(f"SQLGlot version: {versions.sqlglot_version}\n")

                    self._print(self.state_import_info)

                    version_count = len(versions.model_dump())

                    if self.state_import_version_task is not None:
                        self.state_import_progress.start_task(self.state_import_version_task)
                        self.state_import_progress.update(
                            self.state_import_version_task,
                            total=version_count,
                            completed=version_count,
                        )
                        self.state_import_progress.stop_task(self.state_import_version_task)

            if self.state_import_snapshot_task is not None:
                if snapshot_count is not None:
                    self.state_import_progress.start_task(self.state_import_snapshot_task)
                    self.state_import_progress.update(
                        self.state_import_snapshot_task,
                        completed=snapshot_count,
                        total=snapshot_count,
                        refresh=True,
                    )

                if snapshots_complete:
                    self.state_import_progress.stop_task(self.state_import_snapshot_task)

            if self.state_import_environment_task is not None:
                if environment_count is not None:
                    self.state_import_progress.start_task(self.state_import_environment_task)
                    self.state_import_progress.update(
                        self.state_import_environment_task,
                        completed=environment_count,
                        total=environment_count,
                        refresh=True,
                    )

                if environments_complete:
                    self.state_import_progress.stop_task(self.state_import_environment_task)

    def stop_state_import(self, success: bool, input_file: Path) -> None:
        if self.state_import_progress:
            self.state_import_progress.stop()
            self.state_import_progress = None

            self.log_status_update("")

            if success:
                self.log_success(f"State imported successfully from '{input_file.as_posix()}'")
            else:
                self.log_error("State import failed!")

    def show_environment_difference_summary(
        self,
        context_diff: ContextDiff,
        no_diff: bool = True,
    ) -> None:
        """Shows a summary of the environment differences.

        Args:
            context_diff: The context diff to use to print the summary
            no_diff: Hide the actual environment statement differences.
        """
        if context_diff.is_new_environment:
            msg = (
                f"\n`{context_diff.environment}` environment will be initialized"
                if not context_diff.create_from_env_exists
                else f"\nNew environment `{context_diff.environment}` will be created from `{context_diff.create_from}`"
            )
            self._print(Tree(f"[bold]{msg}\n"))
            if not context_diff.has_snapshot_changes:
                return

        if not context_diff.has_changes:
            # This is only reached when the plan is against an existing environment, so we use the environment
            #   name instead of the create_from name. The equivalent message for new environments happens in
            #   the PlanBuilder.
            self._print(
                Tree(
                    f"\n[bold]No changes to plan: project files match the `{context_diff.environment}` environment\n"
                )
            )
            return

        if not context_diff.is_new_environment or (
            context_diff.is_new_environment and context_diff.create_from_env_exists
        ):
            self._print(
                Tree(
                    f"\n[bold]Differences from the `{context_diff.create_from if context_diff.is_new_environment else context_diff.environment}` environment:\n"
                )
            )

        if context_diff.has_requirement_changes:
            self._print(f"[bold]Requirements:\n{context_diff.requirements_diff()}")

        if context_diff.has_environment_statements_changes and not no_diff:
            self._print("[bold]Environment statements:\n")
            for type, diff in context_diff.environment_statements_diff(
                include_python_env=not context_diff.is_new_environment
            ):
                self._print(Syntax(diff, type, line_numbers=False))

    def show_model_difference_summary(
        self,
        context_diff: ContextDiff,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        no_diff: bool = True,
    ) -> None:
        """Shows a summary of the model differences.

        Args:
            context_diff: The context diff to use to print the summary
            environment_naming_info: The environment naming info to reference when printing model names
            default_catalog: The default catalog to reference when deciding to remove catalog from display names
            no_diff: Hide the actual SQL differences.
        """
        self._show_summary_tree_for(
            context_diff,
            "Models",
            lambda x: x.is_model,
            environment_naming_info,
            default_catalog,
            no_diff=no_diff,
        )
        self._show_summary_tree_for(
            context_diff,
            "Standalone Audits",
            lambda x: x.is_audit,
            environment_naming_info,
            default_catalog,
            no_diff=no_diff,
        )

    def plan(
        self,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        default_catalog: t.Optional[str],
        no_diff: bool = False,
        no_prompts: bool = False,
    ) -> None:
        """The main plan flow.

        The console should present the user with choices on how to backfill and version the snapshots
        of a plan.

        Args:
            plan: The plan to make choices for.
            auto_apply: Whether to automatically apply the plan after all choices have been made.
            default_catalog: The default catalog to reference when deciding to remove catalog from display names
            no_diff: Hide text differences for changed models.
            no_prompts: Whether to disable interactive prompts for the backfill time range. Please note that
                if this flag is set to true and there are uncategorized changes the plan creation will
                fail. Default: False
        """
        self._prompt_categorize(
            plan_builder,
            auto_apply,
            no_diff=no_diff,
            no_prompts=no_prompts,
            default_catalog=default_catalog,
        )

        self._show_options_after_categorization(
            plan_builder, auto_apply, default_catalog=default_catalog, no_prompts=no_prompts
        )

        if auto_apply:
            plan_builder.apply()

    def _show_summary_tree_for(
        self,
        context_diff: ContextDiff,
        header: str,
        snapshot_selector: t.Callable[[SnapshotInfoLike], bool],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        no_diff: bool = True,
    ) -> None:
        added_snapshot_ids = {
            s_id for s_id in context_diff.added if snapshot_selector(context_diff.snapshots[s_id])
        }
        removed_snapshot_ids = {
            s_id
            for s_id, snapshot in context_diff.removed_snapshots.items()
            if snapshot_selector(snapshot)
        }
        modified_snapshot_ids = {
            current_snapshot.snapshot_id
            for _, (current_snapshot, _) in context_diff.modified_snapshots.items()
            if snapshot_selector(current_snapshot)
        }

        tree_sets = (
            added_snapshot_ids,
            removed_snapshot_ids,
            modified_snapshot_ids,
        )
        if all(not s_ids for s_ids in tree_sets):
            return

        tree = Tree(f"[bold]{header}:")
        if added_snapshot_ids:
            added_tree = Tree("[bold][added]Added:")
            for s_id in sorted(added_snapshot_ids):
                snapshot = context_diff.snapshots[s_id]
                added_tree.add(
                    f"[added]{snapshot.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}"
                )
            tree.add(self._limit_model_names(added_tree, self.verbosity))
        if removed_snapshot_ids:
            removed_tree = Tree("[bold][removed]Removed:")
            for s_id in sorted(removed_snapshot_ids):
                snapshot_table_info = context_diff.removed_snapshots[s_id]
                removed_tree.add(
                    f"[removed]{snapshot_table_info.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}"
                )
            tree.add(self._limit_model_names(removed_tree, self.verbosity))
        if modified_snapshot_ids:
            tree = self._add_modified_models(
                context_diff,
                modified_snapshot_ids,
                tree,
                environment_naming_info,
                default_catalog,
                no_diff,
            )

        self._print(tree)

    def _add_modified_models(
        self,
        context_diff: ContextDiff,
        modified_snapshot_ids: t.Set[SnapshotId],
        tree: Tree,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str] = None,
        no_diff: bool = True,
    ) -> Tree:
        direct = Tree("[bold][direct]Directly Modified:")
        indirect = Tree("[bold][indirect]Indirectly Modified:")
        metadata = Tree("[bold][metadata]Metadata Updated:")
        for s_id in modified_snapshot_ids:
            name = s_id.name
            display_name = context_diff.snapshots[s_id].display_name(
                environment_naming_info,
                default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                dialect=self.dialect,
            )
            if context_diff.directly_modified(name):
                direct.add(
                    f"[direct]{display_name}"
                    if no_diff
                    else Syntax(f"{display_name}\n{context_diff.text_diff(name)}", "sql")
                )
            elif context_diff.indirectly_modified(name):
                indirect.add(f"[indirect]{display_name}")
            elif context_diff.metadata_updated(name):
                metadata.add(
                    f"[metadata]{display_name}"
                    if no_diff
                    else Syntax(f"{display_name}\n{context_diff.text_diff(name)}", "sql")
                )
        if direct.children:
            tree.add(direct)
        if indirect.children:
            tree.add(self._limit_model_names(indirect, self.verbosity))
        if metadata.children:
            tree.add(metadata)
        return tree

    def _show_options_after_categorization(
        self,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        default_catalog: t.Optional[str],
        no_prompts: bool,
    ) -> None:
        plan = plan_builder.build()
        if not no_prompts and plan.forward_only and plan.new_snapshots:
            self._prompt_effective_from(plan_builder, auto_apply, default_catalog)

        if plan.requires_backfill:
            self._show_missing_dates(plan_builder.build(), default_catalog)

            if not no_prompts:
                self._prompt_backfill(plan_builder, auto_apply, default_catalog)

            backfill_or_preview = "preview" if plan.is_dev and plan.forward_only else "backfill"
            if not auto_apply and self._confirm(
                f"Apply - {backfill_or_preview.capitalize()} Tables"
            ):
                plan_builder.apply()
        elif plan.has_changes and not auto_apply:
            self._prompt_promote(plan_builder)
        elif plan.has_unmodified_unpromoted and not auto_apply:
            self.log_status_update("\n[bold]Virtually updating unmodified models\n")
            self._prompt_promote(plan_builder)

    def _prompt_categorize(
        self,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        no_diff: bool,
        no_prompts: bool,
        default_catalog: t.Optional[str],
    ) -> None:
        """Get the user's change category for the directly modified models."""
        plan = plan_builder.build()

        if plan.restatements:
            # A plan can have restatements for the following reasons:
            # - The user specifically called `sqlmesh plan` with --restate-model.
            #   This creates a "restatement plan" which disallows all other changes and simply force-backfills
            #   the selected models and their downstream dependencies using the versions of the models stored in state.
            # - There are no specific restatements (so changes are allowed) AND dev previews need to be computed.
            #   The "restatements" feature is currently reused for dev previews.
            if plan.selected_models_to_restate:
                # There were legitimate restatements, no dev previews
                tree = Tree(
                    "[bold]Models selected for restatement:[/bold]\n"
                    "This causes backfill of the model itself as well as affected downstream models"
                )
                model_fqn_to_snapshot = {s.name: s for s in plan.snapshots.values()}
                for model_fqn in plan.selected_models_to_restate:
                    snapshot = model_fqn_to_snapshot[model_fqn]
                    display_name = snapshot.display_name(
                        plan.environment_naming_info,
                        default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                        dialect=self.dialect,
                    )
                    tree.add(
                        display_name
                    )  # note: we deliberately dont show any intervals here; they get shown in the backfill section
                self._print(tree)
            else:
                # We are computing dev previews, do not confuse the user by printing out something to do
                # with restatements. Dev previews are already highlighted in the backfill step
                pass
        else:
            self.show_environment_difference_summary(
                plan.context_diff,
                no_diff=no_diff,
            )

        if plan.context_diff.has_changes:
            self.show_model_difference_summary(
                plan.context_diff,
                plan.environment_naming_info,
                default_catalog=default_catalog,
            )

        if not no_diff:
            self._show_categorized_snapshots(plan, default_catalog)

        for snapshot in plan.uncategorized:
            if snapshot.is_model and snapshot.model.forward_only:
                continue
            if not no_diff:
                self.show_sql(plan.context_diff.text_diff(snapshot.name))
            tree = Tree(
                f"[bold][direct]Directly Modified: {snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}"
            )
            indirect_tree = None

            for child_sid in sorted(plan.indirectly_modified.get(snapshot.snapshot_id, set())):
                child_snapshot = plan.context_diff.snapshots[child_sid]
                if not indirect_tree:
                    indirect_tree = Tree("[indirect]Indirectly Modified Children:")
                    tree.add(indirect_tree)
                indirect_tree.add(
                    f"[indirect]{child_snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}"
                )
            if indirect_tree:
                indirect_tree = self._limit_model_names(indirect_tree, self.verbosity)

            self._print(tree)
            if not no_prompts:
                self._get_snapshot_change_category(
                    snapshot, plan_builder, auto_apply, default_catalog
                )

    def _show_categorized_snapshots(self, plan: Plan, default_catalog: t.Optional[str]) -> None:
        context_diff = plan.context_diff

        for snapshot in plan.categorized:
            if context_diff.directly_modified(snapshot.name):
                category_str = SNAPSHOT_CHANGE_CATEGORY_STR[snapshot.change_category]
                tree = Tree(
                    f"\n[bold][direct]Directly Modified: {snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)} ({category_str})"
                )
                indirect_tree = None
                for child_sid in sorted(plan.indirectly_modified.get(snapshot.snapshot_id, set())):
                    child_snapshot = context_diff.snapshots[child_sid]
                    if not indirect_tree:
                        indirect_tree = Tree("[indirect]Indirectly Modified Children:")
                        tree.add(indirect_tree)
                    child_category_str = SNAPSHOT_CHANGE_CATEGORY_STR[
                        child_snapshot.change_category
                    ]
                    indirect_tree.add(
                        f"[indirect]{child_snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)} ({child_category_str})"
                    )
                if indirect_tree:
                    indirect_tree = self._limit_model_names(indirect_tree, self.verbosity)
            elif context_diff.metadata_updated(snapshot.name):
                tree = Tree(
                    f"\n[bold][metadata]Metadata Updated: {snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}"
                )
            else:
                continue

            text_diff = context_diff.text_diff(snapshot.name)
            if text_diff:
                self._print("")
                self._print(Syntax(text_diff, "sql", word_wrap=True))
            self._print(tree)

    def _show_missing_dates(self, plan: Plan, default_catalog: t.Optional[str]) -> None:
        """Displays the models with missing dates."""
        missing_intervals = plan.missing_intervals
        if not missing_intervals:
            return
        backfill = Tree("[bold]Models needing backfill:[/bold]")
        for missing in missing_intervals:
            snapshot = plan.context_diff.snapshots[missing.snapshot_id]
            if not snapshot.is_model:
                continue

            preview_modifier = ""
            if not plan.deployability_index.is_deployable(snapshot):
                preview_modifier = " ([orange1]preview[/orange1])"

            display_name = snapshot.display_name(
                plan.environment_naming_info,
                default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                dialect=self.dialect,
            )
            backfill.add(
                f"{display_name}: \\[{_format_missing_intervals(snapshot, missing)}]{preview_modifier}"
            )

        if backfill:
            backfill = self._limit_model_names(backfill, self.verbosity)
        self._print(backfill)

    def _prompt_effective_from(
        self, plan_builder: PlanBuilder, auto_apply: bool, default_catalog: t.Optional[str]
    ) -> None:
        if not plan_builder.build().effective_from:
            effective_from = self._prompt(
                "Enter the effective date (eg. '1 year', '2020-01-01') to apply forward-only changes retroactively or blank to only apply them going forward once changes are deployed to prod"
            )
            if effective_from:
                plan_builder.set_effective_from(effective_from)

    def _prompt_backfill(
        self, plan_builder: PlanBuilder, auto_apply: bool, default_catalog: t.Optional[str]
    ) -> None:
        plan = plan_builder.build()
        is_forward_only_dev = plan.is_dev and plan.forward_only
        backfill_or_preview = "preview" if is_forward_only_dev else "backfill"

        if plan_builder.is_start_and_end_allowed:
            if not plan_builder.override_start:
                if is_forward_only_dev:
                    if plan.effective_from:
                        blank_meaning = f"to preview starting from the effective date ('{time_like_to_str(plan.effective_from)}')"
                        default_start = plan.effective_from
                    else:
                        blank_meaning = "to preview starting from yesterday"
                        default_start = yesterday_ds()
                else:
                    if plan.provided_start:
                        blank_meaning = f"starting from '{time_like_to_str(plan.provided_start)}'"
                    else:
                        blank_meaning = "from the beginning of history"
                    default_start = None

                start = self._prompt(
                    f"Enter the {backfill_or_preview} start date (eg. '1 year', '2020-01-01') or blank to backfill {blank_meaning}",
                )
                if start:
                    plan_builder.set_start(start)
                elif default_start:
                    plan_builder.set_start(default_start)

            if not plan_builder.override_end:
                if plan.provided_end:
                    blank_meaning = f"'{time_like_to_str(plan.provided_end)}'"
                elif plan.end_override_per_model:
                    max_end = max(plan.end_override_per_model.values())
                    blank_meaning = f"'{time_like_to_str(max_end)}'"
                else:
                    blank_meaning = "now"
                end = self._prompt(
                    f"Enter the {backfill_or_preview} end date (eg. '1 month ago', '2020-01-01') or blank to {backfill_or_preview} up until {blank_meaning}",
                )
                if end:
                    plan_builder.set_end(end)

            plan = plan_builder.build()

    def _prompt_promote(self, plan_builder: PlanBuilder) -> None:
        if self._confirm(
            "Apply - Virtual Update",
        ):
            plan_builder.apply()

    def log_test_results(self, result: ModelTextTestResult, target_dialect: str) -> None:
        # We don't log the test results if no tests were ran
        if not result.testsRun:
            return

        divider_length = 70

        self._log_test_details(result)

        message = (
            f"Ran {result.testsRun} tests against {target_dialect} in {result.duration} seconds."
        )
        if result.wasSuccessful():
            self._print("=" * divider_length)
            self._print(
                f"Successfully {message}",
                style="green",
            )
            self._print("-" * divider_length)
        else:
            self._print("-" * divider_length)
            self._print("Test Failure Summary", style="red")
            self._print("=" * divider_length)
            fail_and_error_tests = result.get_fail_and_error_tests()
            self._print(f"{message} \n")

            self._print(f"Failed tests ({len(fail_and_error_tests)}):")
            for test in fail_and_error_tests:
                self._print(f" • {test.path}::{test.test_name}")
            self._print("=" * divider_length, end="\n\n")

    def _captured_unit_test_results(self, result: ModelTextTestResult) -> str:
        with self.console.capture() as capture:
            self._log_test_details(result)
        return strip_ansi_codes(capture.get())

    def show_sql(self, sql: str) -> None:
        self._print(Syntax(sql, "sql", word_wrap=True), crop=False)

    def log_status_update(self, message: str) -> None:
        self._print(message)

    def log_skipped_models(self, snapshot_names: t.Set[str]) -> None:
        if snapshot_names:
            msg = "  " + "\n  ".join(snapshot_names)
            self._print(f"[dark_orange3]Skipped models[/dark_orange3]\n\n{msg}")

    def log_failed_models(self, errors: t.List[NodeExecutionFailedError]) -> None:
        if errors:
            self._print("\n[red]Failed models[/red]\n")

            error_messages = _format_node_errors(errors)

            for node_name, msg in error_messages.items():
                self._print(f"  [red]{node_name}[/red]\n\n{msg}")

    def log_models_updated_during_restatement(
        self,
        snapshots: t.List[t.Tuple[SnapshotTableInfo, SnapshotTableInfo]],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str] = None,
    ) -> None:
        if snapshots:
            tree = Tree(
                f"[yellow]The following models had new versions deployed while data was being restated:[/yellow]"
            )

            for restated_snapshot, updated_snapshot in snapshots:
                display_name = restated_snapshot.display_name(
                    environment_naming_info,
                    default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                    dialect=self.dialect,
                )
                current_branch = tree.add(display_name)
                current_branch.add(f"restated version: '{restated_snapshot.version}'")
                current_branch.add(f"currently active version: '{updated_snapshot.version}'")

            self._print(tree)
            self._print("")  # newline spacer

    def log_destructive_change(
        self,
        snapshot_name: str,
        alter_operations: t.List[TableAlterOperation],
        dialect: str,
        error: bool = True,
    ) -> None:
        if error:
            self._print(format_destructive_change_msg(snapshot_name, alter_operations, dialect))
        else:
            self.log_warning(
                format_destructive_change_msg(snapshot_name, alter_operations, dialect, error)
            )

    def log_additive_change(
        self,
        snapshot_name: str,
        alter_operations: t.List[TableAlterOperation],
        dialect: str,
        error: bool = True,
    ) -> None:
        if error:
            self._print(format_additive_change_msg(snapshot_name, alter_operations, dialect))
        else:
            self.log_warning(
                format_additive_change_msg(snapshot_name, alter_operations, dialect, error)
            )

    def log_error(self, message: str) -> None:
        self._print(f"[red]{message}[/red]")

    def log_warning(self, short_message: str, long_message: t.Optional[str] = None) -> None:
        logger.warning(long_message or short_message)
        if not self.ignore_warnings:
            if long_message:
                file_path = None
                for handler in logger.root.handlers:
                    if isinstance(handler, logging.FileHandler):
                        file_path = handler.baseFilename
                        break
                file_path_msg = f" Learn more in logs: {file_path}\n" if file_path else ""
                short_message = f"{short_message}{file_path_msg}"
            message_lstrip = short_message.lstrip()
            leading_ws = short_message[: -len(message_lstrip)]
            message_formatted = f"{leading_ws}[yellow]\\[WARNING] {message_lstrip}[/yellow]"
            self._print(message_formatted)

    def log_success(self, message: str) -> None:
        self._print(f"[green]{message}[/green]\n")

    def loading_start(self, message: t.Optional[str] = None) -> uuid.UUID:
        id = uuid.uuid4()
        self.loading_status[id] = Status(message or "", console=self.console, spinner="line")
        self.loading_status[id].start()
        return id

    def loading_stop(self, id: uuid.UUID) -> None:
        self.loading_status[id].stop()
        del self.loading_status[id]

    def show_table_diff_details(
        self,
        models_to_diff: t.List[str],
    ) -> None:
        """Display information about which tables are going to be diffed"""

        if models_to_diff:
            m_tree = Tree("\n[b]Models to compare:")
            for m in models_to_diff:
                m_tree.add(f"[{self.TABLE_DIFF_SOURCE_BLUE}]{m}[/{self.TABLE_DIFF_SOURCE_BLUE}]")
            self._print(m_tree)
            self._print("")

    def start_table_diff_progress(self, models_to_diff: int) -> None:
        if not self.table_diff_progress:
            self.table_diff_progress = make_progress_bar(
                "Calculating model differences", self.console
            )
            self.table_diff_model_progress = Progress(
                TextColumn("{task.fields[view_name]}", justify="right"),
                SpinnerColumn(spinner_name="simpleDots"),
                console=self.console,
            )

            progress_table = Table.grid()
            progress_table.add_row(self.table_diff_progress)
            progress_table.add_row(self.table_diff_model_progress)

            self.table_diff_progress_live = Live(progress_table, refresh_per_second=10)
            self.table_diff_progress_live.start()

            self.table_diff_model_task = self.table_diff_progress.add_task(
                "Diffing", total=models_to_diff
            )

    def start_table_diff_model_progress(self, model: str) -> None:
        if self.table_diff_model_progress and model not in self.table_diff_model_tasks:
            self.table_diff_model_tasks[model] = self.table_diff_model_progress.add_task(
                f"Diffing {model}...",
                view_name=model,
                total=1,
            )

    def update_table_diff_progress(self, model: str) -> None:
        if self.table_diff_progress:
            self.table_diff_progress.update(self.table_diff_model_task, refresh=True, advance=1)
        if self.table_diff_model_progress and model in self.table_diff_model_tasks:
            model_task_id = self.table_diff_model_tasks[model]
            self.table_diff_model_progress.remove_task(model_task_id)

    def stop_table_diff_progress(self, success: bool) -> None:
        if self.table_diff_progress_live:
            self.table_diff_progress_live.stop()
            self.table_diff_progress_live = None
            self.log_status_update("")

            if success:
                self.log_success(f"Table diff completed successfully!")
            else:
                self.log_error("Table diff failed!")

        self.table_diff_progress = None
        self.table_diff_model_progress = None
        self.table_diff_model_tasks = {}

    def show_table_diff_summary(self, table_diff: TableDiff) -> None:
        tree = Tree("\n[b]Table Diff")

        if table_diff.model_name:
            model = Tree("Model:")
            model.add(f"[blue]{table_diff.model_name}[/blue]")

            tree.add(model)

            envs = Tree("Environment:")
            source = Tree(
                f"Source: [{self.TABLE_DIFF_SOURCE_BLUE}]{table_diff.source_alias}[/{self.TABLE_DIFF_SOURCE_BLUE}]"
            )
            envs.add(source)

            target = Tree(
                f"Target: [{self.TABLE_DIFF_TARGET_GREEN}]{table_diff.target_alias}[/{self.TABLE_DIFF_TARGET_GREEN}]"
            )
            envs.add(target)

            tree.add(envs)

        tables = Tree("Tables:")

        tables.add(
            f"Source: [{self.TABLE_DIFF_SOURCE_BLUE}]{table_diff.source}[/{self.TABLE_DIFF_SOURCE_BLUE}]"
        )
        tables.add(
            f"Target: [{self.TABLE_DIFF_TARGET_GREEN}]{table_diff.target}[/{self.TABLE_DIFF_TARGET_GREEN}]"
        )

        tree.add(tables)

        join = Tree("Join On:")
        _, _, key_column_names = table_diff.key_columns
        for col_name in key_column_names:
            join.add(f"[yellow]{col_name}[/yellow]")

        tree.add(join)

        self._print(tree)

    def show_schema_diff(self, schema_diff: SchemaDiff) -> None:
        source_name = schema_diff.source
        if schema_diff.source_alias:
            source_name = schema_diff.source_alias.upper()
        target_name = schema_diff.target
        if schema_diff.target_alias:
            target_name = schema_diff.target_alias.upper()

        first_line = f"\n[b]Schema Diff Between '[{self.TABLE_DIFF_SOURCE_BLUE}]{source_name}[/{self.TABLE_DIFF_SOURCE_BLUE}]' and '[{self.TABLE_DIFF_TARGET_GREEN}]{target_name}[/{self.TABLE_DIFF_TARGET_GREEN}]'"
        if schema_diff.model_name:
            first_line = (
                first_line + f" environments for model '[blue]{schema_diff.model_name}[/blue]'"
            )

        tree = Tree(first_line + ":")

        if any([schema_diff.added, schema_diff.removed, schema_diff.modified]):
            if schema_diff.added:
                added = Tree("[green]Added Columns:")
                for c, t in schema_diff.added:
                    added.add(f"[green]{c} ({t})")
                tree.add(added)

            if schema_diff.removed:
                removed = Tree("[red]Removed Columns:")
                for c, t in schema_diff.removed:
                    removed.add(f"[red]{c} ({t})")
                tree.add(removed)

            if schema_diff.modified:
                modified = Tree("[magenta]Modified Columns:")
                for c, (ft, tt) in schema_diff.modified.items():
                    modified.add(f"[magenta]{c} ({ft} -> {tt})")
                tree.add(modified)
        else:
            tree.add("[b]Schemas match")

        self.console.print(tree)

    def show_row_diff(
        self, row_diff: RowDiff, show_sample: bool = True, skip_grain_check: bool = False
    ) -> None:
        if row_diff.empty:
            self.console.print(
                "\n[b][red]Neither the source nor the target table contained any records[/red][/b]"
            )
            return

        source_name = row_diff.source
        if row_diff.source_alias:
            source_name = row_diff.source_alias.upper()
        target_name = row_diff.target
        if row_diff.target_alias:
            target_name = row_diff.target_alias.upper()

        if row_diff.stats["null_grain_count"] > 0 or (
            not skip_grain_check
            and (
                row_diff.stats["distinct_count_s"] != row_diff.stats["s_count"]
                or row_diff.stats["distinct_count_t"] != row_diff.stats["t_count"]
            )
        ):
            self.console.print(
                "[b][red]\nGrain should have unique and not-null audits for accurate results.[/red][/b]"
            )

        tree = Tree("[b]Row Counts:[/b]")
        if row_diff.full_match_count:
            tree.add(
                f" [b][cyan]FULL MATCH[/cyan]:[/b] {row_diff.full_match_count} rows ({row_diff.full_match_pct}%)"
            )
        if row_diff.partial_match_count:
            tree.add(
                f" [b][blue]PARTIAL MATCH[/blue]:[/b] {row_diff.partial_match_count} rows ({row_diff.partial_match_pct}%)"
            )
        if row_diff.s_only_count:
            tree.add(
                f" [b][yellow]{source_name} ONLY[/yellow]:[/b] {row_diff.s_only_count} rows ({row_diff.s_only_pct}%)"
            )
        if row_diff.t_only_count:
            tree.add(
                f" [b][green]{target_name} ONLY[/green]:[/b] {row_diff.t_only_count} rows ({row_diff.t_only_pct}%)"
            )
        self.console.print("\n", tree)

        self.console.print("\n[b][blue]COMMON ROWS[/blue] column comparison stats:[/b]")
        if row_diff.column_stats.shape[0] > 0:
            self.console.print(row_diff.column_stats.to_string(index=True), end="\n\n")
        else:
            self.console.print("  No columns with same name and data type in both tables")

        if show_sample:
            sample = row_diff.joined_sample
            self.console.print("\n[b][blue]COMMON ROWS[/blue] sample data differences:[/b]")
            if sample.shape[0] > 0:
                keys: list[str] = []
                columns: dict[str, list[str]] = {}
                source_prefix, source_name = (
                    (f"{source_name}__", source_name)
                    if source_name.lower() != row_diff.source.lower()
                    else ("s__", "SOURCE")
                )
                target_prefix, target_name = (
                    (f"{target_name}__", target_name)
                    if target_name.lower() != row_diff.target.lower()
                    else ("t__", "TARGET")
                )

                # Extract key and column names from the joined sample
                for column in row_diff.joined_sample.columns:
                    if source_prefix in column:
                        column_name = "__".join(column.split(source_prefix)[1:])
                        columns[column_name] = [column, target_prefix + column_name]
                    elif target_prefix not in column:
                        keys.append(column)

                column_styles = {
                    source_name: self.TABLE_DIFF_SOURCE_BLUE,
                    target_name: self.TABLE_DIFF_TARGET_GREEN,
                }

                for column, [source_column, target_column] in columns.items():
                    # Create a table with the joined keys and comparison columns
                    column_table = row_diff.joined_sample[keys + [source_column, target_column]]

                    # Filter to retain non identical-valued rows
                    column_table = column_table[
                        column_table.apply(
                            lambda row: not _cells_match(row[source_column], row[target_column]),
                            axis=1,
                        )
                    ]

                    # Rename the column headers for readability
                    column_table = column_table.rename(
                        columns={
                            source_column: source_name,
                            target_column: target_name,
                        }
                    )

                    table = Table(show_header=True)
                    for column_name in column_table.columns:
                        style = column_styles.get(column_name, "")
                        table.add_column(column_name, style=style, header_style=style)

                    for _, row in column_table.iterrows():
                        table.add_row(
                            *[
                                str(
                                    round(cell, row_diff.decimals)
                                    if isinstance(cell, float)
                                    else cell
                                )
                                for cell in row
                            ]
                        )

                    self.console.print(
                        f"Column: [underline][bold cyan]{column}[/bold cyan][/underline]",
                        table,
                        end="\n",
                    )

            else:
                self.console.print("  All joined rows match")

            if row_diff.s_sample.shape[0] > 0:
                self.console.print(f"\n[b][yellow]{source_name} ONLY[/yellow] sample rows:[/b]")
                self.console.print(row_diff.s_sample.to_string(index=False), end="\n\n")

            if row_diff.t_sample.shape[0] > 0:
                self.console.print(f"\n[b][green]{target_name} ONLY[/green] sample rows:[/b]")
                self.console.print(row_diff.t_sample.to_string(index=False), end="\n\n")

    def show_table_diff(
        self,
        table_diffs: t.List[TableDiff],
        show_sample: bool = True,
        skip_grain_check: bool = False,
        temp_schema: t.Optional[str] = None,
    ) -> None:
        """
        Display the table diff between all mismatched tables.
        """
        if len(table_diffs) > 1:
            mismatched_tables = []
            fully_matched = []
            for table_diff in table_diffs:
                if (
                    table_diff.schema_diff().source_schema == table_diff.schema_diff().target_schema
                ) and (
                    table_diff.row_diff(
                        temp_schema=temp_schema, skip_grain_check=skip_grain_check
                    ).full_match_pct
                    == 100
                ):
                    fully_matched.append(table_diff)
                else:
                    mismatched_tables.append(table_diff)
            table_diffs = mismatched_tables if mismatched_tables else []
            if fully_matched:
                m_tree = Tree("\n[b]Identical Tables")
                for m in fully_matched:
                    m_tree.add(
                        f"[{self.TABLE_DIFF_SOURCE_BLUE}]{m.source}[/{self.TABLE_DIFF_SOURCE_BLUE}] - [{self.TABLE_DIFF_TARGET_GREEN}]{m.target}[/{self.TABLE_DIFF_TARGET_GREEN}]"
                    )
                self._print(m_tree)

            if mismatched_tables:
                m_tree = Tree("\n[b]Mismatched Tables")
                for m in mismatched_tables:
                    m_tree.add(
                        f"[{self.TABLE_DIFF_SOURCE_BLUE}]{m.source}[/{self.TABLE_DIFF_SOURCE_BLUE}] - [{self.TABLE_DIFF_TARGET_GREEN}]{m.target}[/{self.TABLE_DIFF_TARGET_GREEN}]"
                    )
                self._print(m_tree)

        for table_diff in table_diffs:
            self.show_table_diff_summary(table_diff)
            self.show_schema_diff(table_diff.schema_diff())
            self.show_row_diff(
                table_diff.row_diff(temp_schema=temp_schema, skip_grain_check=skip_grain_check),
                show_sample=show_sample,
                skip_grain_check=skip_grain_check,
            )

    def print_environments(self, environments_summary: t.List[EnvironmentSummary]) -> None:
        """Prints all environment names along with expiry datetime."""
        output = [
            f"{summary.name} - {time_like_to_str(summary.expiration_ts)}"
            if summary.expiration_ts
            else f"{summary.name} - No Expiry"
            for summary in environments_summary
        ]
        output_str = "\n".join([str(len(output)), *output])
        self.log_status_update(f"Number of SQLMesh environments are: {output_str}")

    def show_intervals(self, snapshot_intervals: t.Dict[Snapshot, SnapshotIntervals]) -> None:
        complete = Tree(f"[b]Complete Intervals[/b]")
        incomplete = Tree(f"[b]Missing Intervals[/b]")

        for snapshot, intervals in sorted(snapshot_intervals.items(), key=lambda s: s[0].node.name):
            if intervals.intervals:
                incomplete.add(
                    f"{snapshot.node.name}: [{intervals.format_intervals(snapshot.node.interval_unit)}]"
                )
            else:
                complete.add(snapshot.node.name)

        if complete.children:
            self._print(complete)

        if incomplete.children:
            self._print(incomplete)

    def print_connection_config(self, config: ConnectionConfig, title: str = "Connection") -> None:
        tree = Tree(f"[b]{title}:[/b]")
        tree.add(f"Type: [bold cyan]{config.type_}[/bold cyan]")
        tree.add(f"Catalog: [bold cyan]{config.get_catalog()}[/bold cyan]")

        try:
            engine_adapter_type = config._engine_adapter
            tree.add(f"Dialect: [bold cyan]{engine_adapter_type.DIALECT}[/bold cyan]")
        except NotImplementedError:
            # not all ConnectionConfig's have an engine adapter associated. The CloudConnectionConfig has a HTTP client instead
            pass

        self._print(tree)

    def _get_snapshot_change_category(
        self,
        snapshot: Snapshot,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        default_catalog: t.Optional[str],
    ) -> None:
        choices = self._snapshot_change_choices(
            snapshot, plan_builder.environment_naming_info, default_catalog
        )
        response = self._prompt(
            "\n".join([f"[{i + 1}] {choice}" for i, choice in enumerate(choices.values())]),
            show_choices=False,
            choices=[f"{i + 1}" for i in range(len(choices))],
        )
        choice = list(choices)[int(response) - 1]
        plan_builder.set_choice(snapshot, choice)

    def _snapshot_change_choices(
        self,
        snapshot: Snapshot,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        use_rich_formatting: bool = True,
    ) -> t.Dict[SnapshotChangeCategory, str]:
        direct = snapshot.display_name(
            environment_naming_info,
            default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
            dialect=self.dialect,
        )
        if use_rich_formatting:
            direct = f"[direct]{direct}[/direct]"
        indirect = "indirectly modified children"
        if use_rich_formatting:
            indirect = f"[indirect]{indirect}[/indirect]"
        if snapshot.is_view:
            choices = {
                SnapshotChangeCategory.BREAKING: f"Update {direct} and backfill {indirect}",
                SnapshotChangeCategory.NON_BREAKING: f"Update {direct} but don't backfill {indirect}",
            }
        elif snapshot.is_symbolic:
            choices = {
                SnapshotChangeCategory.BREAKING: f"Backfill {indirect}",
                SnapshotChangeCategory.NON_BREAKING: f"Don't backfill {indirect}",
            }
        else:
            choices = {
                SnapshotChangeCategory.BREAKING: f"Backfill {direct} and {indirect}",
                SnapshotChangeCategory.NON_BREAKING: f"Backfill {direct} but not {indirect}",
            }
        labeled_choices = {
            k: f"[{SNAPSHOT_CHANGE_CATEGORY_STR[k]}] {v}" for k, v in choices.items()
        }
        return labeled_choices

    def show_linter_violations(
        self, violations: t.List[RuleViolation], model: Model, is_error: bool = False
    ) -> None:
        severity = "errors" if is_error else "warnings"

        # Sort violations by line, then alphabetically the name of the violation
        # Violations with no range go first
        sorted_violations = sorted(
            violations,
            key=lambda v: (
                v.violation_range.start.line if v.violation_range else -1,
                v.rule.name.lower(),
            ),
        )
        violations_text = [
            (
                f" - Line {v.violation_range.start.line + 1}: {v.rule.name} - {v.violation_msg}"
                if v.violation_range
                else f" - {v.rule.name}: {v.violation_msg}"
            )
            for v in sorted_violations
        ]
        violations_msg = "\n".join(violations_text)
        msg = f"Linter {severity} for {model._path}:\n{violations_msg}"

        if is_error:
            self.log_error(msg)
        else:
            self.log_warning(msg)

    def _log_test_details(
        self, result: ModelTextTestResult, unittest_char_separator: bool = True
    ) -> None:
        """
        This is a helper method that encapsulates the logic for logging the relevant unittest for the result.
        The top level method (`log_test_results`) reuses `_log_test_details` differently based on the console.

        Args:
            result: The unittest test result that contains metrics like num success, fails, ect.
        """
        if result.wasSuccessful():
            self._print("\n", end="")
            return

        if unittest_char_separator:
            self._print(f"\n{unittest.TextTestResult.separator1}\n\n", end="")

        for (test_case, failure), test_failure_tables in zip_longest(  # type: ignore
            result.failures, result.failure_tables
        ):
            self._print(unittest.TextTestResult.separator2)
            self._print(f"FAIL: {test_case}")

            if test_description := test_case.shortDescription():
                self._print(test_description)
            self._print(f"{unittest.TextTestResult.separator2}")

            if not test_failure_tables:
                self._print(failure)
            else:
                for failure_table in test_failure_tables:
                    self._print(failure_table)
                    self._print("\n", end="")

        for test_case, error in result.errors:
            self._print(unittest.TextTestResult.separator2)
            self._print(f"ERROR: {test_case}")
            self._print(f"{unittest.TextTestResult.separator2}")
            self._print(error)


def _cells_match(x: t.Any, y: t.Any) -> bool:
    """Helper function to compare two cells and returns true if they're equal, handling array objects."""
    import pandas as pd
    import numpy as np

    # Convert array-like objects to list for consistent comparison
    def _normalize(val: t.Any) -> t.Any:
        # Convert Pandas null to Python null for the purposes of comparison to prevent errors like the following on boolean fields:
        # - TypeError: boolean value of NA is ambiguous
        # note pd.isnull() returns either a bool or a ndarray[bool] depending on if the input
        # is scalar or an array
        isnull = pd.isnull(val)

        if isinstance(isnull, bool):  # scalar
            if isnull:
                val = None
        elif all(isnull):  # array
            val = None

        return list(val) if isinstance(val, (pd.Series, np.ndarray)) else val

    return _normalize(x) == _normalize(y)


def add_to_layout_widget(target_widget: LayoutWidget, *widgets: widgets.Widget) -> LayoutWidget:
    """Helper function to add a widget to a layout widget.

    Args:
        target_widget: The layout widget to add the other widget(s) to.
        *widgets: The widgets to add to the layout widget.

    Returns:
        The layout widget with the children added.
    """
    target_widget.children += tuple(widgets)
    return target_widget


class NotebookMagicConsole(TerminalConsole):
    """
    Console to be used when using the magic notebook interface (`%<command>`).
    Generally reuses the Terminal console when possible by either directly outputing what it provides
    or capturing it and converting it into a widget.
    """

    def __init__(
        self,
        display: t.Optional[t.Callable] = None,
        console: t.Optional[RichConsole] = None,
        dialect: DialectType = None,
        **kwargs: t.Any,
    ) -> None:
        import ipywidgets as widgets
        from IPython import get_ipython
        from IPython.display import display as ipython_display

        super().__init__(console, **kwargs)

        ipython = get_ipython()
        self.display = display or (
            ipython.user_ns.get("display", ipython_display) if ipython else ipython_display
        )
        self.missing_dates_output = widgets.Output()
        self.dynamic_options_after_categorization_output = widgets.VBox()

        self.dialect = dialect

    def _show_missing_dates(self, plan: Plan, default_catalog: t.Optional[str]) -> None:
        self._add_to_dynamic_options(self.missing_dates_output)
        self.missing_dates_output.outputs = ()
        with self.missing_dates_output:
            super()._show_missing_dates(plan, default_catalog)

    def _apply(self, button: widgets.Button) -> None:
        button.disabled = True
        with button.output:
            button.plan_builder.apply()

    def _prompt_promote(self, plan_builder: PlanBuilder) -> None:
        import ipywidgets as widgets

        button = widgets.Button(
            description="Apply - Virtual Update",
            disabled=False,
            button_style="success",
            # Auto will make the button really large.
            # Likely changing this soon anyways to be just `Apply` with description above
            layout={"width": "10rem"},
        )
        self._add_to_dynamic_options(button)
        output = widgets.Output()
        self._add_to_dynamic_options(output)

        button.plan_builder = plan_builder
        button.on_click(self._apply)
        button.output = output

    def _prompt_effective_from(
        self, plan_builder: PlanBuilder, auto_apply: bool, default_catalog: t.Optional[str]
    ) -> None:
        import ipywidgets as widgets

        prompt = widgets.VBox()

        def effective_from_change_callback(change: t.Dict[str, datetime.datetime]) -> None:
            plan_builder.set_effective_from(change["new"])
            self._show_options_after_categorization(
                plan_builder, auto_apply, default_catalog, no_prompts=False
            )

        def going_forward_change_callback(change: t.Dict[str, bool]) -> None:
            checked = change["new"]
            plan_builder.set_effective_from(None if checked else yesterday_ds())
            self._show_options_after_categorization(
                plan_builder,
                auto_apply=auto_apply,
                default_catalog=default_catalog,
                no_prompts=False,
            )

        date_picker = widgets.DatePicker(
            disabled=plan_builder.build().effective_from is None,
            value=to_date(plan_builder.build().effective_from or yesterday_ds()),
            layout={"width": "auto"},
        )
        date_picker.observe(effective_from_change_callback, "value")

        going_forward_checkbox = widgets.Checkbox(
            value=plan_builder.build().effective_from is None,
            description="Apply Going Forward Once Deployed To Prod",
            disabled=False,
            indent=False,
        )
        going_forward_checkbox.observe(going_forward_change_callback, "value")

        add_to_layout_widget(
            prompt,
            widgets.HBox(
                [
                    widgets.Label("Effective From Date:", layout={"width": "8rem"}),
                    date_picker,
                    going_forward_checkbox,
                ]
            ),
        )

        self._add_to_dynamic_options(prompt)

    def _prompt_backfill(
        self, plan_builder: PlanBuilder, auto_apply: bool, default_catalog: t.Optional[str]
    ) -> None:
        import ipywidgets as widgets

        prompt = widgets.VBox()

        backfill_or_preview = (
            "Preview"
            if plan_builder.build().is_dev and plan_builder.build().forward_only
            else "Backfill"
        )

        def _date_picker(
            plan_builder: PlanBuilder, value: t.Any, on_change: t.Callable, disabled: bool = False
        ) -> widgets.DatePicker:
            picker = widgets.DatePicker(
                disabled=disabled,
                value=value,
                layout={"width": "auto"},
            )

            picker.observe(on_change, "value")
            return picker

        def start_change_callback(change: t.Dict[str, datetime.datetime]) -> None:
            plan_builder.set_start(change["new"])
            self._show_options_after_categorization(
                plan_builder, auto_apply, default_catalog, no_prompts=False
            )

        def end_change_callback(change: t.Dict[str, datetime.datetime]) -> None:
            plan_builder.set_end(change["new"])
            self._show_options_after_categorization(
                plan_builder, auto_apply, default_catalog, no_prompts=False
            )

        if plan_builder.is_start_and_end_allowed:
            add_to_layout_widget(
                prompt,
                widgets.HBox(
                    [
                        widgets.Label(
                            f"Start {backfill_or_preview} Date:", layout={"width": "8rem"}
                        ),
                        _date_picker(
                            plan_builder, to_date(plan_builder.build().start), start_change_callback
                        ),
                    ]
                ),
            )

            add_to_layout_widget(
                prompt,
                widgets.HBox(
                    [
                        widgets.Label(f"End {backfill_or_preview} Date:", layout={"width": "8rem"}),
                        _date_picker(
                            plan_builder,
                            to_date(plan_builder.build().end),
                            end_change_callback,
                        ),
                    ]
                ),
            )

        self._add_to_dynamic_options(prompt)

        if not auto_apply:
            button = widgets.Button(
                description=f"Apply - {backfill_or_preview} Tables",
                disabled=False,
                button_style="success",
            )
            self._add_to_dynamic_options(button)
            output = widgets.Output()
            self._add_to_dynamic_options(output)

            button.plan_builder = plan_builder
            button.on_click(self._apply)
            button.output = output

    def _show_options_after_categorization(
        self,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        default_catalog: t.Optional[str],
        no_prompts: bool,
    ) -> None:
        self.dynamic_options_after_categorization_output.children = ()
        self.display(self.dynamic_options_after_categorization_output)
        super()._show_options_after_categorization(
            plan_builder, auto_apply, default_catalog, no_prompts
        )

    def _add_to_dynamic_options(self, *widgets: widgets.Widget) -> None:
        add_to_layout_widget(self.dynamic_options_after_categorization_output, *widgets)

    def _get_snapshot_change_category(
        self,
        snapshot: Snapshot,
        plan_builder: PlanBuilder,
        auto_apply: bool,
        default_catalog: t.Optional[str],
    ) -> None:
        import ipywidgets as widgets

        choice_mapping = self._snapshot_change_choices(
            snapshot,
            plan_builder.environment_naming_info,
            default_catalog,
            use_rich_formatting=False,
        )
        choices = list(choice_mapping)
        plan_builder.set_choice(snapshot, choices[0])

        def radio_button_selected(change: t.Dict[str, t.Any]) -> None:
            plan_builder.set_choice(snapshot, choices[change["owner"].index])
            self._show_options_after_categorization(
                plan_builder, auto_apply, default_catalog, no_prompts=False
            )

        radio = widgets.RadioButtons(
            options=choice_mapping.values(),
            layout={"width": "max-content"},
            disabled=False,
        )
        radio.observe(
            radio_button_selected,
            "value",
        )
        self.display(radio)

    def log_test_results(self, result: ModelTextTestResult, target_dialect: str) -> None:
        # We don't log the test results if no tests were ran
        if not result.testsRun:
            return

        import ipywidgets as widgets

        divider_length = 70
        shared_style = {
            "font-size": "11px",
            "font-weight": "bold",
            "font-family": "Menlo,'DejaVu Sans Mono',consolas,'Courier New',monospace",
        }

        message = (
            f"Ran {result.testsRun} tests against {target_dialect} in {result.duration} seconds."
        )

        if result.wasSuccessful():
            success_color = {"color": "#008000"}
            header = str(h("span", {"style": shared_style}, "-" * divider_length))
            message = str(
                h(
                    "span",
                    {"style": {**shared_style, **success_color}},
                    f"Successfully {message}",
                )
            )
            footer = str(h("span", {"style": shared_style}, "=" * divider_length))
            self.display(widgets.HTML("<br>".join([header, message, footer])))
        else:
            output = self._captured_unit_test_results(result)

            fail_color = {"color": "#db3737"}
            fail_shared_style = {**shared_style, **fail_color}
            header = str(h("span", {"style": fail_shared_style}, "-" * divider_length))
            message = str(h("span", {"style": fail_shared_style}, "Test Failure Summary"))
            fail_and_error_tests = result.get_fail_and_error_tests()
            failed_tests = [
                str(
                    h(
                        "span",
                        {"style": fail_shared_style},
                        f"Failed tests ({len(fail_and_error_tests)}):",
                    )
                )
            ]

            for test in fail_and_error_tests:
                failed_tests.append(
                    str(
                        h(
                            "span",
                            {"style": fail_shared_style},
                            f" • {test.model.name}::{test.test_name}",
                        )
                    )
                )
            failures = "<br>".join(failed_tests)
            footer = str(h("span", {"style": fail_shared_style}, "=" * divider_length))
            error_output = widgets.Textarea(output, layout={"height": "300px", "width": "100%"})
            test_info = widgets.HTML("<br>".join([header, message, footer, failures, footer]))
            self.display(widgets.VBox(children=[test_info, error_output], layout={"width": "100%"}))


class CaptureTerminalConsole(TerminalConsole):
    """
    Captures the output of the terminal console so that it can be extracted out and displayed within other interfaces.
    The captured output is cleared out after it is retrieved.

    Note: `_prompt` and `_confirm` need to also be overriden to work with the custom interface if you want to use
    this console interactively.
    """

    def __init__(self, console: t.Optional[RichConsole] = None, **kwargs: t.Any) -> None:
        super().__init__(console=console, **kwargs)
        self._captured_outputs: t.List[str] = []
        self._warnings: t.List[str] = []
        self._errors: t.List[str] = []

    @property
    def captured_output(self) -> str:
        return "".join(self._captured_outputs)

    @property
    def captured_warnings(self) -> str:
        return "".join(self._warnings)

    @property
    def captured_errors(self) -> str:
        return "".join(self._errors)

    def consume_captured_output(self) -> str:
        try:
            return self.captured_output
        finally:
            self._captured_outputs = []

    def consume_captured_warnings(self) -> str:
        try:
            return self.captured_warnings
        finally:
            self._warnings = []

    def consume_captured_errors(self) -> str:
        try:
            return self.captured_errors
        finally:
            self._errors = []

    def log_warning(
        self,
        short_message: str,
        long_message: t.Optional[str] = None,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> None:
        if short_message not in self._warnings:
            self._warnings.append(short_message)
        if kwargs.pop("print", True):
            super().log_warning(short_message, long_message)

    def log_error(self, message: str, *args: t.Any, **kwargs: t.Any) -> None:
        if message not in self._errors:
            self._errors.append(message)
        if kwargs.pop("print", True):
            super().log_error(message)

    def log_skipped_models(self, snapshot_names: t.Set[str]) -> None:
        if snapshot_names:
            self._captured_outputs.append(
                "\n".join([f"SKIPPED snapshot {skipped}\n" for skipped in snapshot_names])
            )
            super().log_skipped_models(snapshot_names)

    def log_failed_models(self, errors: t.List[NodeExecutionFailedError]) -> None:
        self._errors.extend([str(ex) for ex in errors if str(ex) not in self._errors])
        super().log_failed_models(errors)

    def _print(self, value: t.Any, **kwargs: t.Any) -> None:
        with self.console.capture() as capture:
            self.console.print(value, **kwargs)
        self._captured_outputs.append(capture.get())


class MarkdownConsole(CaptureTerminalConsole):
    """
    A console that outputs markdown. Currently this is only configured for non-interactive use so for use cases
    where you want to display a plan or test results in markdown.
    """

    CHECK_MARK = ""
    AUDIT_PASS_MARK = "passed "
    GREEN_AUDIT_PASS_MARK = AUDIT_PASS_MARK
    AUDIT_FAIL_MARK = "failed "
    AUDIT_PADDING = 7

    def __init__(self, **kwargs: t.Any) -> None:
        self.alert_block_max_content_length = int(kwargs.pop("alert_block_max_content_length", 500))
        self.alert_block_collapsible_threshold = int(
            kwargs.pop("alert_block_collapsible_threshold", 200)
        )

        # capture_only = True: capture but dont print to console
        # capture_only = False: capture and also print to console
        self.warning_capture_only = kwargs.pop("warning_capture_only", False)
        self.error_capture_only = kwargs.pop("error_capture_only", False)

        super().__init__(
            **{**kwargs, "console": RichConsole(no_color=True, width=kwargs.pop("width", None))}
        )

    def show_environment_difference_summary(
        self,
        context_diff: ContextDiff,
        no_diff: bool = True,
    ) -> None:
        """Shows a summary of the environment differences.

        Args:
            context_diff: The context diff to use to print the summary.
            no_diff: Hide the actual environment statements differences.
        """
        if context_diff.is_new_environment:
            msg = (
                f"\n**`{context_diff.environment}` environment will be initialized**"
                if not context_diff.create_from_env_exists
                else f"\n**New environment `{context_diff.environment}` will be created from `{context_diff.create_from}`**"
            )
            self._print(msg)
            if not context_diff.has_snapshot_changes:
                return

        if not context_diff.has_changes:
            self._print(
                f"\n**No changes to plan: project files match the `{context_diff.environment}` environment**\n"
            )
            return

        self._print(f"\n**Summary of differences from `{context_diff.environment}`:**")

        if context_diff.has_requirement_changes:
            self._print(f"\nRequirements:\n{context_diff.requirements_diff()}")

        if context_diff.has_environment_statements_changes and not no_diff:
            self._print("\nEnvironment statements:\n")
            for _, diff in context_diff.environment_statements_diff(
                include_python_env=not context_diff.is_new_environment
            ):
                self._print(diff)

    def show_model_difference_summary(
        self,
        context_diff: ContextDiff,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        no_diff: bool = True,
    ) -> None:
        """Shows a summary of the model differences.

        Args:
            context_diff: The context diff to use to print the summary.
            environment_naming_info: The environment naming info to reference when printing model names
            default_catalog: The default catalog to reference when deciding to remove catalog from display names
            no_diff: Hide the actual SQL differences.
        """
        added_snapshots = {context_diff.snapshots[s_id] for s_id in context_diff.added}
        if added_snapshots:
            self._print("\n**Added Models:**")
            self._print_models_with_threshold(
                environment_naming_info, {s for s in added_snapshots if s.is_model}, default_catalog
            )

        added_snapshot_audits = {s for s in added_snapshots if s.is_audit}
        if added_snapshot_audits:
            self._print("\n**Added Standalone Audits:**")
            for snapshot in sorted(added_snapshot_audits):
                self._print(
                    f"- `{snapshot.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}`"
                )

        removed_snapshot_table_infos = set(context_diff.removed_snapshots.values())
        if removed_snapshot_table_infos:
            self._print("\n**Removed Models:**")
            self._print_models_with_threshold(
                environment_naming_info,
                {s for s in removed_snapshot_table_infos if s.is_model},
                default_catalog,
            )

        removed_audit_snapshot_table_infos = {s for s in removed_snapshot_table_infos if s.is_audit}
        if removed_audit_snapshot_table_infos:
            self._print("\n**Removed Standalone Audits:**")
            for snapshot_table_info in sorted(removed_audit_snapshot_table_infos):
                self._print(
                    f"- `{snapshot_table_info.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}`"
                )

        modified_snapshots = {
            current_snapshot for current_snapshot, _ in context_diff.modified_snapshots.values()
        }
        if modified_snapshots:
            self._print_modified_models(
                context_diff, modified_snapshots, environment_naming_info, default_catalog, no_diff
            )

    def _print_models_with_threshold(
        self,
        environment_naming_info: EnvironmentNamingInfo,
        snapshot_table_infos: t.Set[SnapshotInfoLike],
        default_catalog: t.Optional[str] = None,
    ) -> None:
        models = sorted(snapshot_table_infos)
        list_length = len(models)
        if (
            self.verbosity < Verbosity.VERY_VERBOSE
            and list_length > self.INDIRECTLY_MODIFIED_DISPLAY_THRESHOLD
        ):
            self._print(
                f"- `{models[0].display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}`"
            )
            self._print(f"- `.... {list_length - 2} more ....`\n")
            self._print(
                f"- `{models[-1].display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}`"
            )
        else:
            for snapshot_table_info in models:
                category_str = SNAPSHOT_CHANGE_CATEGORY_STR[snapshot_table_info.change_category]
                self._print(
                    f"- `{snapshot_table_info.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}` ({category_str})"
                )

    def _print_modified_models(
        self,
        context_diff: ContextDiff,
        modified_snapshots: t.Set[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str] = None,
        no_diff: bool = True,
    ) -> None:
        directly_modified = []
        indirectly_modified: t.List[Snapshot] = []
        metadata_modified = []
        for snapshot in modified_snapshots:
            if context_diff.directly_modified(snapshot.name):
                directly_modified.append(snapshot)
            elif context_diff.indirectly_modified(snapshot.name):
                indirectly_modified.append(snapshot)
            elif context_diff.metadata_updated(snapshot.name):
                metadata_modified.append(snapshot)
        if directly_modified:
            self._print("\n**Directly Modified:**")
            for snapshot in sorted(directly_modified):
                category_str = SNAPSHOT_CHANGE_CATEGORY_STR[snapshot.change_category]
                self._print(
                    f"* `{snapshot.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}` ({category_str})"
                )

                indirectly_modified_children = sorted(
                    [s for s in indirectly_modified if snapshot.snapshot_id in s.parents]
                )

                if not no_diff:
                    diff_text = context_diff.text_diff(snapshot.name)
                    # sometimes there is no text_diff, like on a seed model where the data has been updated
                    if diff_text:
                        diff_text = f"\n```diff\n{diff_text}\n```"
                        # these are part of a Markdown list, so indent them by 2 spaces to relate them to the current list item
                        diff_text_indented = "\n".join(
                            [f"  {line}" for line in diff_text.splitlines()]
                        )
                        self._print(diff_text_indented)
                    else:
                        if indirectly_modified_children:
                            self._print("\n")

                if indirectly_modified_children:
                    self._print("  Indirectly Modified Children:")
                    for child_snapshot in indirectly_modified_children:
                        child_category_str = SNAPSHOT_CHANGE_CATEGORY_STR[
                            child_snapshot.change_category
                        ]
                        self._print(
                            f"    - `{child_snapshot.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}` ({child_category_str})"
                        )
                    self._print("\n")

        if indirectly_modified:
            self._print("\n**Indirectly Modified:**")
            self._print_models_with_threshold(
                environment_naming_info, set(indirectly_modified), default_catalog
            )
        if metadata_modified:
            self._print("\n**Metadata Updated:**")
            for snapshot in sorted(metadata_modified):
                self._print(
                    f"- `{snapshot.display_name(environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}`"
                )

    def _show_missing_dates(self, plan: Plan, default_catalog: t.Optional[str]) -> None:
        """Displays the models with missing dates."""
        missing_intervals = plan.missing_intervals
        if not missing_intervals:
            return
        self._print("\n**Models needing backfill:**")
        snapshots = []
        for missing in missing_intervals:
            snapshot = plan.context_diff.snapshots[missing.snapshot_id]
            if not snapshot.is_model:
                continue

            preview_modifier = ""
            if not plan.deployability_index.is_deployable(snapshot):
                preview_modifier = " (**preview**)"

            display_name = snapshot.display_name(
                plan.environment_naming_info,
                default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                dialect=self.dialect,
            )
            snapshots.append(
                f"* `{display_name}`: \\[{_format_missing_intervals(snapshot, missing)}]{preview_modifier}"
            )

        length = len(snapshots)
        if (
            self.verbosity < Verbosity.VERY_VERBOSE
            and length > self.INDIRECTLY_MODIFIED_DISPLAY_THRESHOLD
        ):
            self._print(snapshots[0])
            self._print(f"- `.... {length - 2} more ....`\n")
            self._print(snapshots[-1])
        else:
            for snap in snapshots:
                self._print(snap)

    def _show_categorized_snapshots(self, plan: Plan, default_catalog: t.Optional[str]) -> None:
        context_diff = plan.context_diff
        for snapshot in plan.categorized:
            if context_diff.directly_modified(snapshot.name):
                category_str = SNAPSHOT_CHANGE_CATEGORY_STR[snapshot.change_category]
                tree = Tree(
                    f"[bold][direct]Directly Modified: {snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)} ({category_str})"
                )
                indirect_tree = None
                for child_sid in sorted(plan.indirectly_modified.get(snapshot.snapshot_id, set())):
                    child_snapshot = context_diff.snapshots[child_sid]
                    if not indirect_tree:
                        indirect_tree = Tree("[indirect]Indirectly Modified Children:")
                        tree.add(indirect_tree)
                    child_category_str = SNAPSHOT_CHANGE_CATEGORY_STR[
                        child_snapshot.change_category
                    ]
                    indirect_tree.add(
                        f"[indirect]{child_snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)} ({child_category_str})"
                    )
                if indirect_tree:
                    indirect_tree = self._limit_model_names(indirect_tree, self.verbosity)
            elif context_diff.metadata_updated(snapshot.name):
                tree = Tree(
                    f"[bold][metadata]Metadata Updated: {snapshot.display_name(plan.environment_naming_info, default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None, dialect=self.dialect)}"
                )
            else:
                continue

            self._print(f"```diff\n{context_diff.text_diff(snapshot.name)}\n```\n")
            self._print("```\n")
            self._print(tree)
            self._print("\n```")

    def stop_evaluation_progress(self, success: bool = True) -> None:
        super().stop_evaluation_progress(success)
        self._print("\n")

    def stop_creation_progress(self, success: bool = True) -> None:
        super().stop_creation_progress(success)
        self._print("\n")

    def stop_promotion_progress(self, success: bool = True) -> None:
        super().stop_promotion_progress(success)
        self._print("\n")

    def log_warning(self, short_message: str, long_message: t.Optional[str] = None) -> None:
        super().log_warning(short_message, long_message, print=not self.warning_capture_only)

    def log_error(self, message: str) -> None:
        super().log_error(message, print=not self.error_capture_only)

    def log_success(self, message: str) -> None:
        self._print(message)

    def log_test_results(self, result: ModelTextTestResult, target_dialect: str) -> None:
        # We don't log the test results if no tests were ran
        if not result.testsRun:
            return

        message = f"Ran `{result.testsRun}` Tests Against `{target_dialect}`"

        if result.wasSuccessful():
            self._print(f"**Successfully {message}**\n\n")
        else:
            self._print("```")
            self._log_test_details(result, unittest_char_separator=False)
            self._print("```\n\n")

            fail_and_error_tests = result.get_fail_and_error_tests()
            self._print(f"**{message}**\n")
            self._print(f"**Failed tests ({len(fail_and_error_tests)}):**")
            for test in fail_and_error_tests:
                if isinstance(test, ModelTest):
                    self._print(f" • `{test.model.name}`::`{test.test_name}`\n\n")

    def log_skipped_models(self, snapshot_names: t.Set[str]) -> None:
        if snapshot_names:
            self._print(f"**Skipped models**")
            for snapshot_name in snapshot_names:
                self._print(f"* `{snapshot_name}`")
            self._print("")

    def log_failed_models(self, errors: t.List[NodeExecutionFailedError]) -> None:
        if errors:
            self._print("**Failed models**")

            error_messages = _format_node_errors(errors)

            for node_name, msg in error_messages.items():
                self._print(f"* `{node_name}`\n")
                self._print("  ```")
                self._print(msg)
                self._print("  ```")

            self._print("")

    def show_linter_violations(
        self, violations: t.List[RuleViolation], model: Model, is_error: bool = False
    ) -> None:
        severity = "**errors**" if is_error else "warnings"
        violations_msg = "\n".join(f" - {violation}" for violation in violations)
        msg = f"\nLinter {severity} for `{model._path}`:\n{violations_msg}\n"

        self._print(msg)
        if is_error:
            self._errors.append(msg)
        else:
            self._warnings.append(msg)

    @property
    def captured_warnings(self) -> str:
        return self._render_alert_block("WARNING", self._warnings)

    @property
    def captured_errors(self) -> str:
        return self._render_alert_block("CAUTION", self._errors)

    def _render_alert_block(self, block_type: str, items: t.List[str]) -> str:
        # GitHub Markdown alert syntax, https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#alerts
        if items:
            item_contents = ""
            list_indicator = "- " if len(items) > 1 else ""

            for item in items:
                item = item.replace("\n", "\n> ")
                item_contents += f">\n> {list_indicator}{item}\n"

                if len(item_contents) > self.alert_block_max_content_length:
                    truncation_msg = (
                        "...\n>\n> Truncated. Please check the console for full information.\n"
                    )
                    item_contents = item_contents[
                        0 : self.alert_block_max_content_length - len(truncation_msg)
                    ]
                    item_contents += truncation_msg
                    break

            if len(item_contents) > self.alert_block_collapsible_threshold:
                item_contents = f"> <details>\n{item_contents}> </details>"

            return f"> [!{block_type}]\n{item_contents}\n"

        return ""

    def _print(self, value: t.Any, **kwargs: t.Any) -> None:
        self.console.print(value, **kwargs)
        with self.console.capture() as capture:
            self.console.print(value, **kwargs)
        self._captured_outputs.append(capture.get())


class DatabricksMagicConsole(CaptureTerminalConsole):
    """
    Note: Databricks Magic Console currently does not support progress bars while a plan is being applied. The
    NotebookMagicConsole does support progress bars, but they will time out after 5 minutes of execution
    and it makes it difficult to see the progress of the plan.
    """

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self.evaluation_batch_progress: t.Dict[SnapshotId, t.Tuple[str, int]] = {}
        self.promotion_status: t.Tuple[int, int] = (0, 0)
        self.model_creation_status: t.Tuple[int, int] = (0, 0)
        self.migration_status: t.Tuple[int, int] = (0, 0)

    def _print(self, value: t.Any, **kwargs: t.Any) -> None:
        super()._print(value, **kwargs)
        for captured_output in self._captured_outputs:
            print(captured_output)
        self.consume_captured_output()

    def _prompt(self, message: str, **kwargs: t.Any) -> t.Any:
        self._print(message)
        return super()._prompt("", **kwargs)

    def _confirm(self, message: str, **kwargs: t.Any) -> bool:
        message = f"{message} [y/n]"
        self._print(message)
        return super()._confirm("", **kwargs)

    def start_evaluation_progress(
        self,
        batched_intervals: t.Dict[Snapshot, Intervals],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        audit_only: bool = False,
    ) -> None:
        self.evaluation_model_batch_sizes = {
            snapshot: len(intervals) for snapshot, intervals in batched_intervals.items()
        }
        self.evaluation_environment_naming_info = environment_naming_info
        self.default_catalog = default_catalog

    def start_snapshot_evaluation_progress(
        self, snapshot: Snapshot, audit_only: bool = False
    ) -> None:
        if not self.evaluation_batch_progress.get(snapshot.snapshot_id):
            display_name = snapshot.display_name(
                self.evaluation_environment_naming_info,
                self.default_catalog if self.verbosity < Verbosity.VERY_VERBOSE else None,
                dialect=self.dialect,
            )
            self.evaluation_batch_progress[snapshot.snapshot_id] = (display_name, 0)
            print(
                f"Starting '{display_name}', Total batches: {self.evaluation_model_batch_sizes[snapshot]}"
            )

    def update_snapshot_evaluation_progress(
        self,
        snapshot: Snapshot,
        interval: Interval,
        batch_idx: int,
        duration_ms: t.Optional[int],
        num_audits_passed: int,
        num_audits_failed: int,
        audit_only: bool = False,
        execution_stats: t.Optional[QueryExecutionStats] = None,
        auto_restatement_triggers: t.Optional[t.List[SnapshotId]] = None,
    ) -> None:
        view_name, loaded_batches = self.evaluation_batch_progress[snapshot.snapshot_id]

        if audit_only:
            print(f"Completed Auditing {view_name}")
            return

        total_batches = self.evaluation_model_batch_sizes[snapshot]

        loaded_batches += 1
        self.evaluation_batch_progress[snapshot.snapshot_id] = (view_name, loaded_batches)

        finished_loading = loaded_batches == total_batches
        status = "Loaded" if finished_loading else "Loading"
        print(f"{status} '{view_name}', Completed Batches: {loaded_batches}/{total_batches}")
        if finished_loading:
            total_finished_loading = len(
                [
                    s
                    for s, total in self.evaluation_model_batch_sizes.items()
                    if self.evaluation_batch_progress.get(s.snapshot_id, (None, -1))[1] == total
                ]
            )
            total = len(self.evaluation_batch_progress)
            print(f"Completed Loading {total_finished_loading}/{total} Models")

    def stop_evaluation_progress(self, success: bool = True) -> None:
        self.evaluation_batch_progress = {}
        super().stop_evaluation_progress(success)
        print(f"Loading {'succeeded' if success else 'failed'}")

    def start_creation_progress(
        self,
        snapshots: t.List[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        """Indicates that a new creation progress has begun."""
        self.model_creation_status = (0, len(snapshots))
        print("Starting Creating New Model Versions")

    def update_creation_progress(self, snapshot: SnapshotInfoLike) -> None:
        """Update the snapshot creation progress."""
        num_creations, total_creations = self.model_creation_status
        num_creations += 1
        self.model_creation_status = (num_creations, total_creations)
        if num_creations % 5 == 0:
            print(f"Created New Model Versions: {num_creations}/{total_creations}")

    def stop_creation_progress(self, success: bool = True) -> None:
        """Stop the snapshot creation progress."""
        self.model_creation_status = (0, 0)
        print(f"New Model Creation {'succeeded' if success else 'failed'}")

    def start_promotion_progress(
        self,
        snapshots: t.List[SnapshotTableInfo],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        """Indicates that a new snapshot promotion progress has begun."""
        self.promotion_status = (0, len(snapshots))
        print(f"Virtually Updating '{environment_naming_info.name}'")

    def update_promotion_progress(self, snapshot: SnapshotInfoLike, promoted: bool) -> None:
        """Update the snapshot promotion progress."""
        num_promotions, total_promotions = self.promotion_status
        num_promotions += 1
        self.promotion_status = (num_promotions, total_promotions)
        if num_promotions % 5 == 0:
            print(f"Virtually Updated {num_promotions}/{total_promotions}")

    def stop_promotion_progress(self, success: bool = True) -> None:
        """Stop the snapshot promotion progress."""
        self.promotion_status = (0, 0)
        print(f"Virtual Update {'succeeded' if success else 'failed'}")

    def start_snapshot_migration_progress(self, total_tasks: int) -> None:
        """Indicates that a new migration progress has begun."""
        self.migration_status = (0, total_tasks)
        print("Starting Migration")

    def update_snapshot_migration_progress(self, num_tasks: int) -> None:
        """Update the migration progress."""
        num_migrations, total_migrations = self.migration_status
        num_migrations += num_tasks
        self.migration_status = (num_migrations, total_migrations)
        if num_migrations % 5 == 0:
            print(f"Migration Updated {num_migrations}/{total_migrations}")

    def log_migration_status(self, success: bool = True) -> None:
        """Log the migration status."""
        print(f"Migration {'succeeded' if success else 'failed'}")

    def stop_snapshot_migration_progress(self, success: bool = True) -> None:
        """Stop the migration progress."""
        self.migration_status = (0, 0)
        print(f"Snapshot migration {'succeeded' if success else 'failed'}")

    def start_env_migration_progress(self, total_tasks: int) -> None:
        """Indicates that a new migration progress has begun."""
        self.env_migration_status = (0, total_tasks)
        print("Starting Environment migration")

    def update_env_migration_progress(self, num_tasks: int) -> None:
        """Update the migration progress."""
        num_migrations, total_migrations = self.env_migration_status
        num_migrations += num_tasks
        self.env_migration_status = (num_migrations, total_migrations)
        if num_migrations % 5 == 0:
            print(f"Environment migration Updated {num_migrations}/{total_migrations}")

    def stop_env_migration_progress(self, success: bool = True) -> None:
        """Stop the migration progress."""
        self.env_migration_status = (0, 0)
        print(f"Environment migration {'succeeded' if success else 'failed'}")


class DebuggerTerminalConsole(TerminalConsole):
    """A terminal console to use while debugging with no fluff, progress bars, etc."""

    def __init__(
        self,
        console: t.Optional[RichConsole],
        *args: t.Any,
        dialect: DialectType = None,
        ignore_warnings: bool = False,
        **kwargs: t.Any,
    ) -> None:
        self.console: RichConsole = console or srich.console
        self.dialect = dialect
        self.verbosity = Verbosity.DEFAULT
        self.ignore_warnings = ignore_warnings

    def _write(self, msg: t.Any, *args: t.Any, **kwargs: t.Any) -> None:
        self.console.log(msg, *args, **kwargs)

    def start_plan_evaluation(self, plan: EvaluatablePlan) -> None:
        self._write("Starting plan", plan.plan_id)

    def stop_plan_evaluation(self) -> None:
        self._write("Stopping plan")

    def start_evaluation_progress(
        self,
        batched_intervals: t.Dict[Snapshot, Intervals],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        audit_only: bool = False,
    ) -> None:
        message = "evaluation" if not audit_only else "auditing"
        self._write(
            f"Starting {message} for {sum(len(intervals) for intervals in batched_intervals.values())} snapshots"
        )

    def start_snapshot_evaluation_progress(
        self, snapshot: Snapshot, audit_only: bool = False
    ) -> None:
        self._write(f"{'Evaluating' if not audit_only else 'Auditing'} {snapshot.name}")

    def update_snapshot_evaluation_progress(
        self,
        snapshot: Snapshot,
        interval: Interval,
        batch_idx: int,
        duration_ms: t.Optional[int],
        num_audits_passed: int,
        num_audits_failed: int,
        audit_only: bool = False,
        execution_stats: t.Optional[QueryExecutionStats] = None,
        auto_restatement_triggers: t.Optional[t.List[SnapshotId]] = None,
    ) -> None:
        message = f"Evaluated {snapshot.name} | batch={batch_idx} | duration={duration_ms}ms | num_audits_passed={num_audits_passed} | num_audits_failed={num_audits_failed}"

        if auto_restatement_triggers:
            message += f" | auto_restatement_triggers=[{', '.join(trigger.name for trigger in auto_restatement_triggers)}]"

        if audit_only:
            message = f"Audited {snapshot.name} | duration={duration_ms}ms | num_audits_passed={num_audits_passed} | num_audits_failed={num_audits_failed}"

        self._write(message)

    def stop_evaluation_progress(self, success: bool = True) -> None:
        self._write(f"Stopping evaluation with success={success}")

    def start_creation_progress(
        self,
        snapshots: t.List[Snapshot],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        self._write(f"Starting creation for {len(snapshots)} snapshots")

    def update_creation_progress(self, snapshot: SnapshotInfoLike) -> None:
        self._write(f"Creating {snapshot.name}")

    def stop_creation_progress(self, success: bool = True) -> None:
        self._write(f"Stopping creation with success={success}")

    def update_cleanup_progress(self, object_name: str) -> None:
        self._write(f"Cleaning up {object_name}")

    def start_promotion_progress(
        self,
        snapshots: t.List[SnapshotTableInfo],
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
    ) -> None:
        if snapshots:
            self._write(f"Starting promotion for {len(snapshots)} snapshots")

    def update_promotion_progress(self, snapshot: SnapshotInfoLike, promoted: bool) -> None:
        self._write(f"Promoting {snapshot.name}")

    def stop_promotion_progress(self, success: bool = True) -> None:
        self._write(f"Stopping promotion with success={success}")

    def start_snapshot_migration_progress(self, total_tasks: int) -> None:
        self._write(f"Starting migration for {total_tasks} snapshots")

    def update_snapshot_migration_progress(self, num_tasks: int) -> None:
        self._write(f"Migration {num_tasks}")

    def log_migration_status(self, success: bool = True) -> None:
        self._write(f"Migration finished with success={success}")

    def stop_snapshot_migration_progress(self, success: bool = True) -> None:
        self._write(f"Stopping snapshot migration with success={success}")

    def start_env_migration_progress(self, total_tasks: int) -> None:
        self._write(f"Starting migration for {total_tasks} environments")

    def update_env_migration_progress(self, num_tasks: int) -> None:
        self._write(f"Environment migration {num_tasks}")

    def stop_env_migration_progress(self, success: bool = True) -> None:
        self._write(f"Stopping environment migration with success={success}")

    def show_environment_difference_summary(
        self,
        context_diff: ContextDiff,
        no_diff: bool = True,
    ) -> None:
        self._write("Environment Difference Summary:")

        if context_diff.has_requirement_changes:
            self._write(f"Requirements:\n{context_diff.requirements_diff()}")

        if context_diff.has_environment_statements_changes and not no_diff:
            self._write("Environment statements:\n")
            for _, diff in context_diff.environment_statements_diff(
                include_python_env=not context_diff.is_new_environment
            ):
                self._write(diff)

    def show_model_difference_summary(
        self,
        context_diff: ContextDiff,
        environment_naming_info: EnvironmentNamingInfo,
        default_catalog: t.Optional[str],
        no_diff: bool = True,
    ) -> None:
        self._write("Model Difference Summary:")

        for added in context_diff.new_snapshots:
            self._write(f"  Added: {added}")
        for removed in context_diff.removed_snapshots:
            self._write(f"  Removed: {removed}")
        for modified in context_diff.modified_snapshots:
            self._write(f"  Modified: {modified}")

    def log_test_results(self, result: ModelTextTestResult, target_dialect: str) -> None:
        self._write("Test Results:", result)

    def show_sql(self, sql: str) -> None:
        self._write(sql)

    def log_status_update(self, message: str) -> None:
        self._write(message, style="bold blue")

    def log_error(self, message: str) -> None:
        self._write(message, style="bold red")

    def log_warning(self, short_message: str, long_message: t.Optional[str] = None) -> None:
        logger.warning(long_message or short_message)
        if not self.ignore_warnings:
            self._write(short_message, style="bold yellow")

    def log_success(self, message: str) -> None:
        self._write(message, style="bold green")

    def loading_start(self, message: t.Optional[str] = None) -> uuid.UUID:
        self._write(message)
        return uuid.uuid4()

    def loading_stop(self, id: uuid.UUID) -> None:
        self._write("Done")

    def show_schema_diff(self, schema_diff: SchemaDiff) -> None:
        self._write(schema_diff)

    def show_row_diff(
        self, row_diff: RowDiff, show_sample: bool = True, skip_grain_check: bool = False
    ) -> None:
        self._write(row_diff)

    def show_table_diff(
        self,
        table_diffs: t.List[TableDiff],
        show_sample: bool = True,
        skip_grain_check: bool = False,
        temp_schema: t.Optional[str] = None,
    ) -> None:
        for table_diff in table_diffs:
            self.show_table_diff_summary(table_diff)
            self.show_schema_diff(table_diff.schema_diff())
            self.show_row_diff(
                table_diff.row_diff(temp_schema=temp_schema, skip_grain_check=skip_grain_check),
                show_sample=show_sample,
                skip_grain_check=skip_grain_check,
            )

    def update_table_diff_progress(self, model: str) -> None:
        self._write(f"Finished table diff for: {model}")

    def start_table_diff_progress(self, models_to_diff: int) -> None:
        self._write("Table diff started")

    def start_table_diff_model_progress(self, model: str) -> None:
        self._write(f"Calculating differences for: {model}")

    def stop_table_diff_progress(self, success: bool) -> None:
        self._write(f"Table diff finished with success={success}")

    def show_table_diff_details(
        self,
        models_to_diff: t.List[str],
    ) -> None:
        if models_to_diff:
            models = "\n".join(models_to_diff)
            self._write(f"Models to compare: {models}")

    def show_table_diff_summary(self, table_diff: TableDiff) -> None:
        if table_diff.model_name:
            self._write(f"Model: {table_diff.model_name}")
            self._write(f"Source env: {table_diff.source_alias}")
            self._write(f"Target env: {table_diff.target_alias}")
        self._write(f"Source table: {table_diff.source}")
        self._write(f"Target table: {table_diff.target}")
        _, _, key_column_names = table_diff.key_columns
        keys = ", ".join(key_column_names)
        self._write(f"Join On: {keys}")


_CONSOLE: Console = NoopConsole()


def set_console(console: Console) -> None:
    """Sets the console instance."""
    global _CONSOLE
    _CONSOLE = console


def configure_console(**kwargs: t.Any) -> None:
    """Configures the console instance."""
    global _CONSOLE
    _CONSOLE = create_console(**kwargs)


def get_console() -> Console:
    """Returns the console instance or creates a new one if it hasn't been created yet."""
    return _CONSOLE


def create_console(
    **kwargs: t.Any,
) -> TerminalConsole | DatabricksMagicConsole | NotebookMagicConsole:
    """
    Creates a new console instance that is appropriate for the current runtime environment.

    Note: Google Colab environment is untested and currently assumes is compatible with the base
    NotebookMagicConsole.
    """
    from sqlmesh import RuntimeEnv

    runtime_env = RuntimeEnv.get()

    runtime_env_mapping = {
        RuntimeEnv.DATABRICKS: DatabricksMagicConsole,
        RuntimeEnv.JUPYTER: NotebookMagicConsole,
        RuntimeEnv.TERMINAL: TerminalConsole,
        RuntimeEnv.GOOGLE_COLAB: NotebookMagicConsole,
        RuntimeEnv.DEBUGGER: DebuggerTerminalConsole,
        RuntimeEnv.CI: MarkdownConsole,
    }
    rich_console_kwargs: t.Dict[str, t.Any] = {"theme": srich.theme}
    if runtime_env.is_jupyter or runtime_env.is_google_colab:
        rich_console_kwargs["force_jupyter"] = True
    return runtime_env_mapping[runtime_env](
        **{**{"console": RichConsole(**rich_console_kwargs)}, **kwargs}
    )


def _format_missing_intervals(snapshot: Snapshot, missing: SnapshotIntervals) -> str:
    return (
        missing.format_intervals(snapshot.node.interval_unit)
        if snapshot.is_incremental
        else "recreate view"
        if snapshot.is_view
        else "full refresh"
    )


def _format_node_errors(errors: t.List[NodeExecutionFailedError]) -> t.Dict[str, str]:
    """Formats a list of node execution errors for display."""

    def _format_node_error(ex: NodeExecutionFailedError) -> str:
        cause = ex.__cause__ if ex.__cause__ else ex

        error_msg = str(cause)

        if isinstance(cause, NodeAuditsErrors):
            error_msg = _format_audits_errors(cause)
        elif not isinstance(cause, (NodeExecutionFailedError, PythonModelEvalError)):
            error_msg = "  " + error_msg.replace("\n", "\n  ")
            error_msg = (
                f"  {cause.__class__.__name__}:\n{error_msg}"  # include error class name in msg
            )
        error_msg = error_msg.replace("\n", "\n  ")
        error_msg = error_msg + "\n" if not error_msg.rstrip(" ").endswith("\n") else error_msg

        return error_msg

    error_messages = {}

    num_fails = len(errors)
    for i, error in enumerate(errors):
        node_name = ""
        if isinstance(error.node, SnapshotId):
            node_name = error.node.name
        elif hasattr(error.node, "snapshot_name"):
            node_name = error.node.snapshot_name

        msg = _format_node_error(error)
        msg = "  " + msg.replace("\n", "\n  ")
        if i == (num_fails - 1):
            msg = msg if msg.rstrip(" ").endswith("\n") else msg + "\n"

        error_messages[node_name] = msg

    return error_messages


def _format_audits_errors(error: NodeAuditsErrors) -> str:
    error_messages = []
    for err in error.errors:
        audit_args_sql = []
        for arg_name, arg_value in err.audit_args.items():
            audit_args_sql.append(f"{arg_name} := {arg_value.sql(dialect=err.adapter_dialect)}")
        audit_args_sql_msg = ("\n".join(audit_args_sql) + "\n\n") if audit_args_sql else ""

        err_msg = f"'{err.audit_name}' audit error: {err.count} {'row' if err.count == 1 else 'rows'} failed"

        query = "\n  ".join(textwrap.wrap(err.sql(err.adapter_dialect), width=LINE_WRAP_WIDTH))
        msg = f"{err_msg}\n\nAudit arguments\n  {audit_args_sql_msg}Audit query\n  {query}\n\n"
        msg = msg.replace("\n", "\n  ")
        error_messages.append(msg)
    return "  " + "\n".join(error_messages)


def _format_interval(snapshot: Snapshot, interval: Interval) -> str:
    """Format an interval with an optional prefix."""
    inclusive_interval = make_inclusive(interval[0], interval[1])
    if snapshot.model.interval_unit.is_date_granularity:
        return f"{to_ds(inclusive_interval[0])} - {to_ds(inclusive_interval[1])}"

    if inclusive_interval[0].date() == inclusive_interval[1].date():
        # omit end date if interval start/end on same day
        return f"{to_ds(inclusive_interval[0])} {inclusive_interval[0].strftime('%H:%M:%S')}-{inclusive_interval[1].strftime('%H:%M:%S')}"

    return f"{inclusive_interval[0].strftime('%Y-%m-%d %H:%M:%S')} - {inclusive_interval[1].strftime('%Y-%m-%d %H:%M:%S')}"


def _format_signal_interval(snapshot: Snapshot, interval: Interval) -> str:
    """Format an interval for signal output (without 'insert' prefix)."""
    return _format_interval(snapshot, interval)


def _format_evaluation_model_interval(snapshot: Snapshot, interval: Interval) -> str:
    """Format an interval for evaluation output (with 'insert' prefix)."""
    if snapshot.is_model and (
        snapshot.model.kind.is_incremental
        or snapshot.model.kind.is_managed
        or snapshot.model.kind.is_custom
    ):
        formatted_interval = _format_interval(snapshot, interval)
        return f"insert {formatted_interval}"

    return ""


def _create_evaluation_model_annotation(
    snapshot: Snapshot,
    interval_info: t.Optional[str],
    execution_stats: t.Optional[QueryExecutionStats],
) -> str:
    annotation = None
    execution_stats_str = ""
    if execution_stats:
        rows_processed = execution_stats.total_rows_processed
        if rows_processed:
            # 1.00 and 1.0 to 1
            rows_processed_str = metric(rows_processed).replace(".00", "").replace(".0", "")
            execution_stats_str += f"{rows_processed_str} row{'s' if rows_processed > 1 else ''}"

        bytes_processed = execution_stats.total_bytes_processed
        execution_stats_str += (
            f"{', ' if execution_stats_str else ''}{naturalsize(bytes_processed, binary=True)}"
            if bytes_processed
            else ""
        )
    execution_stats_str = f" ({execution_stats_str})" if execution_stats_str else ""

    if snapshot.is_audit:
        annotation = "run standalone audit"
    if snapshot.is_model:
        if snapshot.model.kind.is_external:
            annotation = "run external audits"
        if snapshot.model.kind.is_view:
            annotation = "recreate view"
        if snapshot.model.kind.is_seed:
            annotation = f"insert seed file{execution_stats_str}"
        if snapshot.model.kind.is_full:
            annotation = f"full refresh{execution_stats_str}"
        if snapshot.model.kind.is_incremental_by_unique_key:
            annotation = f"insert/update rows{execution_stats_str}"
        if snapshot.model.kind.is_incremental_by_partition:
            annotation = f"insert partitions{execution_stats_str}"

    if annotation:
        return annotation

    return f"{interval_info}{execution_stats_str}" if interval_info else ""


def _calculate_interval_str_len(
    snapshot: Snapshot,
    intervals: t.List[Interval],
    execution_stats: t.Optional[QueryExecutionStats] = None,
) -> int:
    interval_str_len = 0
    for interval in intervals:
        interval_str_len = max(
            interval_str_len,
            len(
                _create_evaluation_model_annotation(
                    snapshot, _format_evaluation_model_interval(snapshot, interval), execution_stats
                )
            ),
        )
    return interval_str_len


def _calculate_audit_str_len(snapshot: Snapshot, audit_padding: int = 0) -> int:
    # The annotation includes audit results. We cannot build the audits result string
    # until after evaluation occurs, but we must determine the annotation column width here.
    # Therefore, we add enough padding for the longest possible audits result string.
    audit_str_len = 0
    audit_base_str_len = len(f", audits ") + 1  # +1 for check/X
    if snapshot.is_audit:
        # +1 for "1" audit count, +1 for red X
        audit_str_len = max(
            audit_str_len, audit_base_str_len + (2 if not snapshot.audit.blocking else 1)
        )
    if snapshot.is_model and snapshot.model.audits:
        num_audits = len(snapshot.model.audits_with_args)
        num_nonblocking_audits = sum(
            1
            for audit in snapshot.model.audits_with_args
            if not audit[0].blocking
            or ("blocking" in audit[1] and audit[1]["blocking"] == exp.false())
        )
        if num_audits == 1:
            # +1 for "1" audit count, +1 for red X
            # if audit_padding is > 0 we're using "failed" instead of red X
            audit_len = (
                audit_base_str_len
                + (2 if num_nonblocking_audits else 1)
                + (
                    audit_padding - 1
                    if num_nonblocking_audits and audit_padding > 0
                    else audit_padding
                )
            )
        else:
            audit_len = audit_base_str_len + len(str(num_audits)) + audit_padding
            if num_nonblocking_audits:
                # +1 for space, +1 for red X
                # if audit_padding is > 0 we're using "failed" instead of red X
                audit_len += (
                    len(str(num_nonblocking_audits))
                    + 2
                    + (audit_padding - 1 if audit_padding > 0 else audit_padding)
                )
        audit_str_len = max(audit_str_len, audit_len)
    return audit_str_len


def _calculate_annotation_str_len(
    batched_intervals: t.Dict[Snapshot, t.List[Interval]],
    audit_padding: int = 0,
    execution_stats_len: int = 0,
) -> int:
    annotation_str_len = 0
    for snapshot, intervals in batched_intervals.items():
        annotation_str_len = max(
            annotation_str_len,
            _calculate_interval_str_len(snapshot, intervals)
            + _calculate_audit_str_len(snapshot, audit_padding)
            + execution_stats_len,
        )
    return annotation_str_len
