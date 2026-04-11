from __future__ import annotations

import fnmatch
import typing as t
from pathlib import Path
from itertools import zip_longest
import abc

from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.tokens import Token, TokenType, Tokenizer as BaseTokenizer
from sqlglot.dialects.dialect import Dialect, DialectType
from sqlglot.helper import seq_get

from sqlmesh.core import constants as c
from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core.environment import Environment
from sqlmesh.core.model import update_model_schemas
from sqlmesh.core.audit import StandaloneAudit
from sqlmesh.utils import UniqueKeyDict
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.git import GitClient
from sqlmesh.utils.errors import SQLMeshError


if t.TYPE_CHECKING:
    from typing_extensions import Literal as Lit  # noqa
    from sqlmesh.core.model import Model
    from sqlmesh.core.node import Node
    from sqlmesh.core.state_sync import StateReader


class Selector(abc.ABC):
    def __init__(
        self,
        state_reader: StateReader,
        models: UniqueKeyDict[str, Model],
        context_path: Path = Path("."),
        dag: t.Optional[DAG[str]] = None,
        default_catalog: t.Optional[str] = None,
        dialect: t.Optional[str] = None,
        cache_dir: t.Optional[Path] = None,
    ):
        self._state_reader = state_reader
        self._models = models
        self._context_path = context_path
        self._cache_dir = cache_dir if cache_dir else context_path / c.CACHE
        self._default_catalog = default_catalog
        self._dialect = dialect
        self._git_client = GitClient(context_path)

        if dag is None:
            self._dag: DAG[str] = DAG()
            for fqn, model in models.items():
                self._dag.add(fqn, model.depends_on)
        else:
            self._dag = dag

    def select_models(
        self,
        model_selections: t.Iterable[str],
        target_env_name: str,
        fallback_env_name: t.Optional[str] = None,
        ensure_finalized_snapshots: bool = False,
    ) -> t.Tuple[UniqueKeyDict[str, Model], t.Set[str]]:
        """Given a set of selections returns models from the current state with names matching the
        selection while sourcing the remaining models from the target environment.

        Args:
            model_selections: A set of selections.
            target_env_name: The name of the target environment.
            fallback_env_name: The name of the fallback environment that will be used if the target
                environment doesn't exist.
            ensure_finalized_snapshots: Whether to source environment snapshots from the latest finalized
                environment state, or to use whatever snapshots are in the current environment state even if
                the environment is not finalized.

        Returns:
            A tuple of (models dict, set of all matched FQNs including env models).
        """
        env_models = self._load_env_models(
            target_env_name, fallback_env_name, ensure_finalized_snapshots
        )

        all_selected_models = self.expand_model_selections(
            model_selections, models={**env_models, **self._models}
        )

        dag: DAG[str] = DAG()
        subdag = set()

        for fqn in all_selected_models:
            if fqn not in subdag:
                subdag.add(fqn)
                subdag.update(self._dag.downstream(fqn))

        models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        all_model_fqns = set(self._models) | set(env_models)
        needs_update = False

        def get_model(fqn: str) -> t.Optional[Model]:
            if fqn not in all_selected_models and fqn in env_models:
                # Unselected modified or added model.
                model_from_env = env_models[fqn]
                try:
                    # this triggers a render_query() which can throw an exception
                    model_from_env.depends_on
                    return model_from_env
                except Exception as e:
                    raise SQLMeshError(
                        f"Model '{model_from_env.name}' sourced from state cannot be rendered "
                        f"in the local environment due to:\n> {str(e)}"
                    ) from e
            if fqn in all_selected_models and fqn in self._models:
                # Selected modified or removed model.
                return self._models[fqn]
            return None

        for fqn in all_model_fqns:
            model = get_model(fqn)

            if not model:
                continue

            if model.fqn in subdag:
                dag.add(model.fqn, model.depends_on)

                for dep in model.depends_on:
                    schema = model.mapping_schema

                    for part in exp.to_table(dep).parts:
                        schema = schema.get(part.sql()) or {}

                    parent = get_model(dep)

                    parent_schema = {
                        c: t.sql(dialect=model.dialect)
                        for c, t in ((parent and parent.columns_to_types) or {}).items()
                    }

                    if schema != parent_schema:
                        model = model.copy(update={"mapping_schema": {}})
                        needs_update = True
                        break

            models[model.fqn] = model

        if needs_update:
            update_model_schemas(dag, models=models, cache_dir=self._cache_dir)

        return models, all_selected_models

    def _load_env_models(
        self,
        target_env_name: str,
        fallback_env_name: t.Optional[str] = None,
        ensure_finalized_snapshots: bool = False,
    ) -> t.Dict[str, "Model"]:
        """Loads models from the target environment, falling back to the fallback environment if needed."""
        target_env = self._state_reader.get_environment(Environment.sanitize_name(target_env_name))
        if target_env and target_env.expired:
            target_env = None

        if not target_env and fallback_env_name:
            target_env = self._state_reader.get_environment(
                Environment.sanitize_name(fallback_env_name)
            )

        if not target_env:
            return {}

        environment_snapshot_infos = (
            target_env.snapshots
            if not ensure_finalized_snapshots
            else target_env.finalized_or_current_snapshots
        )
        return {
            s.name: s.model
            for s in self._state_reader.get_snapshots(environment_snapshot_infos).values()
            if s.is_model
        }

    def expand_model_selections(
        self, model_selections: t.Iterable[str], models: t.Optional[t.Dict[str, Node]] = None
    ) -> t.Set[str]:
        """Expands a set of model selections into a set of model fqns that can be looked up in the Context.

        Args:
            model_selections: A set of model selections.

        Returns:
            A set of model fqns.
        """

        node = parse(" | ".join(f"({s})" for s in model_selections))

        all_models: t.Dict[str, Node] = models or dict(self._models)
        models_by_tags: t.Dict[str, t.Set[str]] = {}

        for fqn, model in all_models.items():
            for tag in model.tags:
                tag = tag.lower()
                models_by_tags.setdefault(tag, set())
                models_by_tags[tag].add(model.fqn)

        def evaluate(node: exp.Expr) -> t.Set[str]:
            if isinstance(node, exp.Var):
                pattern = node.this
                if "*" in pattern:
                    return {
                        fqn
                        for fqn, model in all_models.items()
                        if fnmatch.fnmatchcase(self._model_name(model), node.this)
                    }
                return self._pattern_to_model_fqns(pattern, all_models)
            if isinstance(node, exp.And):
                return evaluate(node.left) & evaluate(node.right)
            if isinstance(node, exp.Or):
                return evaluate(node.left) | evaluate(node.right)
            if isinstance(node, exp.Paren):
                return evaluate(node.this)
            if isinstance(node, exp.Not):
                return set(all_models) - evaluate(node.this)
            if isinstance(node, Git):
                target_branch = node.name
                git_modified_files = {
                    *self._git_client.list_untracked_files(),
                    *self._git_client.list_uncommitted_changed_files(),
                    *self._git_client.list_committed_changed_files(target_branch=target_branch),
                }
                return {m.fqn for m in all_models.values() if m._path in git_modified_files}
            if isinstance(node, Tag):
                pattern = node.name.lower()

                if "*" in pattern:
                    return {
                        model
                        for tag, models in models_by_tags.items()
                        for model in models
                        if fnmatch.fnmatchcase(tag, pattern)
                    }
                return models_by_tags.get(pattern, set())
            if isinstance(node, ResourceType):
                resource_type = node.name.lower()
                return {
                    fqn
                    for fqn, model in all_models.items()
                    if self._matches_resource_type(resource_type, model)
                }
            if isinstance(node, Direction):
                selected = set()

                for model_name in evaluate(node.this):
                    selected.add(model_name)
                    if node.args.get("up"):
                        for u in self._dag.upstream(model_name):
                            if u in all_models:
                                selected.add(u)
                    if node.args.get("down"):
                        selected.update(self._dag.downstream(model_name))
                return selected
            raise ParseError(f"Unexpected node {node}")

        return evaluate(node)

    @abc.abstractmethod
    def _model_name(self, model: Node) -> str:
        """Given a model, return the name that a selector pattern contining wildcards should be fnmatch'd on"""
        pass

    @abc.abstractmethod
    def _pattern_to_model_fqns(self, pattern: str, all_models: t.Dict[str, Node]) -> t.Set[str]:
        """Given a pattern, return the keys of the matching models from :all_models"""
        pass

    @abc.abstractmethod
    def _matches_resource_type(self, resource_type: str, model: Node) -> bool:
        """Indicate whether or not the supplied model matches the supplied resource type"""
        pass


class NativeSelector(Selector):
    """Implementation of selectors that matches objects based on SQLMesh native names"""

    def _model_name(self, model: Node) -> str:
        return model.name

    def _pattern_to_model_fqns(self, pattern: str, all_models: t.Dict[str, Node]) -> t.Set[str]:
        fqn = normalize_model_name(pattern, self._default_catalog, self._dialect)
        return {fqn} if fqn in all_models else set()

    def _matches_resource_type(self, resource_type: str, model: Node) -> bool:
        if resource_type == "model":
            return model.is_model
        if resource_type == "audit":
            return isinstance(model, StandaloneAudit)

        raise SQLMeshError(f"Unsupported resource type: {resource_type}")


class DbtSelector(Selector):
    """Implementation of selectors that matches objects based on the DBT names instead of the SQLMesh native names"""

    def _model_name(self, model: Node) -> str:
        if dbt_fqn := model.dbt_fqn:
            return dbt_fqn
        raise SQLMeshError("dbt node information must be populated to use dbt selectors")

    def _pattern_to_model_fqns(self, pattern: str, all_models: t.Dict[str, Node]) -> t.Set[str]:
        # a pattern like "staging.customers" should match a model called "jaffle_shop.staging.customers"
        # but not a model called "jaffle_shop.customers.staging"
        # also a pattern like "aging" should not match "staging" so we need to consider components; not substrings
        pattern_components = pattern.split(".")
        first_pattern_component = pattern_components[0]
        matches = set()
        for fqn, model in all_models.items():
            if not model.dbt_fqn:
                continue

            dbt_fqn_components = model.dbt_fqn.split(".")
            try:
                starting_idx = dbt_fqn_components.index(first_pattern_component)
            except ValueError:
                continue
            for pattern_component, fqn_component in zip_longest(
                pattern_components, dbt_fqn_components[starting_idx:]
            ):
                if pattern_component and not fqn_component:
                    # the pattern still goes but we have run out of fqn components to match; no match
                    break
                if fqn_component and not pattern_component:
                    # all elements of the pattern have matched elements of the fqn; match
                    matches.add(fqn)
                    break
                if pattern_component != fqn_component:
                    # the pattern explicitly doesnt match a component; no match
                    break
            else:
                # called if no explicit break, indicating all components of the pattern matched all components of the fqn
                matches.add(fqn)
        return matches

    def _matches_resource_type(self, resource_type: str, model: Node) -> bool:
        """
        ref: https://docs.getdbt.com/reference/node-selection/methods#resource_type

        # supported by SQLMesh
        "model"
        "seed"
        "source" # external model
        "test" # standalone audit

        # not supported by SQLMesh yet, commented out to throw an error if someone tries to use them
        "analysis"
        "exposure"
        "metric"
        "saved_query"
        "semantic_model"
        "snapshot"
        "unit_test"
        """
        if resource_type not in ("model", "seed", "source", "test"):
            raise SQLMeshError(f"Unsupported resource type: {resource_type}")

        if isinstance(model, StandaloneAudit):
            return resource_type == "test"

        if resource_type == "model":
            return model.is_model and not model.kind.is_external and not model.kind.is_seed
        if resource_type == "source":
            return model.kind.is_external
        if resource_type == "seed":
            return model.kind.is_seed

        return False


class SelectorDialect(Dialect):
    IDENTIFIERS_CAN_START_WITH_DIGIT = True

    class Tokenizer(BaseTokenizer):
        SINGLE_TOKENS = {
            "(": TokenType.L_PAREN,
            ")": TokenType.R_PAREN,
            "&": TokenType.AMP,
            "|": TokenType.PIPE,
            "^": TokenType.CARET,
            "+": TokenType.PLUS,
            "*": TokenType.STAR,
            ":": TokenType.COLON,
        }

        KEYWORDS = {}
        IDENTIFIERS = ["\\"]  # there are no identifiers but need to put something here
        IDENTIFIER_START = ""
        IDENTIFIER_END = ""


class Git(exp.Expression):
    pass


class Tag(exp.Expression):
    pass


class ResourceType(exp.Expression):
    pass


class Direction(exp.Expression):
    pass


def parse(selector: str, dialect: DialectType = None) -> exp.Expr:
    tokens = SelectorDialect().tokenize(selector)
    i = 0

    def _curr() -> t.Optional[Token]:
        return seq_get(tokens, i)

    def _prev() -> Token:
        return tokens[i - 1]

    def _advance(num: int = 1) -> Token:
        nonlocal i
        i += num
        return _prev()

    def _next() -> t.Optional[Token]:
        return seq_get(tokens, i + 1)

    def _error(msg: str) -> str:
        return f"{msg} at index {i}: {selector}"

    def _match(token_type: TokenType, raise_unmatched: bool = False) -> t.Optional[Token]:
        token = _curr()
        if token and token.token_type == token_type:
            return _advance()
        if raise_unmatched:
            raise ParseError(_error(f"Expected {token_type}"))
        return None

    def _parse_kind(kind: str) -> bool:
        token = _curr()
        next_token = _next()

        if (
            token
            and token.token_type == TokenType.VAR
            and token.text.lower() == kind
            and next_token
            and next_token.token_type == TokenType.COLON
        ):
            _advance(2)
            return True
        return False

    def _parse_var() -> exp.Expr:
        upstream = _match(TokenType.PLUS)
        downstream = None
        tag = _parse_kind("tag")
        resource_type = False if tag else _parse_kind("resource_type")
        git = False if resource_type else _parse_kind("git")
        lstar = "*" if _match(TokenType.STAR) else ""
        directions = {}

        if _match(TokenType.VAR) or _match(TokenType.NUMBER):
            name = _prev().text
            rstar = "*" if _match(TokenType.STAR) else ""
            downstream = _match(TokenType.PLUS)
            this: exp.Expr = exp.Var(this=f"{lstar}{name}{rstar}")

        elif _match(TokenType.L_PAREN):
            this = exp.Paren(this=_parse_conjunction())
            downstream = _match(TokenType.PLUS)
            _match(TokenType.R_PAREN, True)
        elif lstar:
            this = exp.var("*")
        else:
            raise ParseError(_error("Expected model name."))

        if upstream:
            directions["up"] = True
        if downstream:
            directions["down"] = True

        if tag:
            this = Tag(this=this)
        if resource_type:
            this = ResourceType(this=this)
        if git:
            this = Git(this=this)
        if directions:
            this = Direction(this=this, **directions)
        return this

    def _parse_unary() -> exp.Expr:
        if _match(TokenType.CARET):
            return exp.Not(this=_parse_unary())
        return _parse_var()

    def _parse_conjunction() -> exp.Expr:
        this = _parse_unary()

        if _match(TokenType.AMP):
            this = exp.And(this=this, expression=_parse_unary())
        if _match(TokenType.PIPE):
            this = exp.Or(this=this, expression=_parse_conjunction())

        return this

    return _parse_conjunction()
