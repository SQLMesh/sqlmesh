from __future__ import annotations

import typing as t
from collections import deque

from sqlglot import exp

from sqlmesh.utils.errors import ConfigError, SQLMeshError
from sqlmesh.utils.pydantic import PydanticModel

if t.TYPE_CHECKING:
    from sqlmesh.core.model import Model


class Reference(PydanticModel, frozen=True):
    model_name: str
    expression: exp.Expr
    unique: bool = False
    _name: str = ""

    @property
    def columns(self) -> t.List[str]:
        expression = self.expression
        if isinstance(expression, exp.Alias):
            expression = expression.this
        expression = expression.unnest()
        if isinstance(expression, (exp.Array, exp.Tuple)):
            return [e.output_name for e in expression.expressions]
        return [expression.output_name]

    @property
    def name(self) -> str:
        if not self._name:
            keys = []

            if isinstance(self.expression, (exp.Tuple, exp.Array)):
                for e in self.expression.expressions:
                    if not e.output_name:
                        raise ConfigError(
                            f"Reference '{e}' must have an inferrable name or explicit alias."
                        )
                    keys.append(e.output_name)
            elif self.expression.output_name:
                keys.append(self.expression.output_name)
            else:
                raise ConfigError(
                    f"Reference '{self.expression}' must have an inferrable name or explicit alias."
                )

            self._name = "__".join(keys)
        return self._name


class ReferenceGraph:
    def __init__(self, models: t.Iterable[Model]):
        self._model_refs: t.Dict[str, t.Dict[str, Reference]] = {}
        self._ref_models: t.Dict[str, t.Set[str]] = {}
        self._dim_models: t.Dict[str, t.Set[str]] = {}

        for model in models:
            self.add_model(model)

    def add_model(self, model: Model) -> None:
        """Add a model and its references to the graph.

        Args:
            model: the model to add.
        """
        for column in model.columns_to_types or {}:
            self._dim_models.setdefault(column, set())
            self._dim_models[column].add(model.name)

        for ref in model.all_references:
            self._model_refs.setdefault(model.name, {})
            self._model_refs[model.name][ref.name] = ref
            self._ref_models.setdefault(ref.name, set())
            self._ref_models[ref.name].add(model.name)

    def models_for_column(self, source: str, column: str, max_depth: int = 3) -> t.List[str]:
        """Find all the models with a column that join to a source within max_depth.

        Args:
            source: The source model.
            column: The column to look for.
            max_depth: The maximum number of models to join to find a path.

        Returns:
            The list of models that fit the criteria of the search.
        """
        models = []

        for model in self._dim_models[column]:
            try:
                if model != source:
                    self.find_path(source, model)
                models.append(model)
            except SQLMeshError:
                pass

        return sorted(models)

    def find_path(self, source: str, target: str, max_depth: int = 3) -> t.List[Reference]:
        """Find a path from source model to target model with max depth.

        Args:
            source: The source model.
            target: The target model.
            max_depth: The maximum number of models to join to find a path.

        Returns:
            The list of references representing the join path of source to target.
        """
        if source not in self._model_refs:
            return []

        queue = deque(([ref] for ref in self._model_refs[source].values()))

        while queue:
            path = queue.popleft()
            visited = set()
            many = False

            for ref in path:
                visited.add(ref.model_name)
                many = many or not ref.unique

            ref_name = path[-1].name

            for model_name in sorted(self._ref_models[ref_name]):
                for ref in self._model_refs[model_name].values():
                    # paths cannot have loops or contain many to many refs
                    if model_name in visited or (many and not ref.unique):
                        continue

                    new_path = path + [ref]

                    if model_name == target:
                        return new_path

                    if len(new_path) < max_depth:
                        queue.append(new_path)

        raise SQLMeshError(
            f"Cannot find path between '{source}' and '{target}'. Make sure that references/grains are configured and that a many to many join is not occurring."
        )
