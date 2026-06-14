"""Contains all the standard rules included with SQLMesh"""

from __future__ import annotations

import typing as t

from sqlglot.expressions import Star
from sqlglot.helper import subclasses

from sqlmesh.core.constants import EXTERNAL_MODELS_YAML
from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core.linter.helpers import (
    TokenPositionDetails,
    get_range_of_model_block,
    read_range_from_string,
)
from sqlmesh.core.linter.rule import (
    Rule,
    RuleViolation,
    Range,
    Fix,
    TextEdit,
    Position,
    CreateFile,
)
from sqlmesh.core.linter.definition import RuleSet
from sqlmesh.core.model import Model, SqlModel, ExternalModel
from sqlmesh.utils.lineage import extract_references_from_query, ExternalModelReference


class NoSelectStar(Rule):
    """Query should not contain SELECT * on its outer most projections, even if it can be expanded."""

    def check_model(self, model: Model) -> t.Optional[RuleViolation]:
        # Only applies to SQL models, as other model types do not have a query.
        if not isinstance(model, SqlModel):
            return None
        if model.query.is_star:
            violation_range = self._get_range(model)
            fixes = self._create_fixes(model, violation_range)
            return self.violation(violation_range=violation_range, fixes=fixes)
        return None

    def _get_range(self, model: SqlModel) -> t.Optional[Range]:
        """Get the range of the violation if available."""
        try:
            if len(model.query.expressions) == 1 and isinstance(model.query.expressions[0], Star):
                return TokenPositionDetails.from_meta(model.query.expressions[0].meta).to_range(
                    None
                )
        except Exception:
            pass

        return None

    def _create_fixes(
        self, model: SqlModel, violation_range: t.Optional[Range]
    ) -> t.Optional[t.List[Fix]]:
        """Create fixes for the SELECT * violation."""
        if not violation_range:
            return None
        columns = model.columns_to_types
        if not columns:
            return None
        path = model._path
        if path is None:
            return None
        new_text = ", ".join(columns.keys())
        return [
            Fix(
                title="Replace SELECT * with explicit column list",
                edits=[
                    TextEdit(
                        path=path,
                        range=violation_range,
                        new_text=new_text,
                    )
                ],
            )
        ]


class InvalidSelectStarExpansion(Rule):
    def check_model(self, model: Model) -> t.Optional[RuleViolation]:
        deps = model.violated_rules_for_query.get(InvalidSelectStarExpansion)
        if not deps:
            return None

        violation_msg = (
            f"SELECT * cannot be expanded due to missing schema(s) for model(s): {deps}. "
            "Run `sqlmesh create_external_models` and / or make sure that the model "
            f"'{model.fqn}' can be rendered at parse time."
        )

        return self.violation(violation_msg)


class AmbiguousOrInvalidColumn(Rule):
    def check_model(self, model: Model) -> t.Optional[RuleViolation]:
        sqlglot_err = model.violated_rules_for_query.get(AmbiguousOrInvalidColumn)
        if not sqlglot_err:
            return None

        violation_msg = (
            f"{sqlglot_err} for model '{model.fqn}', the column may not exist or is ambiguous."
        )

        return self.violation(violation_msg)


class NoMissingAudits(Rule):
    """Model `audits` must be configured to test data quality."""

    def check_model(self, model: Model) -> t.Optional[RuleViolation]:
        if model.audits or model.kind.is_symbolic:
            return None
        if model._path is None or not str(model._path).endswith(".sql"):
            return self.violation()

        try:
            with open(model._path, "r", encoding="utf-8") as file:
                content = file.read()

            range = get_range_of_model_block(content, model.dialect)
            if range:
                return self.violation(violation_range=range)
            return self.violation()
        except Exception:
            return self.violation()


class NoMissingUnitTest(Rule):
    """All models must have a unit test found in the tests/ directory yaml files"""

    def check_model(self, model: Model) -> t.Optional[RuleViolation]:
        #  External models cannot have unit tests
        if isinstance(model, ExternalModel):
            return None

        if model.name not in self.context.models_with_tests:
            return self.violation(
                violation_msg=f"Model {model.name} is missing unit test(s). Please add in the tests/ directory."
            )
        return None


class NoMissingExternalModels(Rule):
    """All external models must be registered in the external_models.yaml file"""

    def check_model(
        self, model: Model
    ) -> t.Optional[t.Union[RuleViolation, t.List[RuleViolation]]]:
        # Ignore external models themselves, because either they are registered,
        # and if they are not, they will be caught as referenced in another model.
        if isinstance(model, ExternalModel):
            return None

        # Handle other models that may refer to the external models.
        not_registered_external_models: t.Set[str] = set()
        for depends_on_model in model.depends_on:
            existing_model = self.context.get_model(depends_on_model)
            if existing_model is None:
                not_registered_external_models.add(depends_on_model)

        if not not_registered_external_models:
            return None

        # If the model is anything other than a sql model that and has a path
        # that ends with .sql, we cannot extract the references from the query.
        path = model._path
        if not isinstance(model, SqlModel) or not path or not str(path).endswith(".sql"):
            return self._standard_error_message(
                model_name=model.fqn,
                external_models=not_registered_external_models,
            )

        with open(path, "r", encoding="utf-8") as file:
            read_file = file.read()
        split_read_file = read_file.splitlines()

        # If there are any unregistered external models, return a violation find
        # the ranges for them.
        references = extract_references_from_query(
            query=model.query,
            context=self.context,
            document_path=path,
            read_file=split_read_file,
            depends_on=model.depends_on,
            dialect=model.dialect,
        )
        external_references = {
            normalize_model_name(
                table=read_range_from_string(read_file, ref.range),
                default_catalog=model.default_catalog,
                dialect=model.dialect,
            ): ref
            for ref in references
            if isinstance(ref, ExternalModelReference) and ref.path is None
        }

        # Ensure that depends_on and external references match.
        if not_registered_external_models != set(external_references.keys()):
            return self._standard_error_message(
                model_name=model.fqn,
                external_models=not_registered_external_models,
            )

        # Return a violation for each unregistered external model with its range.
        violations = []
        for ref_name, ref in external_references.items():
            if ref_name in not_registered_external_models:
                fix = self.create_fix(ref_name)
                violations.append(
                    RuleViolation(
                        rule=self,
                        violation_msg=f"Model '{model.fqn}' depends on unregistered external model '{ref_name}'. "
                        "Please register it in the external models file. This can be done by running 'sqlmesh create_external_models'.",
                        violation_range=ref.range,
                        fixes=[fix] if fix else [],
                    )
                )

        if len(violations) < len(not_registered_external_models):
            return self._standard_error_message(
                model_name=model.fqn,
                external_models=not_registered_external_models,
            )

        return violations

    def _standard_error_message(
        self, model_name: str, external_models: t.Set[str]
    ) -> RuleViolation:
        return RuleViolation(
            rule=self,
            violation_msg=f"Model '{model_name}' depends on unregistered external models: "
            f"{', '.join(m for m in external_models)}. "
            "Please register them in the external models file. This can be done by running 'sqlmesh create_external_models'.",
        )

    def create_fix(self, model_name: str) -> t.Optional[Fix]:
        """
        Add an external model to the external models file.
        - If no external models file exists, it will create one with the model.
        - If the model already exists, it will not add it again.
        """
        root = self.context.path
        if not root:
            return None

        external_models_path = root / EXTERNAL_MODELS_YAML
        if not external_models_path.exists():
            return Fix(
                title="Add external model file",
                edits=[],
                create_files=[
                    CreateFile(
                        path=external_models_path,
                        text=f"- name: '{model_name}'\n",
                    )
                ],
            )

        # Figure out the position to insert the new external model at the end of the file, whether
        # needs new line or not.
        with open(external_models_path, "r", encoding="utf-8") as file:
            lines = file.read()

        # If a file ends in newline, we can add the new model directly.
        split_lines = lines.splitlines()
        if lines.endswith("\n"):
            new_text = f"- name: '{model_name}'\n"
            position = Position(line=len(split_lines), character=0)
        else:
            new_text = f"\n- name: '{model_name}'\n"
            position = Position(
                line=len(split_lines) - 1, character=len(split_lines[-1]) if split_lines else 0
            )

        return Fix(
            title="Add external model",
            edits=[
                TextEdit(
                    path=external_models_path,
                    range=Range(start=position, end=position),
                    new_text=new_text,
                )
            ],
        )


class NoAmbiguousProjections(Rule):
    """All projections in a model must have unique & inferrable names or explicit aliases."""

    def check_model(self, model: Model) -> t.Optional[RuleViolation]:
        query = model.render_query()
        if query is None:
            return None

        name_counts: t.Dict[str, int] = {}
        projection_list = query.selects
        for expression in projection_list:
            alias = expression.output_name
            if alias == "*":
                continue

            if not alias:
                return self.violation(
                    f"Outer projection '{expression.sql(dialect=model.dialect)}' must have inferrable names or explicit aliases."
                )

            name_counts[alias] = name_counts.get(alias, 0) + 1

        for name, count in name_counts.items():
            if count > 1:
                return self.violation(f"Found duplicate outer select name '{name}'")

        return None


_RULE_EXCLUDE: t.Set[t.Type[Rule]] = {Rule}  # type: ignore[type-abstract]
BUILTIN_RULES = RuleSet(subclasses(__name__, Rule, exclude=_RULE_EXCLUDE))
