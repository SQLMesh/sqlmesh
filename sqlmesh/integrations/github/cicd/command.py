from __future__ import annotations

import logging
import traceback

import click

from sqlmesh.core.analytics import cli_analytics
from sqlmesh.core.console import set_console, MarkdownConsole
from sqlmesh.integrations.github.cicd.controller import (
    GithubCheckConclusion,
    GithubCheckStatus,
    GithubController,
    TestFailure,
)
from sqlmesh.utils.errors import CICDBotError, ConflictingPlanError, PlanError, LinterError

logger = logging.getLogger(__name__)


@click.group(no_args_is_help=True)
@click.option(
    "--token",
    type=str,
    envvar="GITHUB_TOKEN",
    help="The Github Token to be used. Pass in `${{ secrets.GITHUB_TOKEN }}` if you want to use the one created by Github actions",
)
@click.option(
    "--full-logs",
    is_flag=True,
    help="Whether to print all logs in the Github Actions output or only in their relevant GA check",
)
@click.pass_context
def github(ctx: click.Context, token: str, full_logs: bool = False) -> None:
    """Github Action CI/CD Bot. See https://sqlmesh.readthedocs.io/en/stable/integrations/github/ for details"""
    # set a larger width because if none is specified, it auto-detects 80 characters when running in GitHub Actions
    # which can result in surprise newlines when outputting dates to backfill
    set_console(
        MarkdownConsole(
            width=1000, warning_capture_only=not full_logs, error_capture_only=not full_logs
        )
    )
    ctx.obj["github"] = GithubController(
        paths=ctx.obj["paths"],
        token=token,
        config=ctx.obj["config"],
    )


def _check_required_approvers(controller: GithubController) -> bool:
    controller.update_required_approval_check(status=GithubCheckStatus.IN_PROGRESS)
    if controller.has_required_approval:
        controller.update_required_approval_check(
            status=GithubCheckStatus.COMPLETED, conclusion=GithubCheckConclusion.SUCCESS
        )
        return True
    controller.update_required_approval_check(
        status=GithubCheckStatus.COMPLETED, conclusion=GithubCheckConclusion.FAILURE
    )
    return False


@github.command()
@click.pass_context
@cli_analytics
def check_required_approvers(ctx: click.Context) -> None:
    """Checks if a required approver has provided approval on the PR."""
    if not _check_required_approvers(ctx.obj["github"]):
        raise CICDBotError(
            "Required approver has not approved the PR. See Pull Requests Checks for more information."
        )


def _run_tests(controller: GithubController) -> bool:
    controller.update_test_check(status=GithubCheckStatus.IN_PROGRESS)
    try:
        result, _ = controller.run_tests()
        controller.update_test_check(
            status=GithubCheckStatus.COMPLETED,
            # Conclusion will be updated with final status based on test results
            conclusion=GithubCheckConclusion.NEUTRAL,
            result=result,
        )
        return result.wasSuccessful()
    except Exception:
        controller.update_test_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.FAILURE,
            traceback=traceback.format_exc(),
        )
        return False


def _run_linter(controller: GithubController) -> bool:
    controller.update_linter_check(status=GithubCheckStatus.IN_PROGRESS)
    try:
        controller.run_linter()
    except LinterError:
        controller.update_linter_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.FAILURE,
        )
        return False

    controller.update_linter_check(
        status=GithubCheckStatus.COMPLETED,
        conclusion=GithubCheckConclusion.SUCCESS,
    )

    return True


@github.command()
@click.pass_context
@cli_analytics
def run_tests(ctx: click.Context) -> None:
    """Runs the unit tests"""
    if not _run_tests(ctx.obj["github"]):
        raise CICDBotError("Failed to run tests. See Pull Requests Checks for more information.")


def _update_pr_environment(controller: GithubController) -> bool:
    controller.update_pr_environment_check(status=GithubCheckStatus.IN_PROGRESS)
    try:
        controller.update_pr_environment()
        conclusion = controller.update_pr_environment_check(status=GithubCheckStatus.COMPLETED)
        return conclusion is not None and conclusion.is_success
    except Exception as e:
        logger.exception("Error occurred when updating PR environment")
        conclusion = controller.update_pr_environment_check(
            status=GithubCheckStatus.COMPLETED, exception=e
        )
        return (
            conclusion is not None
            and not conclusion.is_failure
            and not conclusion.is_action_required
        )


@github.command()
@click.pass_context
@cli_analytics
def update_pr_environment(ctx: click.Context) -> None:
    """Creates or updates the PR environments"""
    if not _update_pr_environment(ctx.obj["github"]):
        raise CICDBotError(
            "Failed to update PR environment. See Pull Requests Checks for more information."
        )


def _gen_prod_plan(controller: GithubController) -> bool:
    controller.update_prod_plan_preview_check(status=GithubCheckStatus.IN_PROGRESS)
    try:
        plan_summary = controller.get_plan_summary(controller.prod_plan)
        controller.update_prod_plan_preview_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.SUCCESS,
            summary=plan_summary,
        )
        return bool(plan_summary)
    except Exception as e:
        logger.exception("Error occurred generating prod plan")
        controller.update_prod_plan_preview_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.FAILURE,
            summary=str(e),
        )
        return False


@github.command()
@click.pass_context
@cli_analytics
def gen_prod_plan(ctx: click.Context) -> None:
    """Generates the production plan"""
    controller = ctx.obj["github"]
    controller.update_prod_plan_preview_check(status=GithubCheckStatus.IN_PROGRESS)
    if not _gen_prod_plan(controller):
        raise CICDBotError(
            "Failed to generate production plan. See Pull Requests Checks for more information."
        )


def _deploy_production(controller: GithubController) -> bool:
    controller.update_prod_environment_check(status=GithubCheckStatus.IN_PROGRESS)
    try:
        controller.deploy_to_prod()
        controller.update_prod_environment_check(
            status=GithubCheckStatus.COMPLETED, conclusion=GithubCheckConclusion.SUCCESS
        )
        controller.try_merge_pr()
        controller.try_invalidate_pr_environment()
        return True
    except ConflictingPlanError as e:
        controller.update_prod_environment_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.SKIPPED,
            skip_reason=str(e),
        )
        return False
    except PlanError as e:
        controller.update_prod_environment_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.ACTION_REQUIRED,
            plan_error=e,
        )
        return False
    except Exception:
        controller.update_prod_environment_check(
            status=GithubCheckStatus.COMPLETED, conclusion=GithubCheckConclusion.FAILURE
        )
        return False


@github.command()
@click.pass_context
@cli_analytics
def deploy_production(ctx: click.Context) -> None:
    """Deploys the production environment"""
    if not _deploy_production(ctx.obj["github"]):
        raise CICDBotError(
            "Failed to deploy to production. See Pull Requests Checks for more information."
        )


def _run_all(controller: GithubController) -> None:
    click.echo(f"SQLMesh Version: {controller.version_info}")

    has_required_approval = False
    is_auto_deploying_prod = (
        controller.deploy_command_enabled or controller.do_required_approval_check
    ) and controller.pr_targets_prod_branch
    if controller.is_comment_added:
        if not controller.deploy_command_enabled:
            # We aren't using commands so we can just return
            return
        command = controller.get_command_from_comment()
        if command.is_invalid:
            # Probably a comment unrelated to SQLMesh so we do nothing
            return
        if command.is_deploy_prod:
            has_required_approval = True
        else:
            raise CICDBotError(f"Unsupported command: {command}")
    controller.update_linter_check(status=GithubCheckStatus.QUEUED)
    controller.update_pr_environment_check(status=GithubCheckStatus.QUEUED)
    controller.update_prod_plan_preview_check(status=GithubCheckStatus.QUEUED)
    controller.update_test_check(status=GithubCheckStatus.QUEUED)
    if is_auto_deploying_prod:
        controller.update_prod_environment_check(status=GithubCheckStatus.QUEUED)
    linter_passed = _run_linter(controller)
    tests_passed = _run_tests(controller)
    if controller.do_required_approval_check:
        if has_required_approval:
            controller.update_required_approval_check(
                status=GithubCheckStatus.COMPLETED, conclusion=GithubCheckConclusion.SKIPPED
            )
        else:
            controller.update_required_approval_check(status=GithubCheckStatus.QUEUED)
            has_required_approval = _check_required_approvers(controller)
    if not tests_passed or not linter_passed:
        controller.update_pr_environment_check(
            status=GithubCheckStatus.COMPLETED,
            exception=LinterError("") if not linter_passed else TestFailure(),
        )
        controller.update_prod_plan_preview_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.SKIPPED,
            summary="Linter or Unit Test(s) failed so skipping creating prod plan",
        )
        if is_auto_deploying_prod:
            controller.update_prod_environment_check(
                status=GithubCheckStatus.COMPLETED,
                conclusion=GithubCheckConclusion.SKIPPED,
                skip_reason="Linter or Unit Test(s) failed so skipping deploying to production",
            )

        raise CICDBotError(
            "Linter or Unit Test(s) failed. See Pull Requests Checks for more information."
        )

    pr_environment_updated = _update_pr_environment(controller)
    prod_plan_generated = False
    if pr_environment_updated:
        prod_plan_generated = _gen_prod_plan(controller)
    else:
        controller.update_prod_plan_preview_check(
            status=GithubCheckStatus.COMPLETED, conclusion=GithubCheckConclusion.SKIPPED
        )
    deployed_to_prod = False
    if has_required_approval and prod_plan_generated and controller.pr_targets_prod_branch:
        deployed_to_prod = _deploy_production(controller)
    elif is_auto_deploying_prod:
        if controller.deploy_command_enabled and not has_required_approval:
            skip_reason = "Skipped Deploying to Production because a `/deploy` command has not been detected yet"
        elif controller.do_required_approval_check and not has_required_approval:
            skip_reason = (
                "Skipped Deploying to Production because a required approver has not approved"
            )
        elif not pr_environment_updated:
            skip_reason = (
                "Skipped Deploying to Production because the PR environment was not updated"
            )
        elif not prod_plan_generated:
            skip_reason = (
                "Skipped Deploying to Production because the production plan could not be generated"
            )
        else:
            skip_reason = "Skipped Deploying to Production for an unknown reason"
        controller.update_prod_environment_check(
            status=GithubCheckStatus.COMPLETED,
            conclusion=GithubCheckConclusion.SKIPPED,
            skip_reason=skip_reason,
        )
    if (
        not pr_environment_updated
        or not prod_plan_generated
        or (has_required_approval and controller.pr_targets_prod_branch and not deployed_to_prod)
    ):
        raise CICDBotError(
            "A step of the run-all check failed. See Pull Requests Checks for more information."
        )


@github.command()
@click.pass_context
@cli_analytics
def run_all(ctx: click.Context) -> None:
    """Runs all the commands in the correct order."""
    return _run_all(ctx.obj["github"])
