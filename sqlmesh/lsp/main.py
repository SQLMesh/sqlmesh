#!/usr/bin/env python
"""A Language Server Protocol (LSP) server for SQL with SQLMesh integration, refactored without globals."""

from itertools import chain
import logging
import typing as t
from pathlib import Path
import urllib.parse
import uuid

from lsprotocol import types
from lsprotocol.types import (
    WorkspaceDiagnosticRefreshRequest,
    WorkspaceInlayHintRefreshRequest,
)
from pygls.server import LanguageServer
from sqlglot import exp
from sqlmesh._version import __version__
from sqlmesh.core.context import Context
from sqlmesh.utils.date import to_timestamp
from sqlmesh.lsp.api import (
    API_FEATURE,
    ApiRequest,
    ApiResponseGetColumnLineage,
    ApiResponseGetLineage,
    ApiResponseGetModels,
    ApiResponseGetTableDiff,
)

from sqlmesh.lsp.commands import EXTERNAL_MODEL_UPDATE_COLUMNS
from sqlmesh.lsp.completions import get_sql_completions
from sqlmesh.lsp.context import (
    LSPContext,
    ModelTarget,
)
from sqlmesh.lsp.custom import (
    ALL_MODELS_FEATURE,
    ALL_MODELS_FOR_RENDER_FEATURE,
    RENDER_MODEL_FEATURE,
    SUPPORTED_METHODS_FEATURE,
    FORMAT_PROJECT_FEATURE,
    GET_ENVIRONMENTS_FEATURE,
    GET_MODELS_FEATURE,
    AllModelsRequest,
    AllModelsResponse,
    AllModelsForRenderRequest,
    AllModelsForRenderResponse,
    CustomMethodResponseBaseClass,
    RenderModelRequest,
    RenderModelResponse,
    SupportedMethodsRequest,
    SupportedMethodsResponse,
    FormatProjectRequest,
    FormatProjectResponse,
    CustomMethod,
    LIST_WORKSPACE_TESTS_FEATURE,
    ListWorkspaceTestsRequest,
    ListWorkspaceTestsResponse,
    LIST_DOCUMENT_TESTS_FEATURE,
    ListDocumentTestsRequest,
    ListDocumentTestsResponse,
    RUN_TEST_FEATURE,
    RunTestRequest,
    RunTestResponse,
    GetEnvironmentsRequest,
    GetEnvironmentsResponse,
    EnvironmentInfo,
    GetModelsRequest,
    GetModelsResponse,
    ModelInfo,
)
from sqlmesh.lsp.errors import ContextFailedError, context_error_to_diagnostic
from sqlmesh.lsp.helpers import to_lsp_range, to_sqlmesh_position
from sqlmesh.lsp.hints import get_hints
from sqlmesh.lsp.reference import (
    CTEReference,
    ModelReference,
    get_references,
    get_all_references,
)
from sqlmesh.lsp.rename import prepare_rename, rename_symbol, get_document_highlights
from sqlmesh.lsp.uri import URI
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.lineage import ExternalModelReference
from sqlmesh.utils.pydantic import PydanticModel
from web.server.api.endpoints.lineage import column_lineage, model_lineage
from web.server.api.endpoints.models import get_models
from web.server.api.endpoints.table_diff import _process_sample_data
from typing import Union
from dataclasses import dataclass, field

from web.server.models import RowDiff, SchemaDiff, TableDiff


class InitializationOptions(PydanticModel):
    """Initialization options for the SQLMesh Language Server, that
    are passed from the client to the server."""

    project_paths: t.Optional[t.List[str]] = None


@dataclass
class NoContext:
    """State when no context has been attempted to load."""

    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ContextLoaded:
    """State when context has been successfully loaded."""

    lsp_context: LSPContext
    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ContextFailed:
    """State when context failed to load with an error message."""

    error: ContextFailedError
    context: t.Optional[Context] = None
    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))


ContextState = Union[NoContext, ContextLoaded, ContextFailed]


class SQLMeshLanguageServer:
    # Specified folders take precedence over workspace folders or looking
    # for a config files. They are explicitly set by the user and optionally
    # pass in at init
    specified_paths: t.Optional[t.List[Path]] = None

    def __init__(
        self,
        context_class: t.Type[Context],
        server_name: str = "sqlmesh_lsp",
        version: str = __version__,
    ):
        """
        :param context_class: A class that inherits from `Context`.
        :param server_name: Name for the language server.
        :param version: Version string.
        """
        self.server = LanguageServer(server_name, version, max_workers=1)
        self.context_class = context_class
        self.context_state: ContextState = NoContext()
        self.workspace_folders: t.List[Path] = []

        self.has_raised_loading_error: bool = False

        self.client_supports_pull_diagnostics = False
        self._supported_custom_methods: t.Dict[
            str,
            t.Callable[
                # mypy unable to recognize the base class
                [LanguageServer, t.Any],
                t.Any,
            ],
        ] = {
            ALL_MODELS_FEATURE: self._custom_all_models,
            RENDER_MODEL_FEATURE: self._custom_render_model,
            ALL_MODELS_FOR_RENDER_FEATURE: self._custom_all_models_for_render,
            API_FEATURE: self._custom_api,
            SUPPORTED_METHODS_FEATURE: self._custom_supported_methods,
            FORMAT_PROJECT_FEATURE: self._custom_format_project,
            LIST_WORKSPACE_TESTS_FEATURE: self._list_workspace_tests,
            LIST_DOCUMENT_TESTS_FEATURE: self._list_document_tests,
            RUN_TEST_FEATURE: self._run_test,
            GET_ENVIRONMENTS_FEATURE: self._custom_get_environments,
            GET_MODELS_FEATURE: self._custom_get_models,
        }

        # Register LSP features (e.g., formatting, hover, etc.)
        self._register_features()

    def _list_workspace_tests(
        self,
        ls: LanguageServer,
        params: ListWorkspaceTestsRequest,
    ) -> ListWorkspaceTestsResponse:
        """List all tests in the current workspace."""
        try:
            context = self._context_get_or_load()
            tests = context.list_workspace_tests()
            return ListWorkspaceTestsResponse(tests=tests)
        except Exception as e:
            ls.log_trace(f"Error listing workspace tests: {e}")
            return ListWorkspaceTestsResponse(tests=[])

    def _list_document_tests(
        self,
        ls: LanguageServer,
        params: ListDocumentTestsRequest,
    ) -> ListDocumentTestsResponse:
        """List tests for a specific document."""
        try:
            uri = URI(params.textDocument.uri)
            context = self._context_get_or_load(uri)
            tests = context.get_document_tests(uri)
            return ListDocumentTestsResponse(tests=tests)
        except Exception as e:
            ls.log_trace(f"Error listing document tests: {e}")
            return ListDocumentTestsResponse(tests=[])

    def _run_test(
        self,
        ls: LanguageServer,
        params: RunTestRequest,
    ) -> RunTestResponse:
        """Run a specific test."""
        try:
            uri = URI(params.textDocument.uri)
            context = self._context_get_or_load(uri)
            result = context.run_test(uri, params.testName)
            return result
        except Exception as e:
            ls.log_trace(f"Error running test: {e}")
            return RunTestResponse(success=False, response_error=str(e))

    # All the custom LSP methods are registered here and prefixed with _custom
    def _custom_all_models(self, ls: LanguageServer, params: AllModelsRequest) -> AllModelsResponse:
        uri = URI(params.textDocument.uri)
        # Get the document content
        content = None
        try:
            document = ls.workspace.get_text_document(params.textDocument.uri)
            content = document.source
        except Exception:
            pass
        try:
            context = self._context_get_or_load(uri)
            return LSPContext.get_completions(context, uri, content)
        except Exception as e:
            from sqlmesh.lsp.completions import get_sql_completions

            return get_sql_completions(None, URI(params.textDocument.uri), content)

    def _custom_render_model(
        self, ls: LanguageServer, params: RenderModelRequest
    ) -> RenderModelResponse:
        uri = URI(params.textDocumentUri)
        context = self._context_get_or_load(uri)
        return RenderModelResponse(models=context.render_model(uri))

    def _custom_all_models_for_render(
        self, ls: LanguageServer, params: AllModelsForRenderRequest
    ) -> AllModelsForRenderResponse:
        context = self._context_get_or_load()
        return AllModelsForRenderResponse(models=context.list_of_models_for_rendering())

    def _custom_format_project(
        self, ls: LanguageServer, params: FormatProjectRequest
    ) -> FormatProjectResponse:
        """Format all models in the current project."""
        try:
            context = self._context_get_or_load()
            context.context.format()
            return FormatProjectResponse()
        except Exception as e:
            ls.log_trace(f"Error formatting project: {e}")
            return FormatProjectResponse()

    def _custom_get_environments(
        self, ls: LanguageServer, params: GetEnvironmentsRequest
    ) -> GetEnvironmentsResponse:
        """Get all environments in the current project."""
        try:
            context = self._context_get_or_load()
            environments = {}

            # Get environments from state
            for env in context.context.state_reader.get_environments():
                environments[env.name] = EnvironmentInfo(
                    name=env.name,
                    snapshots=[s.fingerprint.to_identifier() for s in env.snapshots],
                    start_at=str(to_timestamp(env.start_at)),
                    plan_id=env.plan_id or "",
                )

            return GetEnvironmentsResponse(
                environments=environments,
                pinned_environments=context.context.config.pinned_environments,
                default_target_environment=context.context.config.default_target_environment,
            )
        except Exception as e:
            ls.log_trace(f"Error getting environments: {e}")
            return GetEnvironmentsResponse(
                response_error=str(e),
                environments={},
                pinned_environments=set(),
                default_target_environment="",
            )

    def _custom_get_models(self, ls: LanguageServer, params: GetModelsRequest) -> GetModelsResponse:
        """Get all models available for table diff."""
        try:
            context = self._context_get_or_load()
            models = [
                ModelInfo(
                    name=model.name,
                    fqn=model.fqn,
                    description=model.description,
                )
                for model in context.context.models.values()
                # Filter for models that are suitable for table diff
                if model._path is not None  # Has a file path
            ]
            return GetModelsResponse(models=models)
        except Exception as e:
            ls.log_trace(f"Error getting table diff models: {e}")
            return GetModelsResponse(
                response_error=str(e),
                models=[],
            )

    def _custom_api(
        self, ls: LanguageServer, request: ApiRequest
    ) -> t.Union[
        ApiResponseGetModels,
        ApiResponseGetColumnLineage,
        ApiResponseGetLineage,
        ApiResponseGetTableDiff,
    ]:
        ls.log_trace(f"API request: {request}")
        context = self._context_get_or_load()

        parsed_url = urllib.parse.urlparse(request.url)
        path_parts = parsed_url.path.strip("/").split("/")

        if request.method == "GET":
            if path_parts == ["api", "models"]:
                # /api/models
                return ApiResponseGetModels(data=get_models(context.context))

            if path_parts[:2] == ["api", "lineage"]:
                if len(path_parts) == 3:
                    # /api/lineage/{model}
                    model_name = urllib.parse.unquote(path_parts[2])
                    lineage = model_lineage(model_name, context.context)
                    non_set_lineage = {k: v for k, v in lineage.items() if v is not None}
                    return ApiResponseGetLineage(data=non_set_lineage)

                if len(path_parts) == 4:
                    # /api/lineage/{model}/{column}
                    model_name = urllib.parse.unquote(path_parts[2])
                    column = urllib.parse.unquote(path_parts[3])
                    models_only = False
                    if hasattr(request, "params"):
                        models_only = bool(getattr(request.params, "models_only", False))
                    column_lineage_response = column_lineage(
                        model_name, column, models_only, context.context
                    )
                    return ApiResponseGetColumnLineage(data=column_lineage_response)

            if path_parts[:2] == ["api", "table_diff"]:
                import numpy as np

                # /api/table_diff
                params = request.params
                table_diff_result: t.Optional[TableDiff] = None
                if params := request.params:
                    source = getattr(params, "source", "") if params else ""
                    target = getattr(params, "target", "") if params else ""
                    on = getattr(params, "on", None) if params else None
                    model_or_snapshot = (
                        getattr(params, "model_or_snapshot", None) if params else None
                    )
                    where = getattr(params, "where", None) if params else None
                    temp_schema = getattr(params, "temp_schema", None) if params else None
                    limit = getattr(params, "limit", 20) if params else 20

                    table_diffs = context.context.table_diff(
                        source=source,
                        target=target,
                        on=exp.condition(on) if on else None,
                        select_models={model_or_snapshot} if model_or_snapshot else None,
                        where=where,
                        limit=limit,
                        show=False,
                    )

                    if table_diffs:
                        diff = table_diffs[0] if isinstance(table_diffs, list) else table_diffs

                        _schema_diff = diff.schema_diff()
                        _row_diff = diff.row_diff(temp_schema=temp_schema)
                        schema_diff = SchemaDiff(
                            source=_schema_diff.source,
                            target=_schema_diff.target,
                            source_schema=_schema_diff.source_schema,
                            target_schema=_schema_diff.target_schema,
                            added=_schema_diff.added,
                            removed=_schema_diff.removed,
                            modified=_schema_diff.modified,
                        )

                        # create a readable column-centric sample data structure
                        processed_sample_data = _process_sample_data(_row_diff, source, target)

                        row_diff = RowDiff(
                            source=_row_diff.source,
                            target=_row_diff.target,
                            stats=_row_diff.stats,
                            sample=_row_diff.sample.replace({np.nan: None}).to_dict(),
                            joined_sample=_row_diff.joined_sample.replace({np.nan: None}).to_dict(),
                            s_sample=_row_diff.s_sample.replace({np.nan: None}).to_dict(),
                            t_sample=_row_diff.t_sample.replace({np.nan: None}).to_dict(),
                            column_stats=_row_diff.column_stats.replace({np.nan: None}).to_dict(),
                            source_count=_row_diff.source_count,
                            target_count=_row_diff.target_count,
                            count_pct_change=_row_diff.count_pct_change,
                            decimals=getattr(_row_diff, "decimals", 3),
                            processed_sample_data=processed_sample_data,
                        )

                        s_index, t_index, _ = diff.key_columns
                        table_diff_result = TableDiff(
                            schema_diff=schema_diff,
                            row_diff=row_diff,
                            on=[(s.name, t.name) for s, t in zip(s_index, t_index)],
                        )
                return ApiResponseGetTableDiff(data=table_diff_result)

        raise NotImplementedError(f"API request not implemented: {request.url}")

    def _custom_supported_methods(
        self, ls: LanguageServer, params: SupportedMethodsRequest
    ) -> SupportedMethodsResponse:
        """Return all supported custom LSP methods."""
        return SupportedMethodsResponse(
            methods=[
                CustomMethod(
                    name=name,
                )
                for name in self._supported_custom_methods
            ]
        )

    def _reload_context_and_publish_diagnostics(
        self, ls: LanguageServer, uri: URI, document_uri: str
    ) -> None:
        """Helper method to reload context and publish diagnostics."""
        if isinstance(self.context_state, NoContext):
            pass
        elif isinstance(self.context_state, ContextFailed):
            if self.context_state.context:
                try:
                    self.context_state.context.load()
                    # Creating a new LSPContext will naturally create fresh caches
                    self.context_state = ContextLoaded(
                        lsp_context=LSPContext(self.context_state.context)
                    )
                except Exception as e:
                    ls.log_trace(f"Error loading context: {e}")
                    context = (
                        self.context_state.context
                        if hasattr(self.context_state, "context")
                        else None
                    )
                    self.context_state = ContextFailed(error=e, context=context)
            else:
                # If there's no context, reset to NoContext and try to create one from scratch
                ls.log_trace("No partial context available, attempting fresh creation")
                self.context_state = NoContext()
                self.has_raised_loading_error = False  # Reset error flag to show new errors
                try:
                    self._ensure_context_for_document(uri)
                    # If successful, context_state will be ContextLoaded
                    if isinstance(self.context_state, ContextLoaded):
                        loaded_sqlmesh_message(ls)
                except Exception as e:
                    ls.log_trace(f"Still cannot load context: {e}")
                    # The error will be stored in context_state by _ensure_context_for_document
        else:
            # Reload the context if it was successfully loaded
            try:
                context = self.context_state.lsp_context.context
                context.load()
                # Create new LSPContext which will have fresh, empty caches
                self.context_state = ContextLoaded(lsp_context=LSPContext(context))
            except Exception as e:
                ls.log_trace(f"Error loading context: {e}")
                self.context_state = ContextFailed(
                    error=e, context=self.context_state.lsp_context.context
                )

        # Send a workspace diagnostic refresh request to the client. This is used to notify the client that the diagnostics have changed.
        ls.lsp.send_request(
            types.WORKSPACE_DIAGNOSTIC_REFRESH,
            WorkspaceDiagnosticRefreshRequest(
                id=self.context_state.version_id,
            ),
        )
        ls.lsp.send_request(
            types.WORKSPACE_INLAY_HINT_REFRESH,
            WorkspaceInlayHintRefreshRequest(
                id=self.context_state.version_id,
            ),
        )

        # Only publish diagnostics if client doesn't support pull diagnostics
        if not self.client_supports_pull_diagnostics:
            if hasattr(self.context_state, "lsp_context"):
                diagnostics = self.context_state.lsp_context.lint_model(uri)
                ls.publish_diagnostics(
                    document_uri,
                    LSPContext.diagnostics_to_lsp_diagnostics(diagnostics),
                )

    def _register_features(self) -> None:
        """Register LSP features on the internal LanguageServer instance."""
        for name, method in self._supported_custom_methods.items():

            def create_function_call(method_func: t.Callable) -> t.Callable:
                def function_call(ls: LanguageServer, params: t.Any) -> t.Dict[str, t.Any]:
                    try:
                        response = method_func(ls, params)
                    except Exception as e:
                        response = CustomMethodResponseBaseClass(response_error=str(e))
                    return response.model_dump(mode="json")

                return function_call

            self.server.feature(name)(create_function_call(method))

        @self.server.command(EXTERNAL_MODEL_UPDATE_COLUMNS)
        def command_external_models_update_columns(ls: LanguageServer, raw: t.Any) -> None:
            try:
                if not isinstance(raw, list):
                    raise ValueError("Invalid command parameters")
                if len(raw) != 1:
                    raise ValueError("Command expects exactly one parameter")
                model_name = raw[0]
                if not isinstance(model_name, str):
                    raise ValueError("Command parameter must be a string")

                context = self._context_get_or_load()
                if not isinstance(context, LSPContext):
                    raise ValueError("Context is not loaded or invalid")
                model = context.context.get_model(model_name)
                if model is None:
                    raise ValueError(f"External model '{model_name}' not found")
                if model._path is None:
                    raise ValueError(f"External model '{model_name}' does not have a file path")
                uri = URI.from_path(model._path)
                updated = context.update_external_model_columns(
                    ls=ls,
                    uri=uri,
                    model_name=model_name,
                )
                if updated:
                    ls.show_message(
                        f"Updated columns for '{model_name}'",
                        types.MessageType.Info,
                    )
                else:
                    ls.show_message(
                        f"Columns for '{model_name}' are already up to date",
                    )
            except Exception as e:
                ls.show_message(f"Error executing command: {e}", types.MessageType.Error)
                return None

        @self.server.feature(types.INITIALIZE)
        def initialize(ls: LanguageServer, params: types.InitializeParams) -> None:
            """Initialize the server when the client connects."""
            try:
                # Check the custom options
                if params.initialization_options:
                    options = InitializationOptions.model_validate(params.initialization_options)
                    if options.project_paths is not None:
                        self.specified_paths = [Path(path) for path in options.project_paths]

                # Check if the client supports pull diagnostics
                if params.capabilities and params.capabilities.text_document:
                    diagnostics = getattr(params.capabilities.text_document, "diagnostic", None)
                    if diagnostics:
                        self.client_supports_pull_diagnostics = True
                        ls.log_trace("Client supports pull diagnostics")
                    else:
                        self.client_supports_pull_diagnostics = False
                        ls.log_trace("Client does not support pull diagnostics")
                else:
                    self.client_supports_pull_diagnostics = False

                if params.workspace_folders:
                    # Store all workspace folders for later use
                    self.workspace_folders = [
                        Path(self._uri_to_path(folder.uri)) for folder in params.workspace_folders
                    ]

                    # Try to find a SQLMesh config file in any workspace folder (only at the root level)
                    for folder_path in self.workspace_folders:
                        # Only check for config files directly in the workspace directory
                        for ext in ("py", "yml", "yaml"):
                            config_path = folder_path / f"config.{ext}"
                            if config_path.exists():
                                if self._create_lsp_context([folder_path]):
                                    loaded_sqlmesh_message(ls)
                                    return  # Exit after successfully loading any config
            except Exception as e:
                ls.log_trace(
                    f"Error initializing SQLMesh context: {e}",
                )

        @self.server.feature(types.TEXT_DOCUMENT_DID_OPEN)
        def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams) -> None:
            uri = URI(params.text_document.uri)
            context = self._context_get_or_load(uri)

            # Only publish diagnostics if client doesn't support pull diagnostics
            if not self.client_supports_pull_diagnostics:
                diagnostics = context.lint_model(uri)
                ls.publish_diagnostics(
                    params.text_document.uri,
                    LSPContext.diagnostics_to_lsp_diagnostics(diagnostics),
                )

        @self.server.feature(types.TEXT_DOCUMENT_DID_SAVE)
        def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams) -> None:
            uri = URI(params.text_document.uri)
            self._reload_context_and_publish_diagnostics(ls, uri, params.text_document.uri)

        @self.server.feature(types.TEXT_DOCUMENT_FORMATTING)
        def formatting(
            ls: LanguageServer, params: types.DocumentFormattingParams
        ) -> t.List[types.TextEdit]:
            """Format the document using SQLMesh `format_model_expressions`."""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)
                document = ls.workspace.get_text_document(params.text_document.uri)
                before = document.source

                target = next(
                    (
                        target
                        for target in chain(
                            context.context._models.values(),
                            context.context._audits.values(),
                        )
                        if target._path is not None
                        and target._path.suffix == ".sql"
                        and (target._path.samefile(uri.to_path()))
                    ),
                    None,
                )
                if target is None:
                    return []
                after = context.context._format(
                    target=target,
                    before=before,
                )
                return [
                    types.TextEdit(
                        range=types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(
                                line=len(document.lines),
                                character=len(document.lines[-1]) if document.lines else 0,
                            ),
                        ),
                        new_text=after,
                    )
                ]
            except Exception as e:
                ls.show_message(f"Error formatting SQL: {e}", types.MessageType.Error)
                return []

        @self.server.feature(types.TEXT_DOCUMENT_HOVER)
        def hover(ls: LanguageServer, params: types.HoverParams) -> t.Optional[types.Hover]:
            """Provide hover information for an object."""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)
                document = ls.workspace.get_text_document(params.text_document.uri)

                references = get_references(context, uri, to_sqlmesh_position(params.position))
                if not references:
                    return None
                reference = references[0]
                if isinstance(reference, CTEReference) or not reference.markdown_description:
                    return None
                return types.Hover(
                    contents=types.MarkupContent(
                        kind=types.MarkupKind.Markdown,
                        value=reference.markdown_description,
                    ),
                    range=to_lsp_range(reference.range),
                )

            except Exception as e:
                ls.log_trace(
                    f"Error getting hover information: {e}",
                )
                return None

        @self.server.feature(types.TEXT_DOCUMENT_INLAY_HINT)
        def inlay_hint(
            ls: LanguageServer, params: types.InlayHintParams
        ) -> t.List[types.InlayHint]:
            """Implement type hints for sql columns as inlay hints"""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)

                start_line = params.range.start.line
                end_line = params.range.end.line
                hints = get_hints(context, uri, start_line, end_line)
                return hints

            except Exception as e:
                return []

        @self.server.feature(types.TEXT_DOCUMENT_DEFINITION)
        def goto_definition(
            ls: LanguageServer, params: types.DefinitionParams
        ) -> t.List[types.LocationLink]:
            """Jump to an object's definition."""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)

                references = get_references(context, uri, to_sqlmesh_position(params.position))
                location_links = []
                for reference in references:
                    # Use target_range if available (CTEs, Macros, and external models in YAML)
                    if isinstance(reference, ModelReference):
                        # Regular SQL models - default to start of file
                        target_range = types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        )
                        target_selection_range = types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        )
                    elif isinstance(reference, ExternalModelReference):
                        # External models may have target_range set for YAML files
                        target_range = types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        )
                        target_selection_range = types.Range(
                            start=types.Position(line=0, character=0),
                            end=types.Position(line=0, character=0),
                        )
                        if reference.target_range is not None:
                            target_range = to_lsp_range(reference.target_range)
                            target_selection_range = to_lsp_range(reference.target_range)
                    else:
                        # CTEs and Macros always have target_range
                        target_range = to_lsp_range(reference.target_range)
                        target_selection_range = to_lsp_range(reference.target_range)

                    if reference.path is not None:
                        location_links.append(
                            types.LocationLink(
                                target_uri=URI.from_path(reference.path).value,
                                target_selection_range=target_selection_range,
                                target_range=target_range,
                                origin_selection_range=to_lsp_range(reference.range),
                            )
                        )
                return location_links
            except Exception as e:
                ls.show_message(f"Error getting references: {e}", types.MessageType.Error)
                return []

        @self.server.feature(types.TEXT_DOCUMENT_REFERENCES)
        def find_references(
            ls: LanguageServer, params: types.ReferenceParams
        ) -> t.Optional[t.List[types.Location]]:
            """Find all references of a symbol (supporting CTEs, models for now)"""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)

                all_references = get_all_references(
                    context, uri, to_sqlmesh_position(params.position)
                )

                # Convert references to Location objects
                locations = [
                    types.Location(uri=URI.from_path(ref.path).value, range=to_lsp_range(ref.range))
                    for ref in all_references
                    if ref.path is not None
                ]

                return locations if locations else None
            except Exception as e:
                ls.show_message(f"Error getting locations: {e}", types.MessageType.Error)
                return None

        @self.server.feature(types.TEXT_DOCUMENT_PREPARE_RENAME)
        def prepare_rename_handler(
            ls: LanguageServer, params: types.PrepareRenameParams
        ) -> t.Optional[types.PrepareRenameResult]:
            """Prepare for rename operation by checking if the symbol can be renamed."""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)
                result = prepare_rename(context, uri, params.position)
                return result
            except Exception as e:
                ls.log_trace(f"Error preparing rename: {e}")
                return None

        @self.server.feature(types.TEXT_DOCUMENT_RENAME)
        def rename_handler(
            ls: LanguageServer, params: types.RenameParams
        ) -> t.Optional[types.WorkspaceEdit]:
            """Perform rename operation on the symbol at the given position."""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)
                workspace_edit = rename_symbol(context, uri, params.position, params.new_name)
                return workspace_edit
            except Exception as e:
                ls.show_message(f"Error performing rename: {e}", types.MessageType.Error)
                return None

        @self.server.feature(types.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT)
        def document_highlight_handler(
            ls: LanguageServer, params: types.DocumentHighlightParams
        ) -> t.Optional[t.List[types.DocumentHighlight]]:
            """Highlight all occurrences of the symbol at the given position."""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)
                highlights = get_document_highlights(context, uri, params.position)
                return highlights
            except Exception as e:
                ls.log_trace(f"Error getting document highlights: {e}")
                return None

        @self.server.feature(types.TEXT_DOCUMENT_DIAGNOSTIC)
        def diagnostic(
            ls: LanguageServer, params: types.DocumentDiagnosticParams
        ) -> types.DocumentDiagnosticReport:
            """Handle diagnostic pull requests from the client."""
            try:
                uri = URI(params.text_document.uri)
                diagnostics, result_id = self._get_diagnostics_for_uri(uri)

                # Check if client provided a previous result ID
                if hasattr(params, "previous_result_id") and params.previous_result_id == result_id:
                    # Return unchanged report if diagnostics haven't changed
                    return types.RelatedUnchangedDocumentDiagnosticReport(
                        kind=types.DocumentDiagnosticReportKind.Unchanged,
                        result_id=str(result_id),
                    )

                return types.RelatedFullDocumentDiagnosticReport(
                    kind=types.DocumentDiagnosticReportKind.Full,
                    items=diagnostics,
                    result_id=str(result_id),
                )
            except Exception as e:
                ls.log_trace(
                    f"Error getting diagnostics: {e}",
                )
                return types.RelatedFullDocumentDiagnosticReport(
                    kind=types.DocumentDiagnosticReportKind.Full,
                    items=[],
                )

        @self.server.feature(types.WORKSPACE_DIAGNOSTIC)
        def workspace_diagnostic(
            ls: LanguageServer, params: types.WorkspaceDiagnosticParams
        ) -> types.WorkspaceDiagnosticReport:
            """Handle workspace-wide diagnostic pull requests from the client."""
            try:
                context = self._context_get_or_load()

                items: t.List[
                    t.Union[
                        types.WorkspaceFullDocumentDiagnosticReport,
                        types.WorkspaceUnchangedDocumentDiagnosticReport,
                    ]
                ] = []

                # Get all SQL and Python model files from the context
                for path, target in context.map.items():
                    if isinstance(target, ModelTarget):
                        uri = URI.from_path(path)
                        diagnostics, result_id = self._get_diagnostics_for_uri(uri)

                        # Check if we have a previous result ID for this file
                        previous_result_id = None
                        if hasattr(params, "previous_result_ids") and params.previous_result_ids:
                            for prev in params.previous_result_ids:
                                if prev.uri == uri.value:
                                    previous_result_id = prev.value
                                    break

                        if previous_result_id and previous_result_id == result_id:
                            # File hasn't changed
                            items.append(
                                types.WorkspaceUnchangedDocumentDiagnosticReport(
                                    kind=types.DocumentDiagnosticReportKind.Unchanged,
                                    result_id=str(result_id),
                                    uri=uri.value,
                                )
                            )
                        else:
                            # File has changed or is new
                            items.append(
                                types.WorkspaceFullDocumentDiagnosticReport(
                                    kind=types.DocumentDiagnosticReportKind.Full,
                                    result_id=str(result_id),
                                    uri=uri.value,
                                    items=diagnostics,
                                )
                            )

                return types.WorkspaceDiagnosticReport(items=items)

            except Exception as e:
                ls.log_trace(f"Error getting workspace diagnostics: {e}")
                error_diagnostic, error = context_error_to_diagnostic(e)
                if error_diagnostic:
                    uri_value, unpacked_diagnostic = error_diagnostic
                    return types.WorkspaceDiagnosticReport(
                        items=[
                            types.WorkspaceFullDocumentDiagnosticReport(
                                kind=types.DocumentDiagnosticReportKind.Full,
                                result_id=self.context_state.version_id,  # No versioning, always fresh
                                uri=uri_value,
                                items=[unpacked_diagnostic],
                            )
                        ]
                    )

                return types.WorkspaceDiagnosticReport(items=[])

        @self.server.feature(types.TEXT_DOCUMENT_CODE_ACTION)
        def code_action(
            ls: LanguageServer, params: types.CodeActionParams
        ) -> t.Optional[t.List[t.Union[types.Command, types.CodeAction]]]:
            try:
                ls.log_trace(f"Codeactionrequest: {params}")
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)
                code_actions = context.get_code_actions(uri, params)
                return code_actions

            except Exception as e:
                ls.log_trace(f"Error getting code actions: {e}")
                return None

        @self.server.feature(types.TEXT_DOCUMENT_CODE_LENS)
        def code_lens(ls: LanguageServer, params: types.CodeLensParams) -> t.List[types.CodeLens]:
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)
                code_lenses = context.get_code_lenses(uri)
                return code_lenses if code_lenses else []
            except Exception as e:
                ls.log_trace(f"Error getting code lenses: {e}")
                return []

        @self.server.feature(
            types.TEXT_DOCUMENT_COMPLETION,
            types.CompletionOptions(trigger_characters=["@"]),  # advertise "@" for macros
        )
        def completion(
            ls: LanguageServer, params: types.CompletionParams
        ) -> t.Optional[types.CompletionList]:
            """Handle completion requests from the client."""
            try:
                uri = URI(params.text_document.uri)
                context = self._context_get_or_load(uri)

                # Get the document content
                content = None
                try:
                    document = ls.workspace.get_text_document(params.text_document.uri)
                    content = document.source
                except Exception:
                    pass

                # Get completions using the existing completions module
                completion_response = LSPContext.get_completions(context, uri, content)

                completion_items = []
                # Add model completions
                for model in completion_response.model_completions:
                    completion_items.append(
                        types.CompletionItem(
                            label=model.name,
                            kind=types.CompletionItemKind.Reference,
                            detail="SQLMesh Model",
                            documentation=types.MarkupContent(
                                kind=types.MarkupKind.Markdown,
                                value=model.description or "No description available",
                            )
                            if model.description
                            else None,
                        )
                    )
                # Add macro completions
                triggered_by_at = (
                    params.context is not None
                    and getattr(params.context, "trigger_character", None) == "@"
                )

                for macro in completion_response.macros:
                    macro_name = macro.name
                    insert_text = macro_name if triggered_by_at else f"@{macro_name}"

                    completion_items.append(
                        types.CompletionItem(
                            label=f"@{macro_name}",
                            insert_text=insert_text,
                            insert_text_format=types.InsertTextFormat.PlainText,
                            filter_text=macro_name,
                            kind=types.CompletionItemKind.Function,
                            detail="SQLMesh Macro",
                            documentation=macro.description,
                        )
                    )

                for keyword in completion_response.keywords:
                    completion_items.append(
                        types.CompletionItem(
                            label=keyword,
                            kind=types.CompletionItemKind.Keyword,
                            detail="SQL Keyword",
                        )
                    )

                return types.CompletionList(
                    is_incomplete=False,
                    items=completion_items,
                )

            except Exception as e:
                get_sql_completions(None, URI(params.text_document.uri))
                return None

    def _get_diagnostics_for_uri(self, uri: URI) -> t.Tuple[t.List[types.Diagnostic], str]:
        """Get diagnostics for a specific URI, returning (diagnostics, result_id).

        Since we no longer track version numbers, we always return 0 as the result_id.
        This means pull diagnostics will always fetch fresh results.
        """
        try:
            context = self._context_get_or_load(uri)
            diagnostics = context.lint_model(uri)
            return LSPContext.diagnostics_to_lsp_diagnostics(
                diagnostics
            ), self.context_state.version_id
        except ConfigError as config_error:
            diagnostic, error = context_error_to_diagnostic(config_error, uri_filter=uri)
            if diagnostic:
                location, diag = diagnostic
                if location == uri.value:
                    return [diag], self.context_state.version_id
            return [], self.context_state.version_id

    def _context_get_or_load(self, document_uri: t.Optional[URI] = None) -> LSPContext:
        state = self.context_state
        if isinstance(state, ContextFailed):
            if isinstance(state.error, str):
                raise Exception(state.error)
            raise state.error
        if isinstance(state, NoContext):
            if self.specified_paths is not None:
                # If specified paths are provided, create context from them
                if self._create_lsp_context(self.specified_paths):
                    loaded_sqlmesh_message(self.server)
            else:
                self._ensure_context_for_document(document_uri)
        if isinstance(self.context_state, ContextLoaded):
            return self.context_state.lsp_context
        raise RuntimeError("Context failed to load")

    def _ensure_context_for_document(
        self,
        document_uri: t.Optional[URI] = None,
    ) -> None:
        """
        Ensure that a context exists for the given document if applicable by searching
        for a config.py or config.yml file in the parent directories.
        """
        if document_uri is not None:
            document_path = document_uri.to_path()
            if document_path.is_file() and document_path.suffix in (".sql", ".py"):
                document_folder = document_path.parent
                if document_folder.is_dir():
                    self._ensure_context_in_folder(document_folder)
                    return

        self._ensure_context_in_folder()

    def _ensure_context_in_folder(self, folder_path: t.Optional[Path] = None) -> None:
        if not isinstance(self.context_state, NoContext):
            return

        # If not found in the provided folder, search through all workspace folders
        for workspace_folder in self.workspace_folders:
            for ext in ("py", "yml", "yaml"):
                config_path = workspace_folder / f"config.{ext}"
                if config_path.exists():
                    if self._create_lsp_context([workspace_folder]):
                        loaded_sqlmesh_message(self.server)
                        return

        #  Then , check the provided folder recursively
        path = folder_path
        if path is None:
            path = Path.cwd()
        while path.is_dir():
            for ext in ("py", "yml", "yaml"):
                config_path = path / f"config.{ext}"
                if config_path.exists():
                    if self._create_lsp_context([path]):
                        loaded_sqlmesh_message(self.server)
                        return

            path = path.parent
            if path == path.parent:
                break

        raise RuntimeError(
            f"No context found in workspaces folders {self.workspace_folders}"
            + (f" or in {folder_path}" if folder_path else "")
        )

    def _create_lsp_context(self, paths: t.List[Path]) -> t.Optional[LSPContext]:
        """Create a new LSPContext instance using the configured context class.

        On success, sets self.context_state to ContextLoaded and returns the created context.

        Args:
            paths: List of paths to pass to the context constructor

        Returns:
            A new LSPContext instance wrapping the created context, or None if creation fails
        """
        try:
            if isinstance(self.context_state, NoContext):
                context = self.context_class(paths=paths)
            elif isinstance(self.context_state, ContextFailed):
                if self.context_state.context:
                    context = self.context_state.context
                    context.load()
                else:
                    # If there's no context (initial creation failed), try creating again
                    context = self.context_class(paths=paths)
            else:
                context = self.context_state.lsp_context.context
                context.load()
            self.context_state = ContextLoaded(lsp_context=LSPContext(context))
            return self.context_state.lsp_context
        except Exception as e:
            # Only show the error message once
            if not self.has_raised_loading_error:
                self.server.show_message(
                    f"Error creating context: {e}",
                    types.MessageType.Error,
                )
                self.has_raised_loading_error = True

            self.server.log_trace(f"Error creating context: {e}")
            # Store the error in context state so subsequent requests show the actual error
            # Try to preserve any partially loaded context if it exists
            context = None
            if isinstance(self.context_state, ContextLoaded):
                context = self.context_state.lsp_context.context
            elif isinstance(self.context_state, ContextFailed) and self.context_state.context:
                context = self.context_state.context
            self.context_state = ContextFailed(error=e, context=context)
            return None

    @staticmethod
    def _uri_to_path(uri: str) -> Path:
        """Convert a URI to a path."""
        return URI(uri).to_path()

    def start(self) -> None:
        """Start the server with I/O transport."""
        logging.basicConfig(level=logging.DEBUG)
        self.server.start_io()


def loaded_sqlmesh_message(ls: LanguageServer) -> None:
    ls.show_message(
        f"Loaded SQLMesh Context",
        types.MessageType.Info,
    )


def main() -> None:
    # Example instantiator that just uses the same signature as your original `Context` usage.
    sqlmesh_server = SQLMeshLanguageServer(context_class=Context)
    sqlmesh_server.start()


if __name__ == "__main__":
    main()
