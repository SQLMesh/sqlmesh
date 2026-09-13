from __future__ import annotations

import abc
import typing as t
import logging
from dataclasses import dataclass
from collections import defaultdict

from rich.console import Console as RichConsole
from rich.tree import Tree
from sqlglot import exp
from sqlglot.dialects.dialect import DialectType
from sqlmesh.core import constants as c
from sqlmesh.core.console import Console, TerminalConsole, get_console
from sqlmesh.core.dialect import schema_
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.plan.common import (
    SnapshotIntervalClearRequest,
    identify_restatement_intervals_across_snapshot_versions,
)
from sqlmesh.core.plan.definition import EvaluatablePlan, SnapshotIntervals
from sqlmesh.core.plan import stages
from sqlmesh.core.plan.evaluator import (
    PlanEvaluator,
)
from sqlmesh.core.state_sync import StateReader
from sqlmesh.core.snapshot.definition import (
    SnapshotInfoMixin,
    SnapshotIdAndVersion,
    model_display_name,
)
from sqlmesh.utils import Verbosity, rich as srich, to_snake_case
from sqlmesh.utils.date import to_ts
from sqlmesh.utils.errors import SQLMeshError


logger = logging.getLogger(__name__)


class PlanExplainer(PlanEvaluator):
    def __init__(
        self,
        state_reader: StateReader,
        default_catalog: t.Optional[str],
        console: t.Optional[Console] = None,
    ):
        self.state_reader = state_reader
        self.default_catalog = default_catalog
        self.console = console or get_console()

    def evaluate(
        self,
        plan: EvaluatablePlan,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
    ) -> None:
        plan_stages = stages.build_plan_stages(plan, self.state_reader, self.default_catalog)
        explainer_console = _get_explainer_console(
            self.console, plan.environment, self.default_catalog
        )

        # add extra metadata that's only needed at this point for better --explain output
        plan_stages = [
            ExplainableRestatementStage.from_restatement_stage(stage, self.state_reader, plan)
            if isinstance(stage, stages.RestatementStage)
            else stage
            for stage in plan_stages
        ]

        explainer_console.explain(plan_stages)


class ExplainerConsole(abc.ABC):
    @abc.abstractmethod
    def explain(self, stages: t.List[stages.PlanStage]) -> None:
        pass


@dataclass
class ExplainableRestatementStage(stages.RestatementStage):
    """
    This brings forward some calculations that would usually be done in the evaluator so the user can be given a better indication
    of what might happen when they ask for the plan to be explained
    """

    snapshot_intervals_to_clear: t.Dict[str, t.List[SnapshotIntervalClearRequest]]
    """Which snapshots from other environments would have intervals cleared as part of restatement, grouped by name."""

    @classmethod
    def from_restatement_stage(
        cls: t.Type[ExplainableRestatementStage],
        stage: stages.RestatementStage,
        state_reader: StateReader,
        plan: EvaluatablePlan,
    ) -> ExplainableRestatementStage:
        all_restatement_intervals = identify_restatement_intervals_across_snapshot_versions(
            state_reader=state_reader,
            prod_restatements=plan.restatements,
            disable_restatement_models=plan.disabled_restatement_models,
            loaded_snapshots={s.snapshot_id: s for s in stage.all_snapshots.values()},
        )

        # Group the interval clear requests by snapshot name to make them easier to write to the console
        snapshot_intervals_to_clear = defaultdict(list)
        for clear_request in all_restatement_intervals.values():
            snapshot_intervals_to_clear[clear_request.snapshot.name].append(clear_request)

        return cls(
            snapshot_intervals_to_clear=snapshot_intervals_to_clear,
            all_snapshots=stage.all_snapshots,
        )


MAX_TREE_LENGTH = 10


class RichExplainerConsole(ExplainerConsole):
    def __init__(
        self,
        environment_naming_info: EnvironmentNamingInfo,
        dialect: DialectType,
        default_catalog: t.Optional[str],
        verbosity: Verbosity = Verbosity.DEFAULT,
        console: t.Optional[RichConsole] = None,
    ):
        self.environment_naming_info = environment_naming_info
        self.dialect = dialect
        self.default_catalog = default_catalog
        self.verbosity = verbosity
        self.console: RichConsole = console or srich.console

    def explain(self, stages: t.List[stages.PlanStage]) -> None:
        tree = Tree("[bold]Explained plan[/bold]")
        for stage in stages:
            handler_name = f"visit_{to_snake_case(stage.__class__.__name__)}"
            if not hasattr(self, handler_name):
                logger.error("Unexpected stage: %s", stage.__class__.__name__)
                continue
            handler = getattr(self, handler_name)
            result = handler(stage)
            if result:
                tree.add(self._limit_tree(result))
        self.console.print(tree)

    def visit_before_all_stage(self, stage: stages.BeforeAllStage) -> Tree:
        return Tree("[bold]Execute before all statements[/bold]")

    def visit_after_all_stage(self, stage: stages.AfterAllStage) -> Tree:
        return Tree("[bold]Execute after all statements[/bold]")

    def visit_physical_layer_schema_creation_stage(
        self, stage: stages.PhysicalLayerSchemaCreationStage
    ) -> t.Optional[Tree]:
        tables = [
            exp.to_table(
                snapshot.table_name(is_deployable=stage.deployability_index.is_deployable(snapshot))
            )
            for snapshot in stage.snapshots
            if snapshot.is_model and not snapshot.is_symbolic
        ]
        schemas = {
            schema_(table.args["db"], table.args.get("catalog")).sql(dialect=self.dialect)
            for table in tables
            if table.db
        }
        if not schemas:
            return None

        tree = Tree("[bold]Create physical schemas if they do not exist[/bold]")
        for schema in sorted(schemas):
            tree.add(schema)
        return tree

    def visit_physical_layer_update_stage(self, stage: stages.PhysicalLayerUpdateStage) -> Tree:
        snapshots = [
            s for s in stage.snapshots if s.snapshot_id in stage.snapshots_with_missing_intervals
        ]
        if not snapshots:
            return Tree("[bold]SKIP: No physical layer updates to perform[/bold]")

        tree = Tree(
            "[bold]Validate SQL and create physical layer tables and views if they do not exist[/bold]"
        )
        for snapshot in snapshots:
            is_deployable = (
                stage.deployability_index.is_deployable(snapshot)
                if self.environment_naming_info.name != c.PROD
                else True
            )
            display_name = self._display_name(snapshot)
            table_name = snapshot.table_name(is_deployable)
            model_tree = Tree(f"{display_name} -> {table_name}")

            if snapshot.is_model:
                if snapshot.model.pre_statements:
                    model_tree.add("Run pre-statements")
                if snapshot.model.annotated:
                    model_tree.add("Dry run model query without inserting results")

            if snapshot.is_view:
                create_tree = Tree("Create view if it doesn't exist")
            elif (
                snapshot.is_forward_only and snapshot.previous_versions and not snapshot.is_managed
            ):
                prod_table = snapshot.table_name(True)
                create_tree = Tree(
                    f"Clone {prod_table} into {table_name} and then update its schema if it doesn't exist"
                )
            else:
                create_tree = Tree("Create table if it doesn't exist")

            if not is_deployable:
                create_tree.add("[orange1]preview[/orange1]: data will NOT be reused in production")
            model_tree.add(create_tree)

            if snapshot.is_model and snapshot.model.post_statements:
                model_tree.add("Run post-statements")

            tree.add(model_tree)
        return tree

    def visit_audit_only_run_stage(self, stage: stages.AuditOnlyRunStage) -> Tree:
        tree = Tree("[bold]Audit-only execution[/bold]")
        for snapshot in stage.snapshots:
            display_name = self._display_name(snapshot)
            tree.add(display_name)
        return tree

    def visit_explainable_restatement_stage(self, stage: ExplainableRestatementStage) -> Tree:
        return self.visit_restatement_stage(stage)

    def visit_restatement_stage(
        self, stage: t.Union[ExplainableRestatementStage, stages.RestatementStage]
    ) -> Tree:
        tree = Tree(
            "[bold]Invalidate data intervals in state for development environments to prevent old data from being promoted[/bold]\n"
            "This only affects state and will not clear physical data from the tables until the next plan for each environment"
        )

        if isinstance(stage, ExplainableRestatementStage) and (
            snapshot_intervals := stage.snapshot_intervals_to_clear
        ):
            for name, clear_requests in snapshot_intervals.items():
                display_name = model_display_name(
                    name, self.environment_naming_info, self.default_catalog, self.dialect
                )
                interval_start = min(cr.interval[0] for cr in clear_requests)
                interval_end = max(cr.interval[1] for cr in clear_requests)

                if not interval_start or not interval_end:
                    continue

                node = tree.add(f"{display_name} [{to_ts(interval_start)} - {to_ts(interval_end)}]")

                all_environment_names = sorted(
                    set(env_name for cr in clear_requests for env_name in cr.environment_names)
                )
                node.add("in environments: " + ", ".join(all_environment_names))

        return tree

    def visit_backfill_stage(self, stage: stages.BackfillStage) -> Tree:
        if not stage.snapshot_to_intervals:
            return Tree("[bold]SKIP: No model batches to execute[/bold]")

        tree = Tree(
            "[bold]Backfill models by running their queries and run standalone audits[/bold]"
        )
        for snapshot, intervals in stage.snapshot_to_intervals.items():
            display_name = self._display_name(snapshot)
            if snapshot.is_model:
                is_deployable = stage.deployability_index.is_deployable(snapshot)
                table_name = snapshot.table_name(is_deployable)
                model_tree = Tree(f"{display_name} -> {table_name}")

                for signal_name, _ in snapshot.model.signals:
                    model_tree.add(f"Check '{signal_name}' signal")

                if snapshot.model.pre_statements:
                    model_tree.add("Run pre-statements")

                backfill_tree = Tree("Fully refresh table")
                if snapshot.is_incremental:
                    current_intervals = (
                        snapshot.intervals
                        if stage.deployability_index.is_deployable(snapshot)
                        else snapshot.dev_intervals
                    )
                    # If there are no intervals, the table will be fully refreshed
                    if current_intervals:
                        formatted_range = SnapshotIntervals(
                            snapshot_id=snapshot.snapshot_id, intervals=intervals
                        ).format_intervals(snapshot.node.interval_unit)
                        backfill_tree = Tree(
                            f"Incrementally insert records within the range [{formatted_range}]"
                        )
                elif snapshot.is_view:
                    backfill_tree = Tree("Recreate view")

                if not is_deployable:
                    backfill_tree.add(
                        "[orange1]preview[/orange1]: data will NOT be reused in production"
                    )

                model_tree.add(backfill_tree)

                if snapshot.model.post_statements:
                    model_tree.add("Run post-statements")

                if snapshot.model.audits:
                    for audit_name, _ in snapshot.model.audits:
                        model_tree.add(f"Run '{audit_name}' audit")

                tree.add(model_tree)
            else:
                tree.add(f"{display_name} \\[standalone audit]")
        return tree

    def visit_migrate_schemas_stage(self, stage: stages.MigrateSchemasStage) -> Tree:
        tree = Tree(
            "[bold]Update schemas (add, drop, alter columns) of production physical tables to reflect forward-only changes[/bold]"
        )
        for snapshot in stage.snapshots:
            display_name = self._display_name(snapshot)
            table_name = snapshot.table_name(True)
            tree.add(f"{display_name} -> {table_name}")
        return tree

    def visit_virtual_layer_update_stage(self, stage: stages.VirtualLayerUpdateStage) -> Tree:
        tree = Tree(
            f"[bold]Update the virtual layer for environment '{self.environment_naming_info.name}'[/bold]"
        )
        promote_tree = Tree(
            "[bold]Create or update views in the virtual layer to point at new physical tables and views[/bold]"
        )
        for snapshot in stage.promoted_snapshots:
            display_name = self._display_name(snapshot)
            table_name = snapshot.table_name(stage.deployability_index.is_representative(snapshot))
            promote_tree.add(f"{display_name} -> {table_name}")

        demote_tree = Tree(
            "[bold]Delete views in the virtual layer for models that were removed[/bold]"
        )
        for snapshot in stage.demoted_snapshots:
            display_name = self._display_name(snapshot, stage.demoted_environment_naming_info)
            demote_tree.add(display_name)

        if stage.promoted_snapshots:
            tree.add(self._limit_tree(promote_tree))
        if stage.demoted_snapshots:
            tree.add(self._limit_tree(demote_tree))
        return tree

    def visit_create_snapshot_records_stage(
        self, stage: stages.CreateSnapshotRecordsStage
    ) -> t.Optional[Tree]:
        return None

    def visit_environment_record_update_stage(
        self, stage: stages.EnvironmentRecordUpdateStage
    ) -> t.Optional[Tree]:
        return None

    def visit_unpause_stage(self, stage: stages.UnpauseStage) -> t.Optional[Tree]:
        return None

    def visit_finalize_environment_stage(
        self, stage: stages.FinalizeEnvironmentStage
    ) -> t.Optional[Tree]:
        return None

    def _display_name(
        self,
        snapshot: t.Union[SnapshotInfoMixin, SnapshotIdAndVersion],
        environment_naming_info: t.Optional[EnvironmentNamingInfo] = None,
    ) -> str:
        return snapshot.display_name(
            environment_naming_info=environment_naming_info or self.environment_naming_info,
            default_catalog=self.default_catalog
            if self.verbosity < Verbosity.VERY_VERBOSE
            else None,
            dialect=self.dialect,
        )

    def _limit_tree(self, tree: Tree) -> Tree:
        tree_length = len(tree.children)
        if tree_length <= MAX_TREE_LENGTH:
            return tree
        if self.verbosity < Verbosity.VERY_VERBOSE:
            tree.children = [
                tree.children[0],
                Tree(f".... {tree_length - 2} more ...."),
                tree.children[-1],
            ]
        return tree


def _get_explainer_console(
    console: t.Optional[Console],
    environment_naming_info: EnvironmentNamingInfo,
    default_catalog: t.Optional[str],
) -> ExplainerConsole:
    console = console or get_console()
    if not isinstance(console, TerminalConsole):
        raise SQLMeshError("Plain explaination is only supported in the terminal.")
    return RichExplainerConsole(
        environment_naming_info=environment_naming_info,
        dialect=console.dialect,
        default_catalog=default_catalog,
        verbosity=console.verbosity,
        console=console.console,
    )
