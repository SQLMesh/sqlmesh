from __future__ import annotations

import functools
import json
import logging
import os
import pathlib
import re
import traceback
import typing as t
from enum import Enum
from pathlib import Path
from dataclasses import dataclass
from functools import cached_property

import requests
from sqlglot.helper import seq_get

from sqlmesh.core import constants as c
from sqlmesh.core.console import SNAPSHOT_CHANGE_CATEGORY_STR, get_console, MarkdownConsole
from sqlmesh.core.context import Context
from sqlmesh.core.test.result import ModelTextTestResult
from sqlmesh.core.environment import Environment
from sqlmesh.core.plan import Plan, PlanBuilder, SnapshotIntervals
from sqlmesh.core.plan.definition import UserProvidedFlags
from sqlmesh.core.snapshot.definition import (
    Snapshot,
    SnapshotChangeCategory,
    SnapshotId,
    SnapshotTableInfo,
)
from sqlglot.errors import SqlglotError
from sqlmesh.core.user import User
from sqlmesh.core.config import Config
from sqlmesh.integrations.github.cicd.config import GithubCICDBotConfig
from sqlmesh.utils import word_characters_only, Verbosity
from sqlmesh.utils.date import now
from sqlmesh.utils.errors import (
    CICDBotError,
    NoChangesPlanError,
    PlanError,
    UncategorizedPlanError,
    LinterError,
    SQLMeshError,
)
from sqlmesh.utils.pydantic import PydanticModel

if t.TYPE_CHECKING:
    from github import Github
    from github.CheckRun import CheckRun
    from github.Issue import Issue
    from github.IssueComment import IssueComment
    from github.PullRequest import PullRequest
    from github.PullRequestReview import PullRequestReview
    from github.Repository import Repository

logger = logging.getLogger(__name__)


class TestFailure(Exception):
    pass


class PullRequestInfo(PydanticModel):
    """Contains information related to a pull request that can be used to construct other objects/URLs"""

    owner: str
    repo: str
    pr_number: int

    @property
    def full_repo_path(self) -> str:
        return "/".join([self.owner, self.repo])

    @classmethod
    def create_from_pull_request_url(cls, pull_request_url: str) -> PullRequestInfo:
        owner, repo, _, pr_number = pull_request_url.split("/")[-4:]
        return cls(
            owner=owner,
            repo=repo,
            pr_number=int(pr_number),
        )


class GithubCheckStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"

    @property
    def is_queued(self) -> bool:
        return self == GithubCheckStatus.QUEUED

    @property
    def is_in_progress(self) -> bool:
        return self == GithubCheckStatus.IN_PROGRESS

    @property
    def is_completed(self) -> bool:
        return self == GithubCheckStatus.COMPLETED


class GithubCheckConclusion(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    SKIPPED = "skipped"

    @property
    def is_success(self) -> bool:
        return self == GithubCheckConclusion.SUCCESS

    @property
    def is_failure(self) -> bool:
        return self == GithubCheckConclusion.FAILURE

    @property
    def is_neutral(self) -> bool:
        return self == GithubCheckConclusion.NEUTRAL

    @property
    def is_cancelled(self) -> bool:
        return self == GithubCheckConclusion.CANCELLED

    @property
    def is_timed_out(self) -> bool:
        return self == GithubCheckConclusion.TIMED_OUT

    @property
    def is_action_required(self) -> bool:
        return self == GithubCheckConclusion.ACTION_REQUIRED

    @property
    def is_skipped(self) -> bool:
        return self == GithubCheckConclusion.SKIPPED


class MergeStateStatus(str, Enum):
    """
    https://docs.github.com/en/graphql/reference/enums#mergestatestatus
    """

    BEHIND = "behind"
    BLOCKED = "blocked"
    CLEAN = "clean"
    DIRTY = "dirty"
    DRAFT = "draft"
    HAS_HOOKS = "has_hooks"
    UNKNOWN = "unknown"
    UNSTABLE = "unstable"

    @property
    def is_behind(self) -> bool:
        return self == MergeStateStatus.BEHIND

    @property
    def is_blocked(self) -> bool:
        return self == MergeStateStatus.BLOCKED

    @property
    def is_clean(self) -> bool:
        return self == MergeStateStatus.CLEAN

    @property
    def is_dirty(self) -> bool:
        return self == MergeStateStatus.DIRTY

    @property
    def is_draft(self) -> bool:
        return self == MergeStateStatus.DRAFT

    @property
    def is_has_hooks(self) -> bool:
        return self == MergeStateStatus.HAS_HOOKS

    @property
    def is_unknown(self) -> bool:
        return self == MergeStateStatus.UNKNOWN

    @property
    def is_unstable(self) -> bool:
        return self == MergeStateStatus.UNSTABLE


class BotCommand(Enum):
    INVALID = 1
    DEPLOY_PROD = 2

    @classmethod
    def from_comment_body(cls, body: str, namespace: t.Optional[str] = None) -> BotCommand:
        body = body.strip()
        namespace = namespace.strip() if namespace else ""
        input_to_command = {
            namespace + "/deploy": cls.DEPLOY_PROD,
        }
        return input_to_command.get(body, cls.INVALID)

    @property
    def is_invalid(self) -> bool:
        return self == self.INVALID

    @property
    def is_deploy_prod(self) -> bool:
        return self == self.DEPLOY_PROD


class GithubEvent:
    """
    Takes a Github Actions event payload and provides a simple interface to access
    """

    def __init__(self, payload: t.Dict[str, t.Any]) -> None:
        self.payload = payload
        self._pull_request_info: t.Optional[PullRequestInfo] = None

    @classmethod
    def from_obj(cls, obj: t.Dict[str, t.Any]) -> GithubEvent:
        return cls(payload=obj)

    @classmethod
    def from_path(cls, path: t.Union[str, pathlib.Path]) -> GithubEvent:
        with open(pathlib.Path(path), "r", encoding="utf-8") as f:
            return cls.from_obj(json.load(f))

    @classmethod
    def from_env(cls) -> GithubEvent:
        return cls.from_path(os.environ["GITHUB_EVENT_PATH"])

    @property
    def is_review(self) -> bool:
        return bool(self.payload.get("review"))

    @property
    def is_comment(self) -> bool:
        comment = self.payload.get("comment")
        if not comment:
            return False
        if not comment.get("body"):
            return False
        return True

    @property
    def is_comment_added(self) -> bool:
        return self.is_comment and self.payload.get("action") != "deleted"

    @property
    def is_pull_request(self) -> bool:
        return bool(self.payload.get("pull_request"))

    @property
    def is_pull_request_closed(self) -> bool:
        return self.is_pull_request and self.payload.get("action") == "closed"

    @property
    def pull_request_url(self) -> str:
        if self.is_review:
            return self.payload["review"]["pull_request_url"]
        if self.is_comment:
            return self.payload["issue"]["pull_request"]["url"]
        if self.is_pull_request:
            return self.payload["pull_request"]["_links"]["self"]["href"]
        raise CICDBotError("Unable to determine pull request url")

    @property
    def pull_request_info(self) -> PullRequestInfo:
        if not self._pull_request_info:
            self._pull_request_info = PullRequestInfo.create_from_pull_request_url(
                self.pull_request_url
            )
        return self._pull_request_info

    @property
    def pull_request_comment_body(self) -> t.Optional[str]:
        if self.is_comment_added:
            return self.payload["comment"]["body"]
        return None


class GithubController:
    BOT_HEADER_MSG = ":robot: **SQLMesh Bot Info** :robot:"
    MAX_BYTE_LENGTH = 65535

    def __init__(
        self,
        paths: t.Union[Path, t.Iterable[Path]],
        token: str,
        config: t.Optional[t.Union[Config, str]] = None,
        event: t.Optional[GithubEvent] = None,
        client: t.Optional[Github] = None,
        context: t.Optional[Context] = None,
    ) -> None:
        from github import Github

        logger.debug(f"Initializing GithubController with paths: {paths} and config: {config}")

        self.config = config
        self._paths = paths
        self._token = token
        self._event = event or GithubEvent.from_env()
        logger.debug(f"Github event: {json.dumps(self._event.payload)}")
        self._pr_plan_builder: t.Optional[PlanBuilder] = None
        self._prod_plan_builder: t.Optional[PlanBuilder] = None
        self._prod_plan_with_gaps_builder: t.Optional[PlanBuilder] = None
        self._check_run_mapping: t.Dict[str, CheckRun] = {}

        if not isinstance(get_console(), MarkdownConsole):
            raise CICDBotError("Console must be a markdown console.")
        self._console = t.cast(MarkdownConsole, get_console())

        from github.Consts import DEFAULT_BASE_URL
        from github.Auth import Token

        self._client: Github = client or Github(
            base_url=os.environ.get("GITHUB_API_URL", DEFAULT_BASE_URL), auth=Token(self._token)
        )

        self._repo: Repository = self._client.get_repo(
            self._event.pull_request_info.full_repo_path, lazy=True
        )
        self._pull_request: PullRequest = self._repo.get_pull(
            self._event.pull_request_info.pr_number
        )
        self._issue: Issue = self._repo.get_issue(self._event.pull_request_info.pr_number)
        self._reviews: t.Iterable[PullRequestReview] = self._pull_request.get_reviews()
        # TODO: The python module says that user names can be None and this is not currently handled
        self._approvers: t.Set[str] = {
            review.user.login or "UNKNOWN"
            for review in self._reviews
            if review.state.lower() == "approved"
        }
        logger.debug(f"Approvers: {', '.join(self._approvers)}")
        self._context: Context = context or Context(paths=self._paths, config=self.config)

        # Bot config needs the context to be initialized
        logger.debug(f"Bot config: {self.bot_config.json(indent=2)}")

    @property
    def deploy_command_enabled(self) -> bool:
        return self.bot_config.enable_deploy_command

    @property
    def is_comment_added(self) -> bool:
        return self._event.is_comment_added

    @property
    def _required_approvers(self) -> t.List[User]:
        required_approvers = [
            user
            for user in self._context.users
            if user.is_required_approver and user.github_username
        ]
        logger.debug(
            f"Required approvers: {', '.join(user.github_username for user in required_approvers if user.github_username)}"
        )
        return required_approvers

    @property
    def _required_approvers_with_approval(self) -> t.List[User]:
        return [
            user for user in self._required_approvers if user.github_username in self._approvers
        ]

    @property
    def pr_environment_name(self) -> str:
        return Environment.sanitize_name(
            "_".join(
                [
                    self.bot_config.pr_environment_name or self._event.pull_request_info.repo,
                    str(self._event.pull_request_info.pr_number),
                ]
            )
        )

    @property
    def do_required_approval_check(self) -> bool:
        """We want to skip required approval check if no users have this role"""
        do_required_approval_check = bool(self._required_approvers)
        logger.debug(f"Do required approval check: {do_required_approval_check}")
        return do_required_approval_check

    @property
    def has_required_approval(self) -> bool:
        """
        Check if the PR has a required approver.

        TODO: Allow defining requiring some number, or all, required approvers.
        """
        if not self._required_approvers or self._required_approvers_with_approval:
            logger.debug("Has required Approval")
            return True
        logger.debug("Does not have required approval")
        return False

    @property
    def pr_plan(self) -> Plan:
        if not self._pr_plan_builder:
            self._pr_plan_builder = self._context.plan_builder(
                environment=self.pr_environment_name,
                skip_tests=True,
                skip_linter=True,
                categorizer_config=self.bot_config.auto_categorize_changes,
                start=self.bot_config.default_pr_start,
                min_intervals=self.bot_config.pr_min_intervals,
                skip_backfill=self.bot_config.skip_pr_backfill,
                include_unmodified=self.bot_config.pr_include_unmodified,
                forward_only=self.forward_only_plan,
            )
        assert self._pr_plan_builder
        return self._pr_plan_builder.build()

    @property
    def pr_plan_or_none(self) -> t.Optional[Plan]:
        try:
            return self.pr_plan
        except:
            return None

    @property
    def pr_plan_flags(self) -> t.Optional[t.Dict[str, UserProvidedFlags]]:
        if pr_plan := self.pr_plan_or_none:
            return pr_plan.user_provided_flags
        if pr_plan_builder := self._pr_plan_builder:
            return pr_plan_builder._user_provided_flags
        return None

    @property
    def prod_plan(self) -> Plan:
        if not self._prod_plan_builder:
            self._prod_plan_builder = self._context.plan_builder(
                c.PROD,
                no_gaps=True,
                skip_tests=True,
                skip_linter=True,
                categorizer_config=self.bot_config.auto_categorize_changes,
                run=self.bot_config.run_on_deploy_to_prod,
                forward_only=self.forward_only_plan,
            )
        assert self._prod_plan_builder
        return self._prod_plan_builder.build()

    @property
    def prod_plan_with_gaps(self) -> Plan:
        if not self._prod_plan_with_gaps_builder:
            self._prod_plan_with_gaps_builder = self._context.plan_builder(
                c.PROD,
                # this is required to highlight any data gaps between this PR environment and prod (since PR environments may only contain a subset of data)
                no_gaps=False,
                skip_tests=True,
                skip_linter=True,
                categorizer_config=self.bot_config.auto_categorize_changes,
                run=self.bot_config.run_on_deploy_to_prod,
                forward_only=self.forward_only_plan,
            )
        assert self._prod_plan_with_gaps_builder
        return self._prod_plan_with_gaps_builder.build()

    @property
    def bot_config(self) -> GithubCICDBotConfig:
        bot_config = self._context.config.cicd_bot or GithubCICDBotConfig(
            auto_categorize_changes=self._context.auto_categorize_changes
        )
        return bot_config

    @property
    def modified_snapshots(self) -> t.Dict[SnapshotId, t.Union[Snapshot, SnapshotTableInfo]]:
        return self.prod_plan_with_gaps.modified_snapshots

    @property
    def removed_snapshots(self) -> t.Set[SnapshotId]:
        return set(self.prod_plan_with_gaps.context_diff.removed_snapshots)

    @property
    def pr_targets_prod_branch(self) -> bool:
        return self._pull_request.base.ref in self.bot_config.prod_branch_names

    @property
    def forward_only_plan(self) -> bool:
        default = self._context.config.plan.forward_only
        head_ref = self._pull_request.head.ref
        if isinstance(head_ref, str):
            return head_ref.endswith(self.bot_config.forward_only_branch_suffix) or default
        return default

    @classmethod
    def _append_output(cls, key: str, value: str) -> None:
        """
        Appends the given key/value to output so they can be read by following steps
        """
        logger.debug(f"Setting output. Key: {key}, Value: {value}")

        # GitHub Actions sets this environment variable
        if output_file := os.environ.get("GITHUB_OUTPUT"):
            with open(output_file, "a", encoding="utf-8") as fh:
                print(f"{key}={value}", file=fh)

    def get_forward_only_plan_post_deployment_tip(self, plan: Plan) -> str:
        if not plan.forward_only:
            return ""

        example_model_name = "<model name>"
        for snapshot_id in sorted(plan.snapshots):
            snapshot = plan.snapshots[snapshot_id]
            if snapshot.is_incremental:
                example_model_name = snapshot.node.name
                break

        return (
            "> [!TIP]\n"
            "> In order to see this forward-only plan retroactively apply to historical intervals on the production model, run the below for date ranges in scope:\n"
            "> \n"
            f"> `$ sqlmesh plan --restate-model {example_model_name} --start YYYY-MM-DD --end YYYY-MM-DD`\n"
            ">\n"
            "> Learn more: https://sqlmesh.readthedocs.io/en/stable/concepts/plans/?h=restate#restatement-plans"
        )

    def get_plan_summary(self, plan: Plan) -> str:
        # use Verbosity.VERY_VERBOSE to prevent the list of models from being truncated
        # this is particularly important for the "Models needing backfill" list because
        # there is no easy way to tell this otherwise
        orig_verbosity = self._console.verbosity
        self._console.verbosity = Verbosity.VERY_VERBOSE

        try:
            # Clear out any output that might exist from prior steps
            self._console.consume_captured_output()
            if plan.restatements:
                self._console._print("\n**Restating models**\n")
            else:
                self._console.show_environment_difference_summary(
                    context_diff=plan.context_diff,
                    no_diff=False,
                )
            if plan.context_diff.has_changes:
                self._console.show_model_difference_summary(
                    context_diff=plan.context_diff,
                    environment_naming_info=plan.environment_naming_info,
                    default_catalog=self._context.default_catalog,
                    no_diff=False,
                )
            difference_summary = self._console.consume_captured_output()
            self._console._show_missing_dates(plan, self._context.default_catalog)
            missing_dates = self._console.consume_captured_output()

            plan_flags_section = (
                f"\n\n{self._generate_plan_flags_section(plan.user_provided_flags)}"
                if plan.user_provided_flags
                else ""
            )

            if not difference_summary and not missing_dates:
                return f"No changes to apply.{plan_flags_section}"

            warnings_block = self._console.consume_captured_warnings()
            errors_block = self._console.consume_captured_errors()

            return f"{warnings_block}{errors_block}{difference_summary}\n{missing_dates}{plan_flags_section}"
        except PlanError as e:
            logger.exception("Plan failed to generate")
            return f"Plan failed to generate. Check for pending or unresolved changes. Error: {e}"
        finally:
            self._console.verbosity = orig_verbosity

    def get_pr_environment_summary(
        self, conclusion: GithubCheckConclusion, exception: t.Optional[Exception] = None
    ) -> str:
        heading = ""
        summary = ""

        if conclusion.is_success:
            summary = self._get_pr_environment_summary_success()
        elif conclusion.is_action_required:
            heading = f":warning: Action Required to create or update PR Environment `{self.pr_environment_name}` :warning:"
            summary = self._get_pr_environment_summary_action_required(exception)
        elif conclusion.is_failure:
            heading = (
                f":x: Failed to create or update PR Environment `{self.pr_environment_name}` :x:"
            )
            summary = self._get_pr_environment_summary_failure(exception)
        elif conclusion.is_skipped:
            heading = f":next_track_button: Skipped creating or updating PR Environment `{self.pr_environment_name}` :next_track_button:"
            summary = self._get_pr_environment_summary_skipped(exception)
        else:
            heading = f":interrobang: Got an unexpected conclusion: {conclusion.value}"

        # note: we just add warnings here, errors will be covered by the "failure" conclusion
        if warnings := self._console.consume_captured_warnings():
            summary = f"{warnings}\n{summary}"

        return f"{heading}\n\n{summary}".strip()

    def _get_pr_environment_summary_success(self) -> str:
        prod_plan = self.prod_plan_with_gaps

        if not prod_plan.has_changes:
            summary = "No models were modified in this PR.\n"
        else:
            intro = self._generate_pr_environment_summary_intro()
            summary = intro + self._generate_pr_environment_summary_list(prod_plan)

        if prod_plan.user_provided_flags:
            summary += self._generate_plan_flags_section(prod_plan.user_provided_flags)

        return summary

    def _get_pr_environment_summary_skipped(self, exception: t.Optional[Exception] = None) -> str:
        if isinstance(exception, NoChangesPlanError):
            skip_reason = "No changes were detected compared to the prod environment."
        elif isinstance(exception, TestFailure):
            skip_reason = "Unit Test(s) Failed so skipping PR creation"
        else:
            skip_reason = "A prior stage failed resulting in skipping PR creation."

        return skip_reason

    def _get_pr_environment_summary_action_required(
        self, exception: t.Optional[Exception] = None
    ) -> str:
        plan = self.pr_plan_or_none
        if isinstance(exception, UncategorizedPlanError) and plan:
            failure_msg = f"The following models could not be categorized automatically:\n"
            for snapshot in plan.uncategorized:
                failure_msg += f"- {snapshot.name}\n"
            failure_msg += (
                f"\nRun `sqlmesh plan {self.pr_environment_name}` locally to apply these changes.\n\n"
                "If you would like the bot to automatically categorize changes, check the [documentation](https://sqlmesh.readthedocs.io/en/stable/integrations/github/) for more information."
            )
        else:
            failure_msg = "Please check the Actions Workflow logs for more information."

        return failure_msg

    def _get_pr_environment_summary_failure(self, exception: t.Optional[Exception] = None) -> str:
        console_output = self._console.consume_captured_output()
        failure_msg = ""

        if isinstance(exception, PlanError):
            if exception.args and (msg := exception.args[0]) and isinstance(msg, str):
                failure_msg += f"*{msg}*\n"
            if console_output:
                failure_msg += f"\n{console_output}"
        elif isinstance(exception, (SQLMeshError, SqlglotError, ValueError)):
            # this logic is taken from the global error handler attached to the CLI, which uses `click.echo()` to output the message
            # so cant be re-used here because it bypasses the Console
            failure_msg = f"**Error:** {str(exception)}"
        elif exception:
            logger.debug(
                "Got unexpected error. Error Type: "
                + str(type(exception))
                + " Stack trace: "
                + traceback.format_exc()
            )
            failure_msg = f"This is an unexpected error.\n\n**Exception:**\n```\n{traceback.format_exc()}\n```"

        if captured_errors := self._console.consume_captured_errors():
            failure_msg = f"{captured_errors}\n{failure_msg}"

        if plan_flags := self.pr_plan_flags:
            failure_msg += f"\n\n{self._generate_plan_flags_section(plan_flags)}"

        return failure_msg

    def run_tests(self) -> t.Tuple[ModelTextTestResult, str]:
        """
        Run tests for the PR
        """
        return self._context._run_tests(verbosity=Verbosity.VERBOSE)

    def run_linter(self) -> None:
        """
        Run linter for the PR
        """
        self._console.consume_captured_output()
        self._context.lint_models()

    def _get_or_create_comment(self, header: str = BOT_HEADER_MSG) -> IssueComment:
        comment = seq_get(
            [comment for comment in self._issue.get_comments() if header in comment.body],
            0,
        )
        if not comment:
            logger.debug(f"Did not find comment so creating one with header: {header}")
            return self._issue.create_comment(header)
        logger.debug(f"Found comment with header: {header}")
        return comment

    def _get_merge_state_status(self) -> MergeStateStatus:
        """
        This feature is currently in preview and therefore not available in the python module.
        So we query GraphQL directly instead.
        """
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.merge-info-preview+json",
        }
        query = f"""{{
            repository(owner: "{self._event.pull_request_info.owner}", name: "{self._event.pull_request_info.repo}") {{
                pullRequest(number: {self._event.pull_request_info.pr_number}) {{
                    title
                    state
                    mergeStateStatus
                }}
            }}
        }}"""
        request = requests.post(
            os.environ["GITHUB_GRAPHQL_URL"],
            json={"query": query},
            headers=headers,
        )
        if request.status_code == 200:
            merge_status = MergeStateStatus(
                request.json()["data"]["repository"]["pullRequest"]["mergeStateStatus"].lower()
            )
            logger.debug(f"Merge state status: {merge_status.value}")
            return merge_status
        raise CICDBotError(f"Unable to get merge state status. Error: {request.text}")

    def update_sqlmesh_comment_info(
        self, value: str, *, dedup_regex: t.Optional[str]
    ) -> t.Tuple[bool, IssueComment]:
        """
        Update the SQLMesh PR Comment for the given lookup key with the given value. If a comment does not exist then
        it creates one. It determines the comment to update by looking for a comment with the header. If a dedup
        regex is provided then it will check if the value already exists in the comment and if so it will not update
        """
        comment = self._get_or_create_comment()
        if dedup_regex:
            # If we find a match against the regex then we just return since the comment has already been posted
            if seq_get(re.findall(dedup_regex, comment.body), 0):
                return False, comment
        full_comment = f"{comment.body}\n{value}"
        body, *truncated = self._chunk_up_api_message(f"{full_comment}")
        if truncated:
            logger.warning(
                f"Comment body was too long so we truncated it. Full text: {full_comment}"
            )
        comment.edit(body=body)
        return True, comment

    def update_pr_environment(self) -> None:
        """
        Creates a PR environment from the logic present in the PR. If the PR contains changes that are
        uncategorized, then an error will be raised.
        """
        self._console.consume_captured_output()  # clear output buffer
        self._context.apply(self.pr_plan)  # will raise if PR environment creation fails

        # update PR info comment
        vde_title = "- :eyes: To **review** this PR's changes, use virtual data environment:"
        comment_value = f"{vde_title}\n  - `{self.pr_environment_name}`"
        if self.bot_config.enable_deploy_command:
            full_command = f"{self.bot_config.command_namespace or ''}/deploy"
            comment_value += f"\n- :arrow_forward: To **apply** this PR's plan to prod, comment:\n  - `{full_command}`"
        dedup_regex = vde_title.replace("*", r"\*") + r".*"
        updated_comment, _ = self.update_sqlmesh_comment_info(
            value=comment_value,
            dedup_regex=dedup_regex,
        )
        if updated_comment:
            self._append_output("created_pr_environment", "true")

    def deploy_to_prod(self) -> None:
        """
        Attempts to deploy a plan to prod. If the plan is not up-to-date or has gaps then it will raise.
        """
        # If the PR is already merged then we will not deploy to prod if this event was triggered prior to the merge.
        # The deploy can still happen if the workflow is configured to listen for `closed` events.
        if self._pull_request.merged and not self._event.is_pull_request_closed:
            raise CICDBotError(
                "PR is already merged and this event was triggered prior to the merge."
            )
        merge_status = self._get_merge_state_status()
        if self.bot_config.check_if_blocked_on_deploy_to_prod and merge_status.is_blocked:
            raise CICDBotError(
                "Branch protection or ruleset requirement is likely not satisfied, e.g. missing CODEOWNERS approval. "
                "Please check PR and resolve any issues. To disable this check, set `check_if_blocked_on_deploy_to_prod` to false in the bot configuration."
            )
        if merge_status.is_dirty:
            raise CICDBotError(
                "Merge commit cannot be cleanly created. Likely from a merge conflict. "
                "Please check PR and resolve any issues."
            )
        plan_summary = f"""<details>
  <summary>:ship: Prod Plan Being Applied</summary>

{self.get_plan_summary(self.prod_plan)}
</details>

"""
        if self.forward_only_plan:
            plan_summary = (
                f"{self.get_forward_only_plan_post_deployment_tip(self.prod_plan)}\n{plan_summary}"
            )

        self.update_sqlmesh_comment_info(
            value=plan_summary,
            dedup_regex=None,
        )
        self._context.apply(self.prod_plan)

    def try_invalidate_pr_environment(self) -> None:
        """
        Marks the PR environment for garbage collection.
        """
        if self.bot_config.invalidate_environment_after_deploy:
            self._context.invalidate_environment(self.pr_environment_name)

    def _update_check(
        self,
        name: str,
        status: GithubCheckStatus,
        title: str,
        conclusion: t.Optional[GithubCheckConclusion] = None,
        full_summary: t.Optional[str] = None,
    ) -> None:
        """
        Updates the status of the merge commit.
        """
        current_time = now()
        kwargs: t.Dict[str, t.Any] = {
            "name": name,
            # Note: The environment variable `GITHUB_SHA` would be the merge commit so that is why instead we
            # get the last commit on the PR.
            "head_sha": self._pull_request.head.sha,
            "status": status.value,
        }
        if status.is_in_progress:
            kwargs["started_at"] = current_time
        if status.is_completed:
            kwargs["completed_at"] = current_time
        if conclusion:
            kwargs["conclusion"] = conclusion.value
        full_summary = full_summary or title
        summary, text, *truncated = self._chunk_up_api_message(full_summary) + [None]
        if truncated and truncated[0] is not None:
            logger.warning(f"Summary was too long so we truncated it. Full text: {full_summary}")
        kwargs["output"] = {"title": title, "summary": summary}
        if text:
            kwargs["output"]["text"] = text
        logger.debug(f"Updating check with kwargs: {kwargs}")

        if self.running_in_github_actions:
            # Only make the API call to update the checks if we are running within GitHub Actions
            # One very annoying limitation of the Pull Request Checks API is that its only available to GitHub Apps
            # and not personal access tokens, which makes it unable to be utilized during local development
            if name in self._check_run_mapping:
                logger.debug(f"Found check run in mapping so updating it. Name: {name}")
                check_run = self._check_run_mapping[name]
                check_run.edit(
                    **{
                        k: v
                        for k, v in kwargs.items()
                        if k not in ("name", "head_sha", "started_at")
                    }
                )
            else:
                logger.debug(f"Did not find check run in mapping so creating it. Name: {name}")
                self._check_run_mapping[name] = self._repo.create_check_run(**kwargs)
        else:
            # Output the summary using print() so the newlines are resolved and the result can easily
            # be disambiguated from the rest of the console output and copy+pasted into a Markdown renderer
            print(
                f"---CHECK OUTPUT START: {kwargs['output']['title']} ---\n{kwargs['output']['summary']}\n---CHECK OUTPUT END---\n"
            )

        if conclusion:
            self._append_output(
                word_characters_only(name.replace("SQLMesh - ", "").lower()), conclusion.value
            )

    def _update_check_handler(
        self,
        check_name: str,
        status: GithubCheckStatus,
        conclusion: t.Optional[GithubCheckConclusion],
        status_handler: t.Callable[[GithubCheckStatus], t.Tuple[str, t.Optional[str]]],
        conclusion_handler: t.Callable[
            [GithubCheckConclusion], t.Tuple[GithubCheckConclusion, str, t.Optional[str]]
        ],
    ) -> None:
        if conclusion:
            conclusion, title, summary = conclusion_handler(conclusion)
        else:
            title, summary = status_handler(status)
        self._update_check(
            name=check_name,
            status=status,
            title=title,
            conclusion=conclusion,
            full_summary=summary,
        )

    def update_linter_check(
        self,
        status: GithubCheckStatus,
        conclusion: t.Optional[GithubCheckConclusion] = None,
    ) -> None:
        if not self._context.config.linter.enabled:
            return

        def conclusion_handler(
            conclusion: GithubCheckConclusion,
        ) -> t.Tuple[GithubCheckConclusion, str, t.Optional[str]]:
            linter_summary = self._console.consume_captured_output() or "Linter Success"

            title = "Linter results"

            return conclusion, title, linter_summary

        self._update_check_handler(
            check_name="SQLMesh - Linter",
            status=status,
            conclusion=conclusion,
            status_handler=lambda status: (
                {
                    GithubCheckStatus.IN_PROGRESS: "Running linter",
                    GithubCheckStatus.QUEUED: "Waiting to Run linter",
                }[status],
                None,
            ),
            conclusion_handler=conclusion_handler,
        )

    def update_test_check(
        self,
        status: GithubCheckStatus,
        conclusion: t.Optional[GithubCheckConclusion] = None,
        result: t.Optional[ModelTextTestResult] = None,
        traceback: t.Optional[str] = None,
    ) -> None:
        """
        Updates the status of tests for code in the PR
        """

        def conclusion_handler(
            conclusion: GithubCheckConclusion,
            result: t.Optional[ModelTextTestResult],
        ) -> t.Tuple[GithubCheckConclusion, str, t.Optional[str]]:
            if result:
                # Clear out console
                self._console.consume_captured_output()
                self._console.log_test_results(
                    result,
                    self._context.test_connection_config._engine_adapter.DIALECT,
                )
                test_summary = self._console.consume_captured_output()
                test_title = "Tests Passed" if result.wasSuccessful() else "Tests Failed"
                test_conclusion = (
                    GithubCheckConclusion.SUCCESS
                    if result.wasSuccessful()
                    else GithubCheckConclusion.FAILURE
                )
                return test_conclusion, test_title, test_summary
            if traceback:
                self._console._print(traceback)

            test_title = "Skipped Tests" if conclusion.is_skipped else "Tests Failed"
            return conclusion, test_title, traceback

        self._update_check_handler(
            check_name="SQLMesh - Run Unit Tests",
            status=status,
            conclusion=conclusion,
            status_handler=lambda status: (
                {
                    GithubCheckStatus.IN_PROGRESS: "Running Tests",
                    GithubCheckStatus.QUEUED: "Waiting to Run Tests",
                }[status],
                None,
            ),
            conclusion_handler=functools.partial(conclusion_handler, result=result),
        )

    def update_required_approval_check(
        self, status: GithubCheckStatus, conclusion: t.Optional[GithubCheckConclusion] = None
    ) -> None:
        """
        Updates the status of the merge commit for the required approval.
        """

        def conclusion_handler(
            conclusion: GithubCheckConclusion,
        ) -> t.Tuple[GithubCheckConclusion, str, t.Optional[str]]:
            test_summary = "**List of possible required approvers:**\n"
            for user in self._required_approvers:
                test_summary += f"- `{user.github_username or user.username}`\n"

            title = (
                f"Obtained approval from required approvers: {', '.join([user.github_username or user.username for user in self._required_approvers_with_approval])}"
                if conclusion.is_success
                else "Need a Required Approval"
            )
            return conclusion, title, test_summary

        # If we get a skip that means required approvers is not configured therefore it does not need to be displayed
        if conclusion and conclusion.is_skipped:
            return

        self._update_check_handler(
            check_name="SQLMesh - Has Required Approval",
            status=status,
            conclusion=conclusion,
            status_handler=lambda status: (
                {
                    GithubCheckStatus.IN_PROGRESS: "Checking if we have required Approvers",
                    GithubCheckStatus.QUEUED: "Waiting to Check if we have required Approvers",
                }[status],
                None,
            ),
            conclusion_handler=conclusion_handler,
        )

    def update_pr_environment_check(
        self, status: GithubCheckStatus, exception: t.Optional[Exception] = None
    ) -> t.Optional[GithubCheckConclusion]:
        """
        Updates the status of the merge commit for the PR environment.
        """
        conclusion: t.Optional[GithubCheckConclusion] = None
        if isinstance(exception, (NoChangesPlanError, TestFailure, LinterError)):
            conclusion = GithubCheckConclusion.SKIPPED
        elif isinstance(exception, UncategorizedPlanError):
            conclusion = GithubCheckConclusion.ACTION_REQUIRED
        elif exception:
            conclusion = GithubCheckConclusion.FAILURE
        elif status.is_completed:
            conclusion = GithubCheckConclusion.SUCCESS

        check_title_static = "PR Virtual Data Environment: "
        check_title = check_title_static + self.pr_environment_name

        def conclusion_handler(
            conclusion: GithubCheckConclusion, exception: t.Optional[Exception]
        ) -> t.Tuple[GithubCheckConclusion, str, t.Optional[str]]:
            summary = self.get_pr_environment_summary(conclusion, exception)
            self._append_output("pr_environment_name", self.pr_environment_name)
            return conclusion, check_title, summary

        self._update_check_handler(
            check_name="SQLMesh - PR Environment Synced",
            status=status,
            conclusion=conclusion,
            status_handler=lambda status: (
                check_title,
                {
                    GithubCheckStatus.QUEUED: f":pause_button: Waiting to create or update PR Environment `{self.pr_environment_name}`",
                    GithubCheckStatus.IN_PROGRESS: f":rocket: Creating or Updating PR Environment `{self.pr_environment_name}`",
                }[status],
            ),
            conclusion_handler=functools.partial(conclusion_handler, exception=exception),
        )
        return conclusion

    def update_prod_plan_preview_check(
        self,
        status: GithubCheckStatus,
        conclusion: t.Optional[GithubCheckConclusion] = None,
        summary: t.Optional[str] = None,
    ) -> None:
        """
        Updates the status of the merge commit for the prod plan preview.
        """

        def conclusion_handler(
            conclusion: GithubCheckConclusion, summary: t.Optional[str] = None
        ) -> t.Tuple[GithubCheckConclusion, str, t.Optional[str]]:
            conclusion_to_title = {
                GithubCheckConclusion.SUCCESS: "Prod Plan Preview",
                GithubCheckConclusion.CANCELLED: "Cancelled generating prod plan preview",
                GithubCheckConclusion.SKIPPED: "Skipped generating prod plan preview since PR was not synchronized",
                GithubCheckConclusion.FAILURE: "Failed to generate prod plan preview",
            }
            title = conclusion_to_title.get(
                conclusion, f"Got an unexpected conclusion: {conclusion.value}"
            )
            if conclusion == GithubCheckConclusion.SUCCESS and summary:
                summary = (
                    f"This is a preview that shows the differences between this PR environment `{self.pr_environment_name}` and `prod`.\n\n"
                    "These are the changes that would be deployed.\n\n"
                ) + summary

            return conclusion, title, summary

        self._update_check_handler(
            check_name="SQLMesh - Prod Plan Preview",
            status=status,
            conclusion=conclusion,
            status_handler=lambda status: (
                {
                    GithubCheckStatus.IN_PROGRESS: "Generating Prod Plan",
                    GithubCheckStatus.QUEUED: "Waiting to Generate Prod Plan",
                }[status],
                None,
            ),
            conclusion_handler=functools.partial(conclusion_handler, summary=summary),
        )

    def update_prod_environment_check(
        self,
        status: GithubCheckStatus,
        conclusion: t.Optional[GithubCheckConclusion] = None,
        skip_reason: t.Optional[str] = None,
        plan_error: t.Optional[PlanError] = None,
    ) -> None:
        """
        Updates the status of the merge commit for the prod environment.
        """

        def conclusion_handler(
            conclusion: GithubCheckConclusion, skip_reason: t.Optional[str] = None
        ) -> t.Tuple[GithubCheckConclusion, str, t.Optional[str]]:
            conclusion_to_title = {
                GithubCheckConclusion.SUCCESS: "Deployed to Prod",
                GithubCheckConclusion.CANCELLED: "Cancelled deploying to prod",
                GithubCheckConclusion.SKIPPED: "Skipped deployment",
                GithubCheckConclusion.FAILURE: "Failed to deploy to prod",
                GithubCheckConclusion.ACTION_REQUIRED: "Failed due to error applying plan",
            }
            title = (
                conclusion_to_title.get(conclusion)
                or f"Got an unexpected conclusion: {conclusion.value}"
            )
            if conclusion.is_skipped:
                summary = skip_reason
            elif conclusion.is_failure:
                captured_errors = self._console.consume_captured_errors()
                summary = (
                    captured_errors or f"{title}\n\n**Error:**\n```\n{traceback.format_exc()}\n```"
                )
            elif conclusion.is_action_required:
                if plan_error:
                    summary = f"**Plan error:**\n```\n{plan_error}\n```"
                else:
                    summary = "Got an action required conclusion but no plan error was provided. This is unexpected."
            else:
                summary = "**Generated Prod Plan**\n" + self.get_plan_summary(self.prod_plan)

            return conclusion, title, summary

        self._update_check_handler(
            check_name="SQLMesh - Prod Environment Synced",
            status=status,
            conclusion=conclusion,
            status_handler=lambda status: (
                {
                    GithubCheckStatus.IN_PROGRESS: "Deploying to Prod",
                    GithubCheckStatus.QUEUED: "Waiting to see if we can deploy to prod",
                }[status],
                None,
            ),
            conclusion_handler=functools.partial(conclusion_handler, skip_reason=skip_reason),
        )

    def try_merge_pr(self) -> None:
        """
        Merges the PR using the merge method defined in the bot config. If one is not defined then a merge is not
        performed
        """
        if self.bot_config.merge_method:
            logger.debug(f"Merging PR with merge method: {self.bot_config.merge_method.value}")
            self._pull_request.merge(merge_method=self.bot_config.merge_method.value)
        else:
            logger.debug("No merge method defined so skipping merge")

    def get_command_from_comment(self) -> BotCommand:
        """
        Gets the command from the comment
        """
        if not self._event.is_comment_added:
            logger.debug("Event is not a comment so returning invalid")
            return BotCommand.INVALID
        if self._event.pull_request_comment_body is None:
            raise CICDBotError("Unable to get comment body")
        logger.debug(f"Getting command from comment body: {self._event.pull_request_comment_body}")
        return BotCommand.from_comment_body(
            self._event.pull_request_comment_body, self.bot_config.command_namespace
        )

    def _chunk_up_api_message(self, message: str) -> t.List[str]:
        """
        Chunks up the message into `MAX_BYTE_LENGTH` byte chunks
        """
        message_encoded = message.encode("utf-8")
        return [
            message_encoded[i : i + self.MAX_BYTE_LENGTH].decode("utf-8", "ignore")
            for i in range(0, len(message_encoded), self.MAX_BYTE_LENGTH)
        ]

    @property
    def running_in_github_actions(self) -> bool:
        return os.environ.get("GITHUB_ACTIONS", None) == "true"

    @property
    def version_info(self) -> str:
        from sqlmesh.cli.main import _sqlmesh_version

        return _sqlmesh_version()

    def _generate_plan_flags_section(
        self, user_provided_flags: t.Dict[str, UserProvidedFlags]
    ) -> str:
        # collapsed section syntax:
        # https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections#creating-a-collapsed-section
        section = "<details>\n\n<summary>Plan flags</summary>\n\n"
        for flag_name, flag_value in user_provided_flags.items():
            section += f"- `{flag_name}` = `{flag_value}`\n"
        section += "\n</details>"

        return section

    def _generate_pr_environment_summary_intro(self) -> str:
        note = ""
        subset_reasons = []

        if self.bot_config.skip_pr_backfill:
            subset_reasons.append("`skip_pr_backfill` is enabled")

        if default_pr_start := self.bot_config.default_pr_start:
            subset_reasons.append(f"`default_pr_start` is set to `{default_pr_start}`")

        if subset_reasons:
            note = (
                "> [!IMPORTANT]\n"
                f"> This PR environment may only contain a subset of data because:\n"
                + "\n".join(f"> - {r}" for r in subset_reasons)
                + "\n"
                "> \n"
                "> This means that deploying to `prod` may not be a simple virtual update if there is still some data to load.\n"
                "> See `Dates not loaded in PR` below or the `Prod Plan Preview` check for more information.\n\n"
            )

        return (
            f"Here is a summary of data that has been loaded into the PR environment `{self.pr_environment_name}` and could be deployed to `prod`.\n\n"
            + note
        )

    def _generate_pr_environment_summary_list(self, plan: Plan) -> str:
        added_snapshot_ids = set(plan.context_diff.added)
        modified_snapshot_ids = set(
            s.snapshot_id for s, _ in plan.context_diff.modified_snapshots.values()
        )
        removed_snapshot_ids = set(plan.context_diff.removed_snapshots.keys())

        # note: we sort these to get a deterministic order for the output tests
        table_records = sorted(
            [
                SnapshotSummaryRecord(snapshot_id=snapshot_id, plan=plan)
                for snapshot_id in (
                    added_snapshot_ids | modified_snapshot_ids | removed_snapshot_ids
                )
            ],
            key=lambda r: r.display_name,
        )

        sections = [
            ("### Added", [r for r in table_records if r.is_added]),
            ("### Removed", [r for r in table_records if r.is_removed]),
            ("### Directly Modified", [r for r in table_records if r.is_directly_modified]),
            ("### Indirectly Modified", [r for r in table_records if r.is_indirectly_modified]),
            (
                "### Metadata Updated",
                [r for r in table_records if r.is_metadata_updated and not r.is_modified],
            ),
        ]

        summary = ""
        for title, records in sections:
            if records:
                summary += f"\n{title}\n"

            for record in records:
                summary += f"{record.as_markdown_list_item}\n"

        return summary


@dataclass
class SnapshotSummaryRecord:
    snapshot_id: SnapshotId
    plan: Plan

    @property
    def snapshot(self) -> Snapshot:
        if self.is_removed:
            raise ValueError("Removed snapshots only have SnapshotTableInfo available")
        return self.plan.snapshots[self.snapshot_id]

    @cached_property
    def snapshot_table_info(self) -> SnapshotTableInfo:
        if self.is_removed:
            return self.plan.modified_snapshots[self.snapshot_id].table_info
        return self.plan.snapshots[self.snapshot_id].table_info

    @property
    def display_name(self) -> str:
        dialect = None if self.is_removed else self.snapshot.node.dialect
        return self.snapshot_table_info.display_name(
            self.plan.environment_naming_info, default_catalog=None, dialect=dialect
        )

    @property
    def change_category(self) -> str:
        if self.is_removed:
            return SNAPSHOT_CHANGE_CATEGORY_STR[SnapshotChangeCategory.BREAKING]

        if change_category := self.snapshot.change_category:
            return SNAPSHOT_CHANGE_CATEGORY_STR[change_category]

        return "Uncategorized"

    @property
    def is_added(self) -> bool:
        return self.snapshot_id in self.plan.context_diff.added

    @property
    def is_removed(self) -> bool:
        return self.snapshot_id in self.plan.context_diff.removed_snapshots

    @property
    def is_dev_preview(self) -> bool:
        return not self.plan.deployability_index.is_deployable(self.snapshot_id)

    @property
    def is_directly_modified(self) -> bool:
        return self.plan.context_diff.directly_modified(self.snapshot_table_info.name)

    @property
    def is_indirectly_modified(self) -> bool:
        return self.plan.context_diff.indirectly_modified(self.snapshot_table_info.name)

    @property
    def is_modified(self) -> bool:
        return self.is_directly_modified or self.is_indirectly_modified

    @property
    def is_metadata_updated(self) -> bool:
        return self.plan.context_diff.metadata_updated(self.snapshot_table_info.name)

    @property
    def is_incremental(self) -> bool:
        return self.snapshot_table_info.is_incremental

    @property
    def modification_type(self) -> str:
        if self.is_directly_modified:
            return "Directly modified"
        if self.is_indirectly_modified:
            return "Indirectly modified"
        if self.is_metadata_updated:
            return "Metadata updated"

        return "Unknown"

    @property
    def loaded_intervals(self) -> SnapshotIntervals:
        if self.is_removed:
            raise ValueError("Removed snapshots dont have loaded intervals available")

        return SnapshotIntervals(
            snapshot_id=self.snapshot_id,
            intervals=(
                self.snapshot.dev_intervals
                if self.snapshot.is_forward_only
                else self.snapshot.intervals
            ),
        )

    @property
    def loaded_intervals_rendered(self) -> str:
        if self.is_removed:
            return "REMOVED"

        return self._format_intervals(self.loaded_intervals)

    @property
    def missing_intervals(self) -> t.Optional[SnapshotIntervals]:
        return next(
            (si for si in self.plan.missing_intervals if si.snapshot_id == self.snapshot_id),
            None,
        )

    @property
    def missing_intervals_formatted(self) -> str:
        if not self.is_removed and (intervals := self.missing_intervals):
            return self._format_intervals(intervals)

        return "N/A"

    @property
    def as_markdown_list_item(self) -> str:
        if self.is_removed:
            return f"- `{self.display_name}` ({self.change_category})"

        how_applied = ""

        if not self.is_incremental:
            from sqlmesh.core.console import _format_missing_intervals

            # note: this is to re-use the '[recreate view]' and '[full refresh]' text and keep it in sync with updates to the CLI
            # it doesnt actually use the passed intervals, those are handled differently
            how_applied = _format_missing_intervals(self.snapshot, self.loaded_intervals)

        how_applied_str = f" [{how_applied}]" if how_applied else ""

        item = f"- `{self.display_name}` ({self.change_category})\n"

        if self.snapshot_table_info.model_kind_name:
            item += f"  **Kind:** {self.snapshot_table_info.model_kind_name}{how_applied_str}\n"

        if self.is_incremental:
            # in-depth interval info is only relevant for incremental models
            item += f"  **Dates loaded in PR:** [{self.loaded_intervals_rendered}]\n"
            if self.missing_intervals:
                item += f"  **Dates *not* loaded in PR:** [{self.missing_intervals_formatted}]\n"

        return item

    def _format_intervals(self, intervals: SnapshotIntervals) -> str:
        preview_modifier = " (**preview**)" if self.is_dev_preview else ""
        return f"{intervals.format_intervals(self.snapshot.node.interval_unit)}{preview_modifier}"
