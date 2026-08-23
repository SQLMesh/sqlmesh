import typing as t
from pathlib import Path

from pydantic import Field

from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core.linter.helpers import (
    TokenPositionDetails,
)
from sqlmesh.core.linter.rule import Range, Position
from sqlmesh.core.model.definition import SqlModel, ExternalModel, PythonModel, SeedModel
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope

from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from ruamel.yaml import YAML

from sqlmesh.utils.pydantic import PydanticModel

if t.TYPE_CHECKING:
    from sqlmesh.core.context import Context
    from sqlmesh.core.context import GenericContext


class ModelReference(PydanticModel):
    """A reference to a model, excluding external models."""

    type: t.Literal["model"] = "model"
    path: Path
    range: Range
    markdown_description: t.Optional[str] = None


class ExternalModelReference(PydanticModel):
    """A reference to an external model."""

    type: t.Literal["external_model"] = "external_model"
    range: Range
    target_range: t.Optional[Range] = None
    path: t.Optional[Path] = None
    """The path of the external model, typically a YAML file, it is optional because
    external models can be unregistered and so the path is not available."""

    markdown_description: t.Optional[str] = None


class CTEReference(PydanticModel):
    """A reference to a CTE."""

    type: t.Literal["cte"] = "cte"
    path: Path
    range: Range
    target_range: Range


class MacroReference(PydanticModel):
    """A reference to a macro."""

    type: t.Literal["macro"] = "macro"
    path: Path
    range: Range
    target_range: Range
    markdown_description: t.Optional[str] = None


Reference = t.Annotated[
    t.Union[ModelReference, CTEReference, MacroReference, ExternalModelReference],
    Field(discriminator="type"),
]


def extract_references_from_query(
    query: exp.Expr,
    context: t.Union["Context", "GenericContext[t.Any]"],
    document_path: Path,
    read_file: t.List[str],
    depends_on: t.Set[str],
    dialect: t.Optional[str] = None,
) -> t.List[Reference]:
    # Build a scope tree to properly handle nested CTEs
    try:
        query = normalize_identifiers(query.copy(), dialect=dialect)
        root_scope = build_scope(query)
    except Exception:
        root_scope = None

    references: t.List[Reference] = []
    if not root_scope:
        return references

    # Traverse all scopes to find CTE definitions and table references
    for scope in root_scope.traverse():
        for table in scope.tables:
            table_name = table.name

            # Check if this table reference is a CTE in the current scope
            if cte_scope := scope.cte_sources.get(table_name):
                if cte_scope.expression is None:
                    continue
                cte = cte_scope.expression.parent
                if cte is None:
                    continue
                alias = cte.args["alias"]
                if isinstance(alias, exp.TableAlias):
                    identifier = alias.this
                    if isinstance(identifier, exp.Identifier):
                        target_range_sqlmesh = TokenPositionDetails.from_meta(
                            identifier.meta
                        ).to_range(read_file)
                        table_range_sqlmesh = TokenPositionDetails.from_meta(
                            table.this.meta
                        ).to_range(read_file)

                        references.append(
                            CTEReference(
                                path=document_path,  # Same file
                                range=table_range_sqlmesh,
                                target_range=target_range_sqlmesh,
                            )
                        )

                        column_references = _process_column_references(
                            scope=scope,
                            reference_name=table.name,
                            read_file=read_file,
                            referenced_model_path=document_path,
                            description="",
                            reference_type="cte",
                            cte_target_range=target_range_sqlmesh,
                        )
                        references.extend(column_references)
                continue

            # For non-CTE tables, process these as before (external model references)
            # Normalize the table reference
            unaliased = table.copy()
            if unaliased.args.get("alias") is not None:
                unaliased.set("alias", None)
            reference_name = unaliased.sql(dialect=dialect)
            try:
                normalized_reference_name = normalize_model_name(
                    reference_name,
                    default_catalog=context.default_catalog,
                    dialect=dialect,
                )
                if normalized_reference_name not in depends_on:
                    continue
            except Exception:
                # Skip references that cannot be normalized
                continue

            # Get the referenced model uri
            referenced_model = context.get_model(
                model_or_snapshot=normalized_reference_name, raise_if_missing=False
            )
            if referenced_model is None:
                # Extract metadata for positioning
                table_meta = TokenPositionDetails.from_meta(table.this.meta)
                table_range_sqlmesh = table_meta.to_range(read_file)
                start_pos_sqlmesh = table_range_sqlmesh.start
                end_pos_sqlmesh = table_range_sqlmesh.end

                # If there's a catalog or database qualifier, adjust the start position
                catalog_or_db = table.args.get("catalog") or table.args.get("db")
                if catalog_or_db is not None:
                    catalog_or_db_meta = TokenPositionDetails.from_meta(catalog_or_db.meta)
                    catalog_or_db_range_sqlmesh = catalog_or_db_meta.to_range(read_file)
                    start_pos_sqlmesh = catalog_or_db_range_sqlmesh.start

                references.append(
                    ExternalModelReference(
                        range=Range(
                            start=start_pos_sqlmesh,
                            end=end_pos_sqlmesh,
                        ),
                        markdown_description="Unregistered external model",
                    )
                )
                continue
            referenced_model_path = referenced_model._path
            if referenced_model_path is None:
                continue
            # Check whether the path exists
            if not referenced_model_path.is_file():
                continue

            # Extract metadata for positioning
            table_meta = TokenPositionDetails.from_meta(table.this.meta)
            table_range_sqlmesh = table_meta.to_range(read_file)
            start_pos_sqlmesh = table_range_sqlmesh.start
            end_pos_sqlmesh = table_range_sqlmesh.end

            # If there's a catalog or database qualifier, adjust the start position
            catalog_or_db = table.args.get("catalog") or table.args.get("db")
            if catalog_or_db is not None:
                catalog_or_db_meta = TokenPositionDetails.from_meta(catalog_or_db.meta)
                catalog_or_db_range_sqlmesh = catalog_or_db_meta.to_range(read_file)
                start_pos_sqlmesh = catalog_or_db_range_sqlmesh.start

            description = generate_markdown_description(referenced_model)

            # For external models in YAML files, find the specific model block
            if isinstance(referenced_model, ExternalModel):
                yaml_target_range: t.Optional[Range] = None
                if (
                    referenced_model_path.suffix in (".yaml", ".yml")
                    and referenced_model_path.is_file()
                ):
                    yaml_target_range = _get_yaml_model_range(
                        referenced_model_path, referenced_model.name
                    )
                references.append(
                    ExternalModelReference(
                        path=referenced_model_path,
                        range=Range(
                            start=start_pos_sqlmesh,
                            end=end_pos_sqlmesh,
                        ),
                        markdown_description=description,
                        target_range=yaml_target_range,
                    )
                )

                column_references = _process_column_references(
                    scope=scope,
                    reference_name=normalized_reference_name,
                    read_file=read_file,
                    referenced_model_path=referenced_model_path,
                    description=description,
                    yaml_target_range=yaml_target_range,
                    reference_type="external_model",
                    default_catalog=context.default_catalog,
                    dialect=dialect,
                )
                references.extend(column_references)
            else:
                references.append(
                    ModelReference(
                        path=referenced_model_path,
                        range=Range(
                            start=start_pos_sqlmesh,
                            end=end_pos_sqlmesh,
                        ),
                        markdown_description=description,
                    )
                )

                column_references = _process_column_references(
                    scope=scope,
                    reference_name=normalized_reference_name,
                    read_file=read_file,
                    referenced_model_path=referenced_model_path,
                    description=description,
                    reference_type="model",
                    default_catalog=context.default_catalog,
                    dialect=dialect,
                )
                references.extend(column_references)

    return references


def generate_markdown_description(
    model: t.Union[SqlModel, ExternalModel, PythonModel, SeedModel],
) -> t.Optional[str]:
    description = model.description
    columns = model.columns_to_types
    column_descriptions = model.column_descriptions

    if columns is None:
        return description or None

    columns_table = "\n".join(
        [
            f"| {column} | {column_type} | {column_descriptions.get(column, '')} |"
            for column, column_type in columns.items()
        ]
    )

    table_header = "| Column | Type | Description |\n|--------|------|-------------|\n"
    columns_text = table_header + columns_table
    return f"{description}\n\n{columns_text}" if description else columns_text


def _process_column_references(
    scope: t.Any,
    reference_name: str,
    read_file: t.List[str],
    referenced_model_path: Path,
    description: t.Optional[str] = None,
    yaml_target_range: t.Optional[Range] = None,
    reference_type: t.Literal["model", "external_model", "cte"] = "model",
    default_catalog: t.Optional[str] = None,
    dialect: t.Optional[str] = None,
    cte_target_range: t.Optional[Range] = None,
) -> t.List[Reference]:
    """
    Process column references for a given table and create appropriate reference objects.

    Args:
        scope: The SQL scope to search for columns
        reference_name: The full reference name (may include database/catalog)
        read_file: The file content as list of lines
        referenced_model_path: Path of the referenced model
        description: Markdown description for the reference
        yaml_target_range: Target range for external models (YAML files)
        reference_type: Type of reference - "model", "external_model", or "cte"
        default_catalog: Default catalog for normalization
        dialect: SQL dialect for normalization
        cte_target_range: Target range for CTE references

    Returns:
        List of table references for column usages
    """

    references: t.List[Reference] = []
    for column in scope.find_all(exp.Column):
        if column.table:
            if reference_type == "cte":
                if column.table == reference_name:
                    table_range = _get_column_table_range(column, read_file)
                    references.append(
                        CTEReference(
                            path=referenced_model_path,
                            range=table_range,
                            target_range=cte_target_range,
                        )
                    )
            else:
                table_parts = [part.sql(dialect) for part in column.parts[:-1]]
                table_ref = ".".join(table_parts)
                normalized_reference_name = normalize_model_name(
                    table_ref,
                    default_catalog=default_catalog,
                    dialect=dialect,
                )
                if normalized_reference_name == reference_name:
                    table_range = _get_column_table_range(column, read_file)
                    if reference_type == "external_model":
                        references.append(
                            ExternalModelReference(
                                path=referenced_model_path,
                                range=table_range,
                                markdown_description=description,
                                target_range=yaml_target_range,
                            )
                        )
                    else:
                        references.append(
                            ModelReference(
                                path=referenced_model_path,
                                range=table_range,
                                markdown_description=description,
                            )
                        )

    return references


def _get_column_table_range(column: exp.Column, read_file: t.List[str]) -> Range:
    """
    Get the range for a column's table reference, handling both simple and qualified table names.

    Args:
        column: The column expression
        read_file: The file content as list of lines

    Returns:
        The Range covering the table reference in the column
    """

    table_parts = column.parts[:-1]

    start_range = TokenPositionDetails.from_meta(table_parts[0].meta).to_range(read_file)
    end_range = TokenPositionDetails.from_meta(table_parts[-1].meta).to_range(read_file)

    return Range(
        start=start_range.start,
        end=end_range.end,
    )


def _get_yaml_model_range(path: Path, model_name: str) -> t.Optional[Range]:
    """
    Find the range of a specific model block in a YAML file.

    Args:
        yaml_path: Path to the YAML file
        model_name: Name of the model to find

    Returns:
        The Range of the model block in the YAML file, or None if not found
    """
    model_name_ranges = get_yaml_model_name_ranges(path)
    if model_name_ranges is None:
        return None
    return model_name_ranges.get(model_name, None)


def get_yaml_model_name_ranges(path: Path) -> t.Optional[t.Dict[str, Range]]:
    """
    Get the ranges of all model names in a YAML file.

    Args:
        path: Path to the YAML file

    Returns:
        A dictionary mapping model names to their ranges in the YAML file.
    """
    yaml = YAML()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)

    if not isinstance(data, list):
        return None

    model_name_ranges = {}
    for item in data:
        if isinstance(item, dict):
            position_data = item.lc.data["name"]  # type: ignore
            start = Position(line=position_data[2], character=position_data[3])
            end = Position(line=position_data[2], character=position_data[3] + len(item["name"]))
            name = item.get("name")
            if not name:
                continue
            model_name_ranges[name] = Range(start=start, end=end)

    return model_name_ranges
