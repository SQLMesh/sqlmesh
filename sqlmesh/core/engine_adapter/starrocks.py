from __future__ import annotations

import logging
import re
import sqlglot
from sqlglot import exp
import typing as t

from sqlmesh.core.engine_adapter.base import (
    InsertOverwriteStrategy,
    get_source_columns_to_types,
)
from sqlmesh.core.engine_adapter.mixins import (
    ClusteredByMixin,
    LogicalMergeMixin,
    PandasNativeFetchDFSupportMixin,
)
from sqlmesh.core.engine_adapter.shared import (
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    set_catalog,
    to_schema,
)
from sqlmesh.core.node import IntervalUnit
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import QueryOrDF

logger = logging.getLogger(__name__)


###############################################################################
# Declarative Type System for Property Validation and Normalization
###############################################################################
"""
Declarative type system for property validation and normalization.

This module provides a declarative way to define property types with clear separation
between validation (type checking) and normalization (type conversion).
"""
Validated = t.Any  # validated intermediate value (AST nodes, string, list...)
Normalized = t.Any  # final normalized output

# Allowed outputs for EnumType normalize / or general property outputs.
PROPERTY_OUTPUT_TYPES = {
    "str",  # "HASH"
    "var",  # exp.Var("ASYNC")
    "identifier",  # exp.Identifier
    "literal",  # exp.Literal.string("HASH")
    "column",  # exp.Column(this="HASH")
    "ast_expr",  # generic exp.Expression
}


# ============================================================
# Fragment parser (robust-ish)
# ============================================================
def parse_fragment(text: str) -> t.Union[exp.Expression, t.List[exp.Expression]]:
    """
    Try to parse a DSL fragment into SQLGlot AST(s).

    Behavior:
    1. If parse_one succeeds, return the exp.Expression.
    2. If fails but text contains comma, split by commas and parse each part.
    3. If it's parenthesized like "(a, b)", parse and return exp.Tuple or list.
    4. If it's a simple token like "IDENT", return exp.Identifier.
    """
    if isinstance(text, exp.Expression):
        return text

    if not isinstance(text, str):
        raise TypeError("parse_fragment expects a string")

    s = text.strip()
    try:
        parsed = sqlglot.parse_one(s)
        return parsed
    except Exception:
        raise ValueError(f"Unable to parse fragment: {s}")


# ============================================================
# Base Type
# ============================================================
class DeclarativeType:
    """
    Base class for declarative type system.

    Design Philosophy:
    -----------------
    - validate(value): Type checking only - returns validated intermediate value or None
    - normalize(validated): Type conversion only - transforms to target output format

    Methods:
    --------
    validate(value) -> Optional[Validated]
        Check if value conforms to this type, maybe include some tiny different types
        Returns: Validated intermediate value if valid, None otherwise.

    normalize(validated) -> Normalized
        Convert validated intermediate value to final output format.
        Returns: Normalized value in target format.

    __call__(value) -> Normalized
        Convenience method: validate + normalize in one step.
    """

    def validate(self, value: t.Any) -> t.Optional[Validated]:
        """Check if value conforms to this type. Return validated value or None.
        String that can be parsed as literal
        """
        raise NotImplementedError(f"{self.__class__.__name__}.validate() must be implemented")

    def normalize(self, validated: Validated) -> Normalized:
        """Convert validated intermediate value to final output format."""
        # Default: identity transformation
        return validated

    def __call__(self, value: t.Any) -> Normalized:
        """Validate and normalize in one step."""
        validated = self.validate(value)
        if validated is None:
            raise ValueError(f"Value {value!r} does not conform to type {self.__class__.__name__}")
        return self.normalize(validated)


# ============================================================
# Primitive Types
# ============================================================
class StringType(DeclarativeType):
    """
    String type validator.

    Accepts:
    - Python str only

    Validation: Returns the string if valid, None otherwise.
    Normalization: Returns the string as-is (identity).
    """

    def __init__(self, normalized_type: str = "str"):
        """
        Args:
            normalized_type: Target type for normalization.
                - "literal": Convert to exp.Literal.string()
                - "str": Keep as string (default)
                - "identifier": Convert to exp.Identifier
        """
        self.normalized_type = normalized_type

    def validate(self, value: t.Any) -> t.Optional[str]:
        """Check if value is a Python string. Returns string or None."""
        return value if isinstance(value, str) else None

    def normalize(self, validated: str) -> str:
        """Return string as-is (identity normalization)."""
        return validated


class LiteralType(DeclarativeType):
    """
    Literal type validator.

    Accepts:
    - exp.Literal only (from AST)
    - String that can be parsed as literal

    Validation: Returns exp.Literal if valid, None otherwise.
    Normalization: Converts to target type based on normalized_type parameter.
    """

    def __init__(self, normalized_type: t.Optional[str] = None):
        """
        Args:
            normalized_type: Target type for normalization.
                - None: Keep as exp.Literal (default)
                - "literal": Keep as exp.Literal
                - "str": Convert to Python string
        """
        self.normalized_type = normalized_type

    def validate(self, value: t.Any) -> t.Optional[exp.Literal]:
        """Check if value is a literal type. Returns exp.Literal or None."""
        # Try parsing string first
        if isinstance(value, str):
            try:
                value = parse_fragment(value)
            except Exception:
                return None

        # Check if it's a Literal
        if isinstance(value, exp.Literal):
            return value

        return None

    def normalize(self, validated: exp.Literal) -> t.Union[exp.Literal, str]:
        """Convert to target type based on normalized_type."""
        if self.normalized_type == "str":
            return validated.this
        # None or "literal" - keep as-is
        return validated


class IdentifierType(DeclarativeType):
    """
    Identifier type validator.

    Accepts:
    - exp.Identifier only
    - String that can be parsed as identifier

    Validation: Returns exp.Identifier if valid, None otherwise.
    Normalization: Converts to target type based on normalized_type parameter.
    """

    def __init__(self, normalized_type: t.Optional[str] = None):
        """
        Args:
            normalized_type: Target type for normalization.
                - None: Keep as exp.Identifier (default)
                - "literal": Convert to exp.Literal.string()
                - "str": Convert to Python string
                - "identifier": Keep as exp.Identifier
                - "column": Convert to exp.Column
        """
        self.normalized_type = normalized_type

    def validate(self, value: t.Any) -> t.Optional[exp.Identifier]:
        """Check if value is an identifier type. Returns exp.Identifier or None."""
        # Try parsing string first
        if isinstance(value, str):
            try:
                value = parse_fragment(value)
            except Exception:
                return None

        # Check if it's an Identifier
        if isinstance(value, exp.Identifier):
            return value

        return None

    def normalize(
        self, validated: exp.Identifier
    ) -> t.Union[exp.Identifier, exp.Column, exp.Literal, str]:
        """Convert to target type based on normalized_type."""
        if self.normalized_type == "column":
            return exp.column(validated.this)
        if self.normalized_type == "literal":
            return exp.Literal.string(validated.this)
        if self.normalized_type == "str":
            return validated.this
        # None or "identifier" - keep as-is
        return validated


class ColumnType(DeclarativeType):
    """
    Column type validator.

    Accepts:
    - exp.Column only
    - String that can be parsed as column

    Validation: Returns exp.Column if valid, None otherwise.
    Normalization: Converts to target type based on normalized_type parameter.
    """

    def __init__(self, normalized_type: t.Optional[str] = None):
        """
        Args:
            normalized_type: Target type for normalization.
                - None: Keep as exp.Column (default)
                - "literal": Convert to exp.Literal.string()
                - "str": Convert to Python string
                - "identifier": Convert to exp.Identifier
                - "column": Keep as exp.Column
        """
        self.normalized_type = normalized_type

    def validate(self, value: t.Any) -> t.Optional[exp.Column]:
        """Check if value is a column type. Returns exp.Column or None."""
        # Try parsing string first
        if isinstance(value, str):
            try:
                value = parse_fragment(value)
            except Exception:
                return None

        # Check if it's a Column
        if isinstance(value, exp.Column):
            return value

        return None

    def normalize(
        self, validated: exp.Column
    ) -> t.Union[exp.Column, exp.Identifier, exp.Literal, str]:
        """Convert to target type based on normalized_type."""
        if self.normalized_type == "identifier":
            return exp.Identifier(this=validated.this)
        if self.normalized_type == "literal":
            return exp.Literal.string(validated.this)
        if self.normalized_type == "str":
            return str(validated.this)
        # None or "column" - keep as-is
        return validated


class EqType(DeclarativeType):
    """
    EQ expression type validator (key=value pairs).

    Accepts:
    - exp.EQ(left, right)
    - String that can be parsed as key=value

    Validation: Returns (key_name, value_expr) tuple if valid, None otherwise.
    Normalization: Returns the (key, value) tuple as-is.
    """

    def validate(self, value: t.Any) -> t.Optional[t.Tuple[str, t.Any]]:
        """Check if value is an EQ expression. Returns (key, value) tuple or None."""
        # Try parsing string first
        if isinstance(value, str):
            try:
                value = parse_fragment(value)
            except Exception:
                return None

        # Check if it's an EQ expression
        if isinstance(value, exp.EQ):
            # Extract key name from left side
            left = value.this
            # Extract value from right side
            right = value.expression

            key_name = None
            if isinstance(left, exp.Column):
                key_name = left.this.name if hasattr(left.this, "name") else str(left.this)
            elif isinstance(left, exp.Identifier):
                key_name = left.this
            elif isinstance(left, str):
                key_name = left
            else:
                key_name = str(left)

            return (key_name, right)

        return None

    def normalize(self, validated: t.Tuple[str, t.Any]) -> t.Tuple[str, t.Any]:
        """Return (key, value) tuple as-is (identity normalization)."""
        return validated


class EnumType(DeclarativeType):
    """
    Enumerated value type validator.

    Accepts values from a predefined set of allowed values.
    Following input types are allowed:
    - str
    - exp.Literal
    - exp.Var
    - exp.Identifier
    - exp.Column

    Parameters:
    -----------
    valid_values : t.Sequence[str]
        List of allowed values (e.g., ["HASH", "RANDOM"])
    normalized_type : t.Optional[str]
        Target type for normalization:
        - "str": Python string (default)
        - "identifier": exp.Identifier
        - "literal": exp.Literal.string()
        - "column": exp.Column
        - "ast_expr": generic exp.Expression (defaults to Identifier)
    case_sensitive : bool
        Whether to perform case-sensitive matching (default: False)

    Validation: Checks if value is in allowed set, returns canonical string.
    Normalization: Converts to specified target type.
    """

    def __init__(
        self,
        valid_values: t.Sequence[str],
        normalized_type: str = "str",
        case_sensitive: bool = False,
    ):
        self.valid_values = list(valid_values)
        self.case_sensitive = bool(case_sensitive)
        self.normalized_type = normalized_type

        if self.normalized_type is not None and self.normalized_type not in PROPERTY_OUTPUT_TYPES:
            raise ValueError(
                f"normalized_type must be one of {PROPERTY_OUTPUT_TYPES}, got {self.normalized_type!r}"
            )

        # Pre-compute normalized values for efficient lookup
        self._values_normalized = [v if case_sensitive else v.upper() for v in self.valid_values]

    def _extract_text(self, value: t.Any) -> t.Optional[str]:
        """Extract text from various value types."""
        if isinstance(value, str):
            return value
        if isinstance(value, (exp.Literal, exp.Var)):
            return str(value.this)
        if isinstance(value, (exp.Identifier, exp.Column)):
            # For Identifier/Column, this might be another Expression
            if isinstance(value.this, str):
                return value.this
            elif hasattr(value.this, "name"):  # noqa: RET505
                return str(value.this.name)
            else:
                return str(value.this)
        return None

    def _normalize_text(self, text: str) -> str:
        """Normalize text for comparison based on case sensitivity."""
        return text if self.case_sensitive else text.upper()

    def validate(self, value: t.Any) -> t.Optional[str]:
        """Check if value is in the allowed enum set. Returns canonical string or None."""
        # Try parsing string first
        if isinstance(value, str):
            try:
                parsed = parse_fragment(value)
                # If parsed successfully, extract text from AST node
                if isinstance(parsed, (exp.Identifier, exp.Literal, exp.Column)):
                    value = parsed
            except Exception:
                # If parsing fails, treat as plain string
                pass

        # Extract text from value
        text = self._extract_text(value)

        if text is None:
            return None

        # Normalize and check against allowed values
        normalized_text = self._normalize_text(text)
        if normalized_text in self._values_normalized:
            return normalized_text

        return None

    def normalize(self, validated: str) -> Normalized:
        """Convert validated enum string to target type."""
        # validated is already canonical (e.g., "HASH")
        if self.normalized_type is None or self.normalized_type == "str":
            return validated
        if self.normalized_type == "var":
            return exp.Var(this=validated)
        if self.normalized_type == "literal":
            return exp.Literal.string(validated)
        if self.normalized_type == "identifier":
            return exp.Identifier(this=validated)
        if self.normalized_type == "column":
            return exp.Column(this=validated)
        if self.normalized_type == "ast_expr":
            return exp.Identifier(this=validated)

        # Fallback to string
        return validated


class FuncType(DeclarativeType):
    """
    Function type validator.

    Accepts:
    - exp.Func (built-in functions like date_trunc, CAST, etc.)
    - exp.Anonymous (custom/dialect functions like RANGE, LIST)
    - String that can be parsed as function call

    Validation: Returns exp.Func or exp.Anonymous if valid, None otherwise.
    Normalization: Returns the function expression as-is (identity).

    Examples:
        date_trunc('day', col1)     → exp.Func
        RANGE(col1, col2)           → exp.Anonymous
        LIST(region, status)        → exp.Anonymous
    """

    def validate(self, value: t.Any) -> t.Optional[t.Union[exp.Func, exp.Anonymous]]:
        """Check if value is a function type. Returns exp.Func/exp.Anonymous or None."""
        # Try parsing string first
        if isinstance(value, str):
            try:
                value = parse_fragment(value)
            except Exception:
                return None

        # Check if it's a Func or Anonymous function
        if isinstance(value, (exp.Func, exp.Anonymous)):
            return value

        return None

    def normalize(
        self, validated: t.Union[exp.Func, exp.Anonymous]
    ) -> t.Union[exp.Func, exp.Anonymous]:
        """Return function expression as-is (identity normalization)."""
        return validated


# ============================================================
# AnyOf (combinator)
# ============================================================
class AnyOf(DeclarativeType):
    """
    Union type - accepts first matching subtype.

    This is a combinator type that tries each subtype in order and accepts
    the first one that validates successfully.

    Validation: Tries each subtype, returns (matched_type, validated_value) tuple.
    Normalization: Uses the matched subtype's normalize method.
    """

    def __init__(self, *types: DeclarativeType):
        if not types:
            raise ValueError("AnyOf requires at least one type")

        # Validate all types are DeclarativeType instances
        for type_ in types:
            if not isinstance(type_, DeclarativeType):
                raise TypeError(f"AnyOf expects DeclarativeType instances, got {type_!r}")

        self.types: t.List[DeclarativeType] = list(types)

    def validate(self, value: t.Any) -> t.Optional[t.Tuple[DeclarativeType, Validated]]:
        """Try each subtype in order, return (matched_type, validated_value) or None."""
        for sub_type in self.types:
            validated = sub_type.validate(value)
            if validated is not None:
                # Return both the matched type and validated value
                return (sub_type, validated)

        # No type matched
        return None

    def normalize(self, validated: t.Tuple[DeclarativeType, Validated]) -> Normalized:
        """Normalize using the matched subtype's normalize method."""
        matched_type, validated_value = validated
        return matched_type.normalize(validated_value)


# ============================================================
# SequenceOf (Tuple/List/Paren/Single -> normalized list/tuple)
# ============================================================
class SequenceOf(DeclarativeType):
    """
    Sequence/List type validator with built-in union type support.

    Accepts various sequence representations and validates each element against
    one or more possible types (similar to AnyOf for each element).
    Optionally accepts single elements (promoted to single-item lists).

    Accepts:
    - exp.Tuple: (a, b, c)
    - exp.Array: [a, b, c]
    - exp.Paren: (a) or ((a, b))
    - Python list/tuple: [a, b] or (a, b)
    - String: "a, b, c" (parsed)
    - Single element: a (if allow_single=True, promoted to [a])

    Validation: Returns list of (matched_type, validated_value) tuples or None.
    Normalization: Returns list of normalized elements using matched type's normalize.

    Examples:
        # Single type
        SequenceOf(ColumnType())

        # Multiple types (union) - each element tries types in order
        SequenceOf(ColumnType(), IdentifierType(), LiteralType())

        # Allow single element
        SequenceOf(ColumnType(), allow_single=True)

        # Multiple types + allow single
        SequenceOf(ColumnType(), IdentifierType(), allow_single=True)
    """

    def __init__(
        self,
        *elem_types: DeclarativeType,
        allow_single: bool = False,
        output_as: str = "list",
    ):
        """
        Args:
            *elem_types: One or more type validators for elements.
                        If multiple types provided, each element tries types in order (AnyOf behavior).
            allow_single: Whether to accept single elements (promoted to list). Default: False.
            output_as: Output format - "list" or "tuple". Default: "list".
        """
        if not elem_types:
            raise ValueError("SequenceOf requires at least one element type")

        self.elem_types: t.List[DeclarativeType] = list(elem_types)
        self.allow_single = allow_single
        self.output_as = output_as

    def validate(self, value: t.Any) -> t.Optional[t.List[t.Tuple[DeclarativeType, Validated]]]:
        """Validate each element in the sequence. Returns list of (matched_type, validated_value) tuples or None."""
        # Extract elements from various container types
        elems = self._extract_elements(value)
        if elems is None:
            return None

        # Validate each element against all possible types (AnyOf behavior)
        validated_items: t.List[t.Tuple[DeclarativeType, Validated]] = []
        for elem in elems:
            # Try each type until one matches
            matched = False
            for elem_type in self.elem_types:
                validated = elem_type.validate(elem)
                if validated is not None:
                    validated_items.append((elem_type, validated))
                    matched = True
                    break

            # If no type matched, the whole sequence fails if any element fails
            if not matched:
                return None

        return validated_items

    def normalize(
        self, validated: t.List[t.Tuple[DeclarativeType, Validated]]
    ) -> t.Union[t.List[Normalized], t.Tuple[Normalized, ...]]:
        """Normalize each validated element using its matched type's normalize method."""
        normalized_items = [elem_type.normalize(value) for elem_type, value in validated]

        # Convert to desired output format
        if self.output_as == "tuple":
            return tuple(normalized_items)
        return normalized_items  # default: list

    def _extract_elements(self, value: t.Any) -> t.Optional[t.List[t.Any]]:
        """
        Extract elements from various container representations.
        Returns list of raw elements or None if extraction fails.
        """
        # Python list/tuple - process first before string parsing
        if isinstance(value, (list, tuple)):
            return list(value)

        # Try parsing string for AST types
        if isinstance(value, str):
            try:
                value = parse_fragment(value)
            except Exception:
                # If parsing fails and we accept single strings, promote to list
                if self.allow_single and any(isinstance(t, StringType) for t in self.elem_types):
                    return [value]
                return None

        # SQL Tuple: (a, b, c)
        if isinstance(value, exp.Tuple):
            return list(value.expressions)

        # SQL Array: [a, b, c]
        if isinstance(value, exp.Array):
            return list(value.expressions)

        # SQL Paren: (a) or ((a, b))
        if isinstance(value, exp.Paren):
            inner = value.this
            if isinstance(inner, exp.Tuple):
                return list(inner.expressions)
            return [inner]

        # Single AST element: promote to list (if allow_single)
        if self.allow_single and isinstance(value, exp.Expression):
            return [value]

        return None


# ============================================================
# Field Definition for Structured Types
# ============================================================
class Field:
    """
    Field specification for StructuredTupleType.

    Defines validation rules, types, and metadata for a single field.

    Args:
        type: DeclarativeType instance for validating field value
        required: Whether this field is required (default: False)
        aliases: List of alternative field names (default: [])
        doc: Documentation string for this field

    Example:
        Field(
            type=EnumType(["HASH", "RANDOM"]),
            required=True,
            aliases=["distribution_type"],
            doc="Distribution kind: HASH or RANDOM"
        )
    """

    def __init__(
        self,
        type: DeclarativeType,
        required: bool = False,
        aliases: t.Optional[t.List[str]] = None,
        doc: t.Optional[str] = None,
    ):
        self.type = type
        self.required = required
        self.aliases = aliases or []
        self.doc = doc


# ============================================================
# StructuredTupleType - Base class for typed tuples
# ============================================================
class StructuredTupleType(DeclarativeType):
    """
    Base class for validating tuples with typed fields.

    Subclasses define FIELDS dict to specify structure:

    FIELDS = {
        "field_name": Field(
            type=SomeType(),
            required=True,
            aliases=["alt_name1", "alt_name2"]
        ),
        ...
    }

    Validation Process:
    1. Parse tuple into key=value pairs (exp.EQ)
    2. Match keys against FIELDS (including aliases)
    3. Validate each field value with specified type
    4. Check required fields are present
    5. Handle unknown/invalid fields based on error flags

    Returns: Dict[str, Any] with canonical field names as keys

    Example:
        class DistributionTupleInputType(StructuredTupleType):
            FIELDS = {
                "kind": Field(type=EnumType(["HASH", "RANDOM"]), required=True),
                "columns": Field(type=SequenceOf(ColumnType())),
            }

    Args:
        error_on_unknown_field: If True, raise error when encountering unknown fields.
                                If False, silently skip unknown fields (default: False)
        error_on_invalid_field: If True, raise error when field value validation fails.
                                If False, return None for entire validation (default: True)
    """

    FIELDS: t.Dict[str, Field] = {}  # Subclasses override this

    def __init__(self, error_on_unknown_field: bool = True, error_on_invalid_field: bool = True):
        self.error_on_unknown_field = error_on_unknown_field
        self.error_on_invalid_field = error_on_invalid_field

        # Build alias mapping: alias -> canonical_name
        self._alias_map: t.Dict[str, str] = {}
        for field_name, field_spec in self.FIELDS.items():
            # Map canonical name to itself
            self._alias_map[field_name] = field_name
            # Map aliases to canonical name
            for alias in field_spec.aliases:
                self._alias_map[alias] = field_name

    def validate(
        self, value: t.Any
    ) -> t.Optional[t.Dict[str, t.Tuple[DeclarativeType, Validated]]]:
        """
        Validate structured tuple.

        Returns: Dict mapping canonical field names to (matched_type, validated_value) tuples,
                 or None if validation fails.

        Raises:
            ValueError: If error_on_unknown_field=True and unknown field encountered
            ValueError: If error_on_invalid_field=True and field validation fails
        """
        # Try parsing string first
        if isinstance(value, str):
            try:
                value = parse_fragment(value)
            except Exception:
                return None

        # Extract key=value pairs from tuple/paren
        pairs = self._extract_pairs(value)
        if pairs is None:
            return None

        # Validate each pair and build result dict
        result: t.Dict[str, t.Tuple[DeclarativeType, Validated]] = {}
        eq_type = EqType()

        for pair_expr in pairs:
            # Validate as EQ expression
            eq_validated = eq_type.validate(pair_expr)
            if eq_validated is None:
                continue  # Skip non-EQ expressions

            key, value_expr = eq_validated

            # Resolve alias to canonical name
            canonical_name = self._alias_map.get(key)
            if canonical_name is None:
                # Unknown field
                if self.error_on_unknown_field:
                    raise ValueError(
                        f"Unknown field '{key}' in {self.__class__.__name__}. "
                        f"Valid fields: {list(self.FIELDS.keys())}"
                    )
                # Skip unknown field
                continue

            # Get field spec
            field_spec = self.FIELDS[canonical_name]

            # Validate field value with specified type
            validated_value = field_spec.type.validate(value_expr)
            if validated_value is None:
                # Field validation failed
                if self.error_on_invalid_field:
                    raise ValueError(
                        f"Invalid value for field '{canonical_name}': {value_expr}. "
                        f"Expected type: {field_spec.type.__class__.__name__}, "
                        f"Actual type: {type(value_expr).__name__}"
                    )
                # Return None for entire validation
                return None

            # Store with canonical name
            result[canonical_name] = (field_spec.type, validated_value)

        # Check required fields
        for field_name, field_spec in self.FIELDS.items():
            if field_spec.required and field_name not in result:
                # Required field missing
                if self.error_on_invalid_field:
                    raise ValueError(
                        f"Required field '{field_name}' is missing in {self.__class__.__name__}"
                    )
                return None

        return result

    def normalize(
        self, validated: t.Dict[str, t.Tuple[DeclarativeType, Validated]]
    ) -> t.Dict[str, Normalized]:
        """
        Normalize validated fields.

        Returns: Dict mapping canonical field names to normalized values.
        """
        return {
            field_name: field_type.normalize(value)
            for field_name, (field_type, value) in validated.items()
        }

    def _extract_pairs(self, value: t.Any) -> t.Optional[t.List[t.Any]]:
        """
        Extract list of expressions from tuple/paren.
        Each expression should be an exp.EQ (key=value).
        """
        # exp.Tuple: (a=1, b=2)
        if isinstance(value, list):
            return value
        if isinstance(value, exp.Tuple):
            return list(value.expressions)

        # exp.Paren: (a=1) or ((a=1, b=2))
        if isinstance(value, exp.Paren):
            inner = value.this
            if isinstance(inner, exp.Tuple):
                return list(inner.expressions)
            return [inner]

        return None


class DistributionTupleInputType(StructuredTupleType):
    """
    StarRocks distribution tuple validator.

    Accepts:
    - (kind='HASH', columns=(id, dt), buckets=10)
    - (kind='HASH', expressions=(id, dt), bucket_num=10)
    - (kind='RANDOM')

    Returns: Dict with fields:
        - kind: "HASH" or "RANDOM" (string)
        - columns: List[exp.Column] (optional, for HASH)
        - buckets: exp.Literal (optional)

    Field Aliases:
        - columns: expressions
        - buckets: bucket, bucket_num

    Examples:
        Input:  (kind='HASH', columns=(id, dt), buckets=10)
        Output: {
            'kind': 'HASH',
            'columns': [exp.Column('id'), exp.Column('dt')],
            'buckets': exp.Literal.number(10)
        }

        Input:  (kind='RANDOM')
        Output: {'kind': 'RANDOM'}

    Conversion:
        Use factory methods to convert normalized values to unified dict format:
        - from_enum(): Convert EnumType normalized value (str) → dict
        - from_func(): Convert FuncType normalized value (exp.Func) → dict
        - to_unified_dict(): Convert any normalized value → dict
    """

    FIELDS = {
        "kind": Field(
            type=EnumType(["HASH", "RANDOM"], normalized_type="str"),
            required=True,
            doc="Distribution type: HASH or RANDOM",
        ),
        "columns": Field(
            type=SequenceOf(
                ColumnType(),
                IdentifierType(normalized_type="column"),
                allow_single=True,
            ),
            required=False,
            aliases=["expressions"],
            doc="Columns for HASH distribution",
        ),
        "buckets": Field(
            type=AnyOf(LiteralType(), StringType(normalized_type="literal")),
            required=False,
            aliases=["bucket", "bucket_num"],
            doc="Number of buckets",
        ),
    }


class DistributionTupleOutputType(StructuredTupleType):
    """
    Output validator for distribution tuple.

    Used to validate normalized distribution values which are already dicts.
    Overrides validate() to handle dict input directly (for output validation),
    while parent class handles tuple/string input (for input validation).
    """

    FIELDS = {
        "kind": Field(
            type=EnumType(["HASH", "RANDOM"]),
            required=True,
        ),
        "columns": Field(
            type=SequenceOf(ColumnType(), allow_single=False),
            required=False,
        ),
        "buckets": Field(
            type=LiteralType(),
            required=False,
        ),
    }

    def validate(self, value: t.Any) -> t.Optional[t.Dict[str, t.Any]]:
        """
        Validate a distribution value for OUTPUT validation.

        For output validation, accepts:
        - dict: Validate structure directly (normalized output)
        - tuple/string: Delegate to parent class (for completeness)

        Returns: The dict if valid, None otherwise
        """
        # For output validation, handle dict directly
        if isinstance(value, dict):
            # Validate required 'kind' field
            kind = value.get("kind")
            if kind is None:
                return None

            # Validate 'kind' is a valid enum value
            kind_spec = self.FIELDS["kind"].type
            if kind_spec.validate(kind) is None:
                return None

            # Validate 'columns' if present
            columns = value.get("columns")
            if columns is not None:
                columns_spec = self.FIELDS["columns"].type
                if columns_spec.validate(columns) is None:
                    return None

            # Validate 'buckets' if present
            buckets = value.get("buckets")
            if buckets is not None:
                buckets_spec = self.FIELDS["buckets"].type
                if buckets_spec.validate(buckets) is None:
                    return None

            return value

        # For tuple/string, delegate to parent class
        return super().validate(value)

    # ============================================================
    # Factory methods for conversion from other normalized types
    # ============================================================

    @staticmethod
    def from_enum(enum_value: str, buckets: t.Optional[int] = None) -> t.Dict[str, t.Any]:
        """
        Create distribution dict from EnumType normalized value.

        Args:
            enum_value: "RANDOM" (from EnumType)
            buckets: Optional bucket count

        Returns:
            Dict with kind/columns/buckets fields

        Example:
            >>> DistributionTupleOutputType.from_enum("RANDOM")
            {'kind': 'RANDOM', 'columns': [], 'buckets': None}
        """
        return {"kind": enum_value, "columns": [], "buckets": buckets}

    @staticmethod
    def from_func(
        func: t.Union[exp.Func, exp.Anonymous], buckets: t.Optional[int] = None
    ) -> t.Dict[str, t.Any]:
        """
        Create distribution dict from FuncType normalized value.

        Args:
            func: HASH(id, dt) or RANDOM() (from FuncType)
            buckets: Optional bucket count

        Returns:
            Dict with kind/columns/buckets fields

        Example:
            >> func = sqlglot.parse_one("HASH(id, dt)")
            >> DistributionTupleOutputType.from_func(func)
            {"kind": "HASH", "columns": [exp.Column("id"), exp.Column("dt")], "buckets": None}
        """
        func_name = func.name.upper() if hasattr(func, "name") else str(func.this).upper()

        if func_name == "HASH":
            # Extract columns from HASH(col1, col2, ...)
            columns: list[exp.Column] = [func.this] if isinstance(func.this, exp.Column) else []
            columns.extend(func.expressions)
            return {"kind": "HASH", "columns": columns, "buckets": buckets}
        elif func_name == "RANDOM":  # noqa: RET505
            return {"kind": "RANDOM", "columns": [], "buckets": buckets}
        else:
            raise ValueError(f"Unknown distribution function: {func_name}")

    @staticmethod
    def to_unified_dict(
        normalized_value: t.Any, buckets: t.Optional[int] = None
    ) -> t.Dict[str, t.Any]:
        """
        Convert any normalized distribution value to unified dict format.

        This is a convenience method that dispatches to appropriate factory method.

        Args:
            normalized_value: Result from DistributedByInputSpec normalization
                             (dict | str | exp.Func)
            buckets: Optional bucket count override

        Returns:
            Unified dict with kind/columns/buckets fields

        Raises:
            TypeError: If value type is not supported

        Example:
            >>> # From DistributionTupleOutputType
            >>> DistributionTupleOutputType.to_unified_dict({"kind": "HASH", "columns": [...]})
            {'kind': 'HASH', 'columns': [Ellipsis]}

            >>> # From EnumType
            >>> DistributionTupleOutputType.to_unified_dict("RANDOM")
            {'kind': 'RANDOM', 'columns': [], 'buckets': None}

            >> # From FuncType
            >> DistributionTupleOutputType.to_unified_dict(sqlglot.parse_one("HASH(id)"))
            {'kind': 'HASH', 'columns': [exp.Column('id')], 'buckets': None}
        """
        if isinstance(normalized_value, dict):
            # Already in DistributionTupleInputType format
            return normalized_value
        elif isinstance(normalized_value, str):  # noqa: RET505
            # From EnumType: "RANDOM"
            return DistributionTupleOutputType.from_enum(normalized_value, buckets)
        elif isinstance(normalized_value, (exp.Func, exp.Anonymous)):
            # From FuncType: HASH(id, dt)
            return DistributionTupleOutputType.from_func(normalized_value, buckets)
        else:
            raise TypeError(
                f"Cannot convert {type(normalized_value).__name__} to distribution dict. "
                f"Expected dict, str, or exp.Func/exp.Anonymous."
            )


# ============================================================
# Type Specifications for StarRocks Properties (INPUT and OUTPUT)
# ============================================================
class PropertySpecs:
    # Accepts:
    # - Single column: id
    # - Multiple columns: (id, dt)
    # - String for string input: "id, dt" (will be auto-wrapped and parsed by preprocess_parentheses)
    GeneralColumnListInputSpec = SequenceOf(
        ColumnType(),
        StringType(normalized_type="column"),
        IdentifierType(normalized_type="column"),
        allow_single=True,
    )

    # TableKey: Simple key specification (primary_key, duplicate_key, unique_key, aggregate_key)
    # Accepts:
    # - Single column: id
    # - Multiple columns: (id, dt)
    TableKeyInputSpec = GeneralColumnListInputSpec

    # Partitioned By: Flexible partition specification
    # Accepts:
    # - Single column: col1
    # - Multiple columns: (col1, col2)
    # - Mixed: (col1, "col2") - string will be parsed
    # - RANGE(col1) or RANGE(col1, col2)
    # - LIST(col1) or LIST(col1, col2)
    # - Expression: (date_trunc('day', col1), col2)
    PartitionedByInputSpec = SequenceOf(
        ColumnType(),
        StringType(normalized_type="column"),
        IdentifierType(normalized_type="column"),
        FuncType(),  # RANGE(), LIST(), date_trunc(), etc.
        allow_single=True,
    )

    # Partitions: List of partition definitions (strings)
    # Accepts:
    # - Single partition: 'PARTITION p1 VALUES LESS THAN ("2024-01-01")'
    # - Multiple partitions: ('PARTITION p1 ...', 'PARTITION p2 ...')
    # Note: Single string is auto-promoted to list
    PartitionsInputSpec = SequenceOf(
        StringType(), LiteralType(normalized_type="str"), allow_single=True
    )

    # Distribution: StarRocks distribution specification
    # Accepts:
    # - Structured tuple1: (kind='HASH', columns=(id, dt), buckets=10)
    # - Structured tuple2: (kind='RANDOM')
    # - String format: "HASH(id)", "RANDOM", or "(kind='HASH', columns=(id), buckets=10)"
    # Note: Does NOT accept simple columns like id or (id, dt)
    #    And it can't directly accept "HASH(id) BUCKETS 10", you need to split it with "BUCKETS" to two parts.
    DistributedByInputSpec = AnyOf(
        DistributionTupleInputType(),  # Try structured tuple first (most specific)
        EnumType(["RANDOM"], normalized_type="str"),  # "RANDOM"
        FuncType(),  # "HASH(id)",
    )

    # OrderBy: Simple ordering specification
    # Accepts:
    # - Single column: dt
    # - Multiple columns: (dt, id, status)
    OrderByInputSpec = GeneralColumnListInputSpec

    # Refresh scheme: Accepts various types, normalizes to string
    # For properties like refresh_scheme, it can be a string, identifier, or column
    RefreshSchemeInputSpec = AnyOf(
        EnumType(["ASYNC", "MANUAL"], normalized_type="var"),
        ColumnType(normalized_type="str"),  # Columns → will be converted to string
        IdentifierType(normalized_type="str"),  # Identifiers → will be converted to string
        LiteralType(normalized_type="str"),  # Numbers and string → to string
        StringType(),  # Plain strings
    )

    # Generic property value: Accepts various types, normalizes to string
    # For properties like replication_num, storage_medium, etc.
    # StarRocks PROPERTIES syntax requires all values to be strings: "value"
    # So we normalize everything to string for consistent SQL generation
    GenericPropertyInputSpec = AnyOf(
        StringType(),  # Plain strings
        LiteralType(normalized_type="str"),  # Numbers and string → will be converted to string
        IdentifierType(normalized_type="str"),  # Identifiers → will be converted to string
        ColumnType(normalized_type="str"),  # Columns → will be converted to string
    )

    """
    Input Property Specification for StarRocks

    This specification defines the validation and normalization rules for StarRocks properties.
    Properties are specified in the physical_properties block of a SQLMesh model.

    Supported properties:
    - partitioned_by / partition_by: Partition specification
    - partitions: List of partition definitions
    - distributed_by: Distribution specification (HASH/RANDOM with structured tuple or string)
    - order_by: Ordering specification (simple column list)
    - table key:
        - primary_key: Primary key columns
        - duplicate_key: Duplicate key columns (for DUPLICATE KEY table)
        - unique_key: Unique key columns (for UNIQUE KEY table)
        - aggregate_key: Aggregate key columns (for AGGREGATE KEY table)
    - other properties: Any other properties not listed above will be treated as generic
                        string properties (e.g., replication_num, storage_medium, etc.)

    Examples:
        duplicate_key = dt                             # Single key
        primary_key = (id, customer_id)                # Multiple keys

        partitioned_by = col1                          # Single column
        partitioned_by = (col1, col2)                  # Multiple columns
        partitioned_by = (col1, "col2")                # Mixed (string will be parsed)
        partitioned_by = date_trunc('day', col1)       # Expression partition with single func
        partitioned_by = (date_trunc('day', col1), col2)  # Expression partition with multiple exprs
        partitioned_by = RANGE(col1, col2)             # RANGE partition
        partitioned_by = LIST(region, status)          # LIST partition

        distributed_by = (kind='HASH', columns=(id, dt), buckets=10)  # Structured
        distributed_by = (kind='RANDOM')               # RANDOM distribution
        distributed_by = "HASH(id)"                    # String format
        distributed_by = "RANDOM"                      # String format

        order_by = dt                                  # Single column
        order_by = (dt, id, status)                    # Multiple columns

        replication_num = 3                            # Generic property (auto-handled)
        storage_medium = "SSD"                         # Generic property (auto-handled)
    """
    PROPERTY_INPUT_SPECS: t.Dict[str, DeclarativeType] = {
        # Table key properties
        "primary_key": TableKeyInputSpec,
        "duplicate_key": TableKeyInputSpec,
        "unique_key": TableKeyInputSpec,
        "aggregate_key": TableKeyInputSpec,
        # Partition-related properties
        "partitioned_by": PartitionedByInputSpec,
        "partitions": PartitionsInputSpec,
        # Distribution property
        "distributed_by": DistributedByInputSpec,
        # Ordering property
        "clustered_by": OrderByInputSpec,
        # View properties
        # StarRocks syntax: SECURITY {NONE | INVOKER | DEFINER}
        "security": EnumType(["NONE", "INVOKER", "DEFINER"], normalized_type="str"),
        # Materialized view refresh properties (StarRocks uses REFRESH ...)
        # - refresh_moment: IMMEDIATE | DEFERRED
        "refresh_moment": EnumType(["IMMEDIATE", "DEFERRED"], normalized_type="str"),
        # - refresh_scheme: ASYNC | ASYNC [START (...) EVERY (INTERVAL ...)] | MANUAL
        #     it should be a string/literal if START/EVERY is present, other than ASYNC
        "refresh_scheme": RefreshSchemeInputSpec,
        # Note: All other properties not listed here will be handled, an example here
        "replication_num": GenericPropertyInputSpec,
    }

    # Default output spec for properties not in PROPERTY_OUTPUT_SPECS
    GenericPropertyOutputSpec = StringType()

    """
    Output Property Specification for StarRocks after validation+normalization

    This specification describes the expected types after normalization.
    For most properties, OUTPUT spec is the same as INPUT spec since normalization
    preserves the diverse types (dict | str | exp.Func for distribution).

    Conversion to unified formats (e.g., all distributions → dict) happens separately
    in the usage layer via factory methods like DistributionTupleInputType.to_unified_dict().

    Expected Output Types (after normalization):
    - table keys: List[exp.Expression] - columns
    - partitioned_by: List[exp.Expression] - columns, functions
    - partitions: List[str] - partition definition strings
    - distributed_by: Dict | str | exp.Func - DistributionTupleInputType, EnumType, or FuncType output
    - order_by: List[exp.Expression] - columns
    - generic properties: str - normalized string values
    """
    GeneralColumnListOutputSpec: DeclarativeType = SequenceOf(ColumnType(), allow_single=False)

    PROPERTY_OUTPUT_SPECS: t.Dict[str, DeclarativeType] = {
        "primary_key": GeneralColumnListOutputSpec,
        "duplicate_key": GeneralColumnListOutputSpec,
        "unique_key": GeneralColumnListOutputSpec,
        "aggregate_key": GeneralColumnListOutputSpec,
        "partitioned_by": SequenceOf(ColumnType(), FuncType(), allow_single=False),
        "partitions": SequenceOf(StringType(), allow_single=False),
        "distributed_by": AnyOf(
            DistributionTupleOutputType(),  # Try structured tuple first (most specific)
            EnumType(["RANDOM"], normalized_type="str"),  # "RANDOM"
            FuncType(),  # "HASH(id)",
        ),  # Still dict | str | exp.Func after normalize
        "clustered_by": GeneralColumnListOutputSpec,
        "security": EnumType(["NONE", "INVOKER", "DEFINER"], normalized_type="str"),
        "refresh_moment": EnumType(["IMMEDIATE", "DEFERRED"], normalized_type="str"),
        "refresh_scheme": AnyOf(
            EnumType(["ASYNC", "MANUAL"], normalized_type="var"),
            StringType(),
        ),
        # Generic properties use GenericPropertyOutputSpec, an example here
        "replication_num": GenericPropertyOutputSpec,
    }

    # ============================================================
    # Helper functions
    # ============================================================

    @staticmethod
    def get_property_input_spec(property_name: str) -> DeclarativeType:
        """
        Get the INPUT type validator for a property.

        Returns the specific type from PROPERTY_INPUT_SPECS if defined,
        otherwise returns GenericPropertyInputSpec for unknown properties.

        This allows any property not explicitly defined to be treated
        as a generic string property.
        """
        return PropertySpecs.PROPERTY_INPUT_SPECS.get(
            property_name, PropertySpecs.GenericPropertyInputSpec
        )

    @staticmethod
    def get_property_output_spec(property_name: str) -> DeclarativeType:
        """
        Get the OUTPUT type validator for a property.

        Returns the specific type from PROPERTY_OUTPUT_SPECS if defined,
        otherwise returns GenericPropertyOutputSpec for unknown properties.

        This allows validating that normalized values conform to expected output types.
        """
        return PropertySpecs.PROPERTY_OUTPUT_SPECS.get(
            property_name, PropertySpecs.GenericPropertyOutputSpec
        )


# ============================================================
# Property Validation Helpers
# ============================================================
class PropertyValidator:
    """
    Centralized property validation helpers for table properties.

    Provides reusable validation functions to avoid code duplication
    and ensure consistent error messages across different property handlers.
    """

    TABLE_KEY_TYPES = {"primary_key", "duplicate_key", "unique_key", "aggregate_key"}

    # All important properties except generic properties
    IMPORTANT_PROPERTY_NAMES = {
        *TABLE_KEY_TYPES,
        "partitioned_by",
        "partitions",
        "distributed_by",
        "clustered_by",
    }

    # Centralized property alias configuration
    # Maps canonical name -> list of valid aliases
    PROPERTY_ALIASES: t.Dict[str, t.Set[str]] = {
        "partitioned_by": {"partition_by"},
        "clustered_by": {"order_by"},
    }

    EXCLUSIVE_PROPERTY_NAME_MAP: t.Dict[str, t.Set[str]] = {
        "key_type": set(TABLE_KEY_TYPES),
        **PROPERTY_ALIASES,
    }

    # Centralized invalid property name configuration
    # Maps canonical name -> list of invalid/deprecated names
    INVALID_PROPERTY_NAME_MAP: t.Dict[str, t.List[str]] = {
        "partitioned_by": ["partition"],
        "distributed_by": ["distribution", "distribute"],
        "clustered_by": ["order", "ordering"],
    }

    @staticmethod
    def ensure_parenthesized(value: t.Any) -> t.Any:
        """
        Ensure string value is wrapped in parentheses for parse_fragment compatibility.

        For string inputs like 'id1, id2', wraps to '(id1, id2)' so that
        parse_fragment can parse it correctly.

        Args:
            value: Input value (string, expression, or other)

        Returns:
            - For strings/Literal/Column(quoted): wrapped in parentheses if not already
            - For other types: returned unchanged

        Example:
            >>> PropertyValidator.ensure_parenthesized('id1, id2')
            '(id1, id2)'
            >>> PropertyValidator.ensure_parenthesized('(id1, id2)')
            '(id1, id2)'
            >>> PropertyValidator.ensure_parenthesized(exp.Literal.string('id1, id2'))
            '(id1, id2)'
            >>> PropertyValidator.ensure_parenthesized(exp.Column(quoted=True, name='id1, id2'))
            Column(quoted=True, name=id1, id2)
        """
        # logger.debug("ensure_parenthesized. value: %s, type: %s", value, type(value))

        # Extract string content from Literal
        if isinstance(value, exp.Literal) and value.is_string:
            value = value.this
        # Extract string content from Column (quoted)
        elif isinstance(value, exp.Column) and hasattr(value.this, "quoted") and value.this.quoted:
            value = value.name  # Column.name returns the string
        elif not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return value

        # Check if already wrapped in parentheses
        if stripped.startswith("(") and stripped.endswith(")"):
            return value

        return f"({stripped})"

    @staticmethod
    def validate_and_normalize_property(
        property_name: str, value: t.Any, preprocess_parentheses: bool = False
    ) -> t.Any:
        """
        Complete property processing pipeline using SPEC:
        1. Optionally preprocess string with parentheses
        2. Get INPUT type validator
        3. Validate and normalize input value
        4. Get OUTPUT type validator
        5. Verify normalized output conforms to expected type
        6. Return verified output

        After validation, the output type is guaranteed by SPEC.
        Unexpected types indicate SPEC configuration errors.

        Args:
            property_name: Name of the property
            value: The property value to validate
            preprocess_parentheses: If True, wrap string values in parentheses

        Returns:
            The normalized value

        Raises:
            SQLMeshError: If validation fails

        Example:
            >>> validated = PropertyValidator.validate_and_normalize_property("distributed_by", "RANDOM")
            >>> # Result: "RANDOM" (string from EnumType)
        """
        # logger.debug("validate_and_normalize_property. value: %s, type: %s", value, type(value))

        # Step 1: Optionally preprocess string with parentheses
        if preprocess_parentheses:
            value = PropertyValidator.ensure_parenthesized(value)

        # Step 2: Get INPUT type validator
        input_spec = PropertySpecs.get_property_input_spec(property_name)
        if input_spec is None:
            raise SQLMeshError(f"Unknown property '{property_name}'.")

        # Step 3: Validate
        validated = input_spec.validate(value)
        if validated is None:
            raise SQLMeshError(f"Invalid value type for property '{property_name}': {value!r}.")

        # Step 4: Normalize
        normalized = input_spec.normalize(validated)

        # Step 5: Check by using output spec
        output_spec = PropertySpecs.get_property_output_spec(property_name)
        if output_spec is not None:
            if output_spec.validate(normalized) is None:
                raise SQLMeshError(
                    f"Normalized value for property '{property_name}' doesn't match output spec: {normalized!r}."
                )

        # Step 6: Return
        return normalized

    @staticmethod
    def check_invalid_names(
        valid_name: str,
        invalid_names: t.List[str],
        table_properties: t.Dict[str, t.Any],
        suggestion: t.Optional[str] = None,
    ) -> None:
        """
        Check for invalid/deprecated property names and raise error with suggestion.

        Args:
            valid_name: The correct property name
            invalid_names: List of invalid/deprecated names to check for
            table_properties: Table properties dictionary to check
            suggestion: Optional custom error message suggestion

        Raises:
            SQLMeshError: If any invalid name is found

        Example:
            >> PropertyValidator.check_invalid_names(
            ...     valid_name="partitioned_by",
            ...     invalid_names=["partition_by", "partition"],
            ...     table_properties={"partition_by": "dt"}
            ... )
            SQLMeshError: Invalid property 'partition_by'. Use 'partitioned_by' instead.
        """
        for invalid_name in invalid_names:
            if invalid_name in table_properties:
                msg = suggestion or f"Use '{valid_name}' instead"
                raise SQLMeshError(f"Invalid property '{invalid_name}'. {msg}.")

    @classmethod
    def check_all_invalid_names(cls, table_properties: t.Dict[str, t.Any]) -> None:
        """
        Check all invalid property names at once using INVALID_PROPERTY_NAME_MAP config.

        Args:
            table_properties: Table properties dictionary to check

        Raises:
            SQLMeshError: If any invalid name is found
        """
        for valid_name, invalid_names in cls.INVALID_PROPERTY_NAME_MAP.items():
            cls.check_invalid_names(valid_name, invalid_names, table_properties)

    @staticmethod
    def check_at_most_one(
        property_name: str,
        property_description: str,
        table_properties: t.Dict[str, t.Any],
        exclusive_property_names: t.Optional[t.Set[str]] = None,
        parameter_value: t.Optional[t.Any] = None,
    ) -> t.Optional[str]:
        """
        Ensure at most one property from a mutually exclusive group is defined.

        Args:
            property_name: the canonical name
            property_description: description of the property group (for error messages)
            exclusive_property_names: List of mutually exclusive property names.
                Defaults to canonical name and aliases if not provided.
            table_properties: Table properties dictionary to check
            parameter_value: Optional parameter value (takes priority over table_properties)

        Returns:
            Name of the active property, or None if none found
            NOTE: If the parameter value is provided, it returns None

        Raises:
            SQLMeshError: If multiple properties from the group are defined

        Example:
            >> PropertyValidator.check_at_most_one(
            ...     property_name="primary_key",
            ...     property_description="key type",
            ...     exclusive_property_names=["primary_key", "duplicate_key", "unique_key", "aggregate_key"],
            ...     table_properties={"primary_key": "(id)", "duplicate_key": "(id)"}
            ... )
            SQLMeshError: Multiple key type properties defined: ['primary_key', 'duplicate_key'].
                         Only one is allowed.
        """
        if not exclusive_property_names:
            exclusive_property_names = PropertyValidator.EXCLUSIVE_PROPERTY_NAME_MAP.get(
                property_name, set()
            ) | {property_name}
        # logger.debug("Checking at most one property for '%s': %s", property_name, exclusive_property_names)
        # Check parameter first (highest priority)
        if parameter_value is not None:
            # Check if any conflicting properties exist in table_properties
            conflicts = [name for name in exclusive_property_names if name in table_properties]
            if conflicts:
                param_display = f"{property_name} (parameter)"
                raise SQLMeshError(
                    f"Conflicting {property_description} definitions: "
                    f"{param_display} provided along with table_properties {conflicts}. "
                    f"Only one {property_description} is allowed."
                )
            return None

        # Check table_properties for multiple definitions
        present = [name for name in exclusive_property_names if name in table_properties]
        # logger.debug("Get table key names for %s from table_properties: %s", property_name, present)

        if len(present) > 1:
            raise SQLMeshError(
                f"Multiple {property_description} properties defined: {present}. "
                f"Only one is allowed."
            )

        return present[0] if present else None


###############################################################################
# StarRocks Engine Adapter
###############################################################################
@set_catalog()
class StarRocksEngineAdapter(
    LogicalMergeMixin,
    PandasNativeFetchDFSupportMixin,
    ClusteredByMixin,
):
    """
    StarRocks Engine Adapter for SQLMesh.

    StarRocks is a high-performance analytical database with its own dialect-specific
    behavior. This adapter highlights a few key characteristics:

    1. PRIMARY KEY support is native and must be emitted in the post-schema section.
    2. DELETE with subqueries is supported on PRIMARY KEY tables, but other key types still
       need guard rails (no boolean literals, TRUNCATE for WHERE TRUE, etc.).
    3. Partitioning supports RANGE, LIST, and expression-based syntaxes.

    Implementation strategy:
    - Override only where StarRocks syntax/behavior diverges from the base adapter.
    - Keep the rest of the functionality delegated to the shared base implementation.
    """

    # ==================== Class Attributes (Declarative Configuration) ====================

    DIALECT = "starrocks"
    """SQLGlot dialect name for SQL generation"""

    DEFAULT_BATCH_SIZE = 10000
    """Default batch size for bulk operations"""

    SUPPORTS_TRANSACTIONS = False
    """
    StarRocks does not support transactions for multiple DML statements.
    - No BEGIN/COMMIT/ROLLBACK (only txn for multiple INSERT statements from v3.5)
    - Operations are auto-committed
    - Backfill uses partition-level atomicity
    """

    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.DELETE_INSERT
    """
    StarRocks does support INSERT OVERWRITE syntax (and dynamic overwrite from v3.5).
    Use DELETE + INSERT pattern:
    1. DELETE FROM table WHERE condition
    2. INSERT INTO table SELECT ...

    Base class automatically handles this strategy without overriding insert methods.

    TODO: later, we can add support for INSERT OVERWRITE, even use Primary Key for beter performance
    """

    COMMENT_CREATION_TABLE = CommentCreationTable.IN_SCHEMA_DEF_CTAS
    """Table comments are added in both CREATE TABLE statement and CTAS"""

    COMMENT_CREATION_VIEW = CommentCreationView.IN_SCHEMA_DEF_NO_COMMANDS
    """View comments are added in CREATE VIEW statement"""

    SUPPORTS_MATERIALIZED_VIEWS = True
    """StarRocks supports materialized views with refresh strategies"""

    SUPPORTS_MATERIALIZED_VIEW_SCHEMA = True
    """
    StarRocks materialized views support specifying a column list, but the column definition is
    limited (e.g. column name + comment, not full type definitions). We set this to True and
    implement custom MV schema rendering in create_view/_create_materialized_view.
    """

    SUPPORTS_REPLACE_TABLE = False
    """No REPLACE TABLE syntax; use DROP + CREATE instead"""

    SUPPORTS_CREATE_DROP_CATALOG = False
    """StarRocks supports DROPing external catalogs.
    TODO: whether it's external catalogs, or includes the internal catalog
    """

    SUPPORTS_INDEXES = True
    """
    StarRocks supports PRIMARY KEY in CREATE TABLE, but NOT standalone CREATE INDEX.

    We set this to True to enable PRIMARY KEY generation in CREATE TABLE statements.
    The create_index() method is overridden to prevent actual CREATE INDEX execution.

    Supported (defined in CREATE TABLE):
    - PRIMARY KEY: Automatically creates sorted index
    - INDEX clause: For bloom filter, bitmap, inverted indexes
    NOT supported:
        CREATE INDEX idx_name ON t (name);  -- Will be skipped by create_index()
    """

    SUPPORTS_TUPLE_IN = False
    """
    StarRocks does NOT support tuple IN syntax: (col1, col2) IN ((val1, val2), (val3, val4))

    Instead, use OR with AND conditions:
    (col1 = val1 AND col2 = val2) OR (col1 = val3 AND col2 = val4)

    This is automatically handled by snapshot_id_filter and snapshot_name_version_filter
    in sqlmesh/core/state_sync/db/utils.py when SUPPORTS_TUPLE_IN = False.
    """

    MAX_TABLE_COMMENT_LENGTH = 2048
    """Maximum length for table comments"""

    MAX_COLUMN_COMMENT_LENGTH = 255
    """Maximum length for column comments"""

    MAX_IDENTIFIER_LENGTH = 64
    """Maximum length for table/column names"""

    # ==================== Schema Operations ====================
    # StarRocks supports CREATE/DROP SCHEMA the same as CREATE/DROP DATABSE.
    # So, no need to implement create_schema / drop_schema

    # ==================== Data Object Query ====================
    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema.
        Uses information_schema tables which are compatible with MySQL protocol.

        StarRocks uses the MySQL-compatible information_schema layout, so the same query
        works here.
        Note: Materialized View is not reliably distinguished from View (both may appear as `VIEW`)
        in information_schema.tables. We therefore best-effort detect MVs via
        information_schema.materialized_views and upgrade matching objects to `materialized_view`.

        Args:
            schema_name: The schema (database) to query
            object_names: Optional set of specific table names to filter

        Returns:
            List of DataObject instances representing tables and views
        """
        schema_db = to_schema(schema_name).db
        query = (
            exp.select(
                exp.column("table_schema").as_("schema_name"),
                exp.column("table_name").as_("name"),
                exp.case(exp.column("table_type"))
                .when(
                    exp.Literal.string("BASE TABLE"),
                    exp.Literal.string("table"),
                )
                .when(
                    exp.Literal.string("VIEW"),
                    exp.Literal.string("view"),
                )
                .else_("table_type")
                .as_("type"),
            )
            .from_(exp.table_("tables", db="information_schema"))
            .where(exp.column("table_schema").eq(schema_db))
        )
        if object_names:
            # StarRocks may treat information_schema table_name comparisons as case-sensitive.
            # Use LOWER(table_name) to match case-insensitively.
            lowered_names = [name.lower() for name in object_names]
            query = query.where(exp.func("LOWER", exp.column("table_name")).isin(*lowered_names))

        df = self.fetchdf(query)
        objects = [
            DataObject(
                schema=row.schema_name,
                name=row.name,
                type=DataObjectType.from_str(str(row.type)),
            )
            for row in df.itertuples()
        ]

        # Best-effort upgrade of MV types using information_schema.materialized_views.
        # If this fails (unsupported / permissions / version), fall back to information_schema.tables.
        try:
            mv_query = (
                exp.select(
                    exp.column("table_schema").as_("schema_name"),
                    exp.column("table_name").as_("name"),
                )
                .from_(exp.table_("materialized_views", db="information_schema"))
                .where(exp.column("table_schema").eq(schema_db))
            )
            if object_names:
                lowered_names = [name.lower() for name in object_names]
                mv_query = mv_query.where(
                    exp.func("LOWER", exp.column("table_name")).isin(*lowered_names)
                )

            mv_df = self.fetchdf(mv_query)
            mv_names: t.Set[str] = {
                t.cast(str, r.name).lower() for r in mv_df.itertuples() if r.name
            }

            if mv_names:
                for obj in objects:
                    if obj.name.lower() in mv_names:
                        obj.type = DataObjectType.MATERIALIZED_VIEW
        except Exception:
            logger.warning(
                f"[StarRocks] Failed to get materialized views from information_schema.materialized_views"
            )

        return objects

    def create_index(
        self,
        table_name: TableName,
        index_name: str,
        columns: t.Tuple[str, ...],
        exists: bool = True,
    ) -> None:
        """
        Override to prevent CREATE INDEX statements (not supported in StarRocks).

        StarRocks does not support standalone CREATE INDEX statements.
        Indexes must be defined during CREATE TABLE using INDEX clause.

        Since SQLMesh state tables use PRIMARY KEY (which provides efficient indexing),
        we simply log and skip additional index creation requests.

        This matches upstream StarRocks limitations and prevents accidental CREATE INDEX calls.
        """
        logger.warning(
            f"[StarRocks] Skipping CREATE INDEX {index_name} on {table_name} - "
            f"StarRocks does not support standalone CREATE INDEX statements. "
            f"PRIMARY KEY provides equivalent indexing for columns: {columns}"
        )
        return

    def _create_table_like(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        exists: bool,
        **kwargs: t.Any,
    ) -> None:
        """Create a new table using StarRocks' native `CREATE TABLE ... LIKE ...` syntax.

        The base implementation re-creates the target table from `columns(source)` which can
        lose non-column metadata. Using LIKE lets the engine preserve more of the original
        table definition (engine-defined behavior).
        """
        self.execute(
            exp.Create(
                this=exp.to_table(target_table_name),
                kind="TABLE",
                exists=exists,
                properties=exp.Properties(
                    expressions=[
                        exp.LikeProperty(
                            this=exp.to_table(source_table_name),
                        ),
                    ],
                ),
            )
        )

    def delete_from(
        self,
        table_name: TableName,
        where: t.Optional[t.Union[str, exp.Expression]] = None,
    ) -> None:
        """
        Delete from a table.

        StarRocks DELETE limitations by table type:

        PRIMARY KEY tables:
        - Support complex WHERE conditions (subqueries, BETWEEN, etc.)
        - No special handling needed

        Other table types (DUPLICATE/UNIQUE/AGGREGATE KEY):
        - WHERE TRUE not supported → use TRUNCATE TABLE
        - Boolean literals (TRUE/FALSE) not supported
        - BETWEEN not supported → convert to >= AND <=
        - Others not supported:
            - CAST() not supported in WHERE
            - Subqueries not supported
            - ...

        But, I don't know what the table type is.

        Args:
            table_name: The table to delete from
            where: The where clause to filter rows to delete
        """
        # Parse where clause if it's a string
        where_expr: t.Optional[exp.Expression]
        if isinstance(where, str):
            from sqlglot import parse_one

            where_expr = parse_one(where, dialect=self.dialect)
        else:
            where_expr = where

        # If no where clause or WHERE TRUE, use TRUNCATE TABLE (for all table types)
        if not where_expr or where_expr == exp.true():
            table_expr = exp.to_table(table_name) if isinstance(table_name, str) else table_name
            logger.info(
                f"Converting DELETE FROM {table_name} WHERE TRUE to TRUNCATE TABLE "
                "(StarRocks does not support WHERE TRUE in DELETE)"
            )
            self.execute(f"TRUNCATE TABLE {table_expr.sql(dialect=self.dialect, identify=True)}")
            return

        # For non-PRIMARY KEY tables, apply WHERE clause restrictions
        # Note: We conservatively apply restrictions to all tables since we can't easily
        # determine table type at DELETE time. PRIMARY KEY tables will still work with
        # simplified conditions, while non-PRIMARY KEY tables require them.
        if isinstance(where_expr, exp.Expression):
            original_where = where_expr
            # Remove boolean literals (not supported in any table type)
            where_expr = self._where_clause_remove_boolean_literals(where_expr)
            # Convert BETWEEN to >= AND <= (required for DUPLICATE/UNIQUE/AGGREGATE KEY tables)
            where_expr = self._where_clause_convert_between_to_comparison(where_expr)

            if where_expr != original_where:
                logger.debug(
                    f"Converted WHERE clause for StarRocks compatibility, table: {table_name}.\n"
                    f"  Original: {original_where.sql(dialect=self.dialect)}\n"
                    f"  Converted: {where_expr.sql(dialect=self.dialect)}"
                )

        # Use parent implementation
        super().delete_from(table_name, where_expr)

    def _where_clause_remove_boolean_literals(self, expression: exp.Expression) -> exp.Expression:
        """
        Remove TRUE/FALSE boolean literals from WHERE expressions.

        StarRocks Limitation (except PRIMARY KEY tables):
        Boolean literals (TRUE/FALSE) are not supported in WHERE clauses.

        This method simplifies expressions:
        - (condition) AND TRUE / TRUE AND (condition) → condition
        - (condition) OR FALSE / FALSE OR (condition) → condition
        - WHERE TRUE → 1=1 (though TRUNCATE is used instead)
        - WHERE FALSE → 1=0

        Args:
            expression: The expression to clean

        Returns:
            Cleaned expression without boolean literals
        """

        def transform(node: exp.Expression) -> exp.Expression:
            # Handle standalone TRUE/FALSE at the top level
            if node == exp.true():
                # Convert TRUE to 1=1
                return exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(1))
            elif node == exp.false():  # noqa: RET505
                # Convert FALSE to 1=0
                return exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(0))

            # Handle AND expressions
            elif isinstance(node, exp.And):
                left = node.this
                right = node.expression

                # Remove TRUE from AND
                if left == exp.true():
                    return right
                if right == exp.true():
                    return left

            # Handle OR expressions
            elif isinstance(node, exp.Or):
                left = node.this
                right = node.expression

                # Remove FALSE from OR
                if left == exp.false():
                    return right
                if right == exp.false():
                    return left

            return node

        # Transform the expression tree
        return expression.transform(transform, copy=True)

    def _where_clause_convert_between_to_comparison(
        self, expression: exp.Expression
    ) -> exp.Expression:
        """
        Convert BETWEEN expressions to >= AND <= comparisons.

        StarRocks Limitation (DUPLICATE/UNIQUE/AGGREGATE KEY Tables):
        BETWEEN is not supported in DELETE WHERE clauses for non-PRIMARY KEY tables.

        PRIMARY KEY tables support BETWEEN, but this conversion is safe for all table types
        since the converted form (>= AND <=) is semantically equivalent.

        This method converts:
        - col BETWEEN a AND b  →  col >= a AND col <= b

        Args:
            expression: The expression potentially containing BETWEEN

        Returns:
            Expression with BETWEEN converted to comparisons
        """

        def transform(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Between):
                # Extract components: col BETWEEN low AND high
                column = node.this  # The column being tested
                low = node.args.get("low")  # Lower bound
                high = node.args.get("high")  # Upper bound

                if column and low and high:
                    # Build: column >= low AND column <= high
                    gte = exp.GTE(this=column.copy(), expression=low.copy())
                    lte = exp.LTE(this=column.copy(), expression=high.copy())
                    return exp.And(this=gte, expression=lte)

            return node

        # Transform the expression tree
        return expression.transform(transform, copy=True)

    def execute(
        self,
        expressions: t.Union[str, exp.Expression, t.Sequence[exp.Expression]],
        ignore_unsupported_errors: bool = False,
        quote_identifiers: bool = True,
        track_rows_processed: bool = False,
        **kwargs: t.Any,
    ) -> None:
        """
        Override execute to strip FOR UPDATE from queries (not supported in StarRocks).

        StarRocks is an OLAP database and does not support row-level locking via
        SELECT ... FOR UPDATE. This method removes lock expressions before execution.

        Args:
            expressions: SQL expression(s) to execute
            ignore_unsupported_errors: Whether to ignore unsupported errors
            quote_identifiers: Whether to quote identifiers
            track_rows_processed: Whether to track rows processed
            **kwargs: Additional arguments
        """
        from sqlglot.helper import ensure_list

        if isinstance(expressions, str):
            super().execute(
                expressions,
                ignore_unsupported_errors=ignore_unsupported_errors,
                quote_identifiers=quote_identifiers,
                track_rows_processed=track_rows_processed,
                **kwargs,
            )
            return

        # Process expressions to remove FOR UPDATE
        processed_expressions: t.List[exp.Expression] = []
        for e in ensure_list(expressions):
            if not isinstance(e, exp.Expression):
                super().execute(
                    expressions,
                    ignore_unsupported_errors=ignore_unsupported_errors,
                    quote_identifiers=quote_identifiers,
                    track_rows_processed=track_rows_processed,
                    **kwargs,
                )
                return

            # Remove lock (FOR UPDATE) from SELECT statements
            if isinstance(e, exp.Select) and e.args.get("locks"):
                e = e.copy()
                e.set("locks", None)
                logger.warning(
                    f"[StarRocks] Removed FOR UPDATE from SELECT statement: "
                    f"{e.sql(dialect=self.dialect, identify=quote_identifiers)}"
                )
            processed_expressions.append(e)

        # Call parent execute with processed expressions
        super().execute(
            processed_expressions,
            ignore_unsupported_errors=ignore_unsupported_errors,
            quote_identifiers=quote_identifiers,
            track_rows_processed=track_rows_processed,
            **kwargs,
        )

    # ==================== Table Creation (CORE IMPLEMENTATION) ====================
    def _create_table_from_columns(
        self,
        table_name: TableName,
        target_columns_to_types: t.Dict[str, exp.DataType],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
        exists: bool = True,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        **kwargs: t.Any,
    ) -> None:
        """
        Create a table using column definitions.

        Unified Model Parameter vs Physical Properties Handling:
        For properties that can be defined both as model parameters and in physical_properties,
        this method implements a unified priority strategy:
        1. Model parameter takes priority if present
        2. Otherwise, use value from physical_properties
        3. Ensure at most one definition exists

        Supported unified properties:
        - primary_key: Model parameter OR physical_properties.primary_key
        - partitioned_by: Model parameter OR physical_properties.partitioned_by/partition_by
        - clustered_by: Model parameter OR physical_properties.clustered_by/order_by

        Other key types (duplicate_key, aggregate_key, unique_key) only support physical_properties.

        StarRocks Key Column Ordering Constraint:
        ALL key types (PRIMARY KEY, UNIQUE KEY, DUPLICATE KEY, AGGREGATE KEY) require:
        - Key columns MUST be the first N columns in CREATE TABLE
        - Column order MUST match the KEY clause order

        Implementation Strategy:
        1. Normalize model parameters into table_properties with priority handling
        2. Extract and validate key columns from unified table_properties
        3. Validate no conflicts between different key types
        4. Reorder columns to place key columns first
        5. For PRIMARY KEY: Pass to base class (sets SUPPORTS_INDEXES=True)
        6. For other keys: Handle in _build_table_key_property

        Args:
            table_name: Fully qualified table name
            target_columns_to_types: Column definitions {name: DataType}
            primary_key: Primary key column names (model parameter, takes priority)
            exists: Add IF NOT EXISTS clause
            table_description: Table comment
            column_descriptions: Column comments {column_name: comment}
            kwargs: Additional properties including:
                - partitioned_by: Partition columns (model parameter)
                - clustered_by: Clustering columns (model parameter)
                - table_properties: Physical properties dict

        Example:
            # Model parameter (priority):
            partitioned_by=dt,
            clustered_by=(dt, id))
            physical_properties(
                primary_key=(id, dt)
            )

            # Or physical_properties only:
            physical_properties(
                duplicate_key=(id, dt),
                partitioned_by=dt,
                order_by=(dt, id)
            )
        """
        # Use setdefault to simplify table_properties access
        table_properties = kwargs.setdefault("table_properties", {})

        # Extract and validate key columns from table_properties
        # Priority: parameter primary_key > table_properties (already handled above)
        key_type, key_columns = self._extract_and_validate_key_columns(
            table_properties, primary_key
        )
        # logger.debug(
        #     "_create_table_from_columns: extracted key_type=%s, key_columns=%s",
        #     key_type,
        #     key_columns,
        # )

        # IMPORTANT: Normalize parameter primary_key into table_properties for unified handling
        # This ensures _build_table_properties_exp() can access primary_key even when
        # it's passed as a model parameter rather than in physical_properties
        if primary_key:
            table_properties["primary_key"] = primary_key
            logger.debug("_create_table_from_columns: unified primary_key into table_properties")
        elif key_type:
            # logger.debug(
            #     "table key type '%s' may be handled in _build_table_key_property", key_type
            # )
            pass

        # StarRocks key column ordering constraint: All key types need reordering
        if key_columns:
            target_columns_to_types = self._reorder_columns_for_key(
                target_columns_to_types, key_columns, key_type or "key"
            )

        # IMPORTANT: Do NOT pass primary_key to base class!
        # Unlike other databases, StarRocks requires PRIMARY KEY to be in POST_SCHEMA location
        # (in properties section after columns), not inside schema (inside column definitions).
        # We handle ALL key types (including PRIMARY KEY) in _build_table_key_property.
        # logger.debug(
        #     "_create_table_from_columns: NOT passing primary_key to base class (handled in _build_table_key_property)"
        # )
        super()._create_table_from_columns(
            table_name=table_name,
            target_columns_to_types=target_columns_to_types,
            primary_key=None,  # StarRocks handles PRIMARY KEY in properties, not schema
            exists=exists,
            table_description=table_description,
            column_descriptions=column_descriptions,
            **kwargs,
        )

    # ==================== View / Materialized View ====================
    def create_view(
        self,
        view_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        replace: bool = True,
        materialized: bool = False,
        materialized_properties: t.Optional[t.Dict[str, t.Any]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        view_properties: t.Optional[t.Dict[str, exp.Expression]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **create_kwargs: t.Any,
    ) -> None:
        """
        StarRocks behavior:
        - Regular VIEW: supports CREATE OR REPLACE (base behavior)
        - MATERIALIZED VIEW: does NOT support CREATE OR REPLACE, so replace=True => DROP + CREATE
        """
        if not materialized:
            return super().create_view(
                view_name=view_name,
                query_or_df=query_or_df,
                target_columns_to_types=target_columns_to_types,
                replace=replace,
                materialized=False,
                materialized_properties=materialized_properties,
                table_description=table_description,
                column_descriptions=column_descriptions,
                view_properties=view_properties,
                source_columns=source_columns,
                **create_kwargs,
            )

        # MATERIALIZED VIEW path
        if replace:
            # Avoid DROP MATERIALIZED VIEW failure when an object with the same name exists but is not an MV.
            self.drop_data_object_on_type_mismatch(
                self.get_data_object(view_name), DataObjectType.MATERIALIZED_VIEW
            )
            self.drop_view(view_name, ignore_if_not_exists=True, materialized=True)
        # logger.debug(
        #     f"Creating materialized view: {view_name}, materialized: {materialized}, "
        #     f"materialized_properties: {materialized_properties}, "
        #     f"view_properties: {view_properties}, create_kwargs: {create_kwargs}, "
        # )

        return self._create_materialized_view(
            view_name=view_name,
            query_or_df=query_or_df,
            target_columns_to_types=target_columns_to_types,
            materialized_properties=materialized_properties,
            table_description=table_description,
            column_descriptions=column_descriptions,
            view_properties=view_properties,
            source_columns=source_columns,
            **create_kwargs,
        )

    def _create_materialized_view(
        self,
        view_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        materialized_properties: t.Optional[t.Dict[str, t.Any]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        view_properties: t.Optional[t.Dict[str, exp.Expression]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **create_kwargs: t.Any,
    ) -> None:
        """
        Create a StarRocks materialized view.

        StarRocks MV schema supports a column list but does NOT support explicit data types in that list.
        We therefore build a schema with column names + optional COMMENT only.
        """
        import pandas as pd

        query_or_df = self._native_df_to_pandas_df(query_or_df)

        if isinstance(query_or_df, pd.DataFrame):
            values: t.List[t.Tuple[t.Any, ...]] = list(
                query_or_df.itertuples(index=False, name=None)
            )
            target_columns_to_types, source_columns = self._columns_to_types(
                query_or_df, target_columns_to_types, source_columns
            )
            if not target_columns_to_types:
                raise SQLMeshError("columns_to_types must be provided for dataframes")
            source_columns_to_types = get_source_columns_to_types(
                target_columns_to_types, source_columns
            )
            query_or_df = self._values_to_sql(
                values,
                source_columns_to_types,
                batch_start=0,
                batch_end=len(values),
            )

        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types,
            batch_size=0,
            target_table=view_name,
            source_columns=source_columns,
        )
        if len(source_queries) != 1:
            raise SQLMeshError("Only one source query is supported for creating materialized views")

        target_table = exp.to_table(view_name)
        schema: t.Union[exp.Table, exp.Schema] = self._build_materialized_view_schema_exp(
            target_table,
            target_columns_to_types=target_columns_to_types,
            column_descriptions=column_descriptions,
        )

        # Pass model materialized properties through the existing properties builder
        partitioned_by = None
        clustered_by = None
        partition_interval_unit = None
        if materialized_properties:
            partitioned_by = materialized_properties.get("partitioned_by")
            clustered_by = materialized_properties.get("clustered_by")
            partition_interval_unit = materialized_properties.get("partition_interval_unit")
            # logger.debug(
            #     f"Get info from materialized_properties: {materialized_properties}, "
            #     f"partitioned_by: {partitioned_by}, "
            #     f"clustered_by: {clustered_by}, "
            #     f"partition_interval_unit: {partition_interval_unit}"
            # )

        properties_exp = self._build_table_properties_exp(
            catalog_name=target_table.catalog,
            table_properties=view_properties,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            partitioned_by=partitioned_by,
            clustered_by=clustered_by,
            partition_interval_unit=partition_interval_unit,
            table_kind="MATERIALIZED_VIEW",
        )

        with source_queries[0] as query:
            self.execute(
                exp.Create(
                    this=schema,
                    kind="VIEW",
                    replace=False,
                    expression=query,
                    properties=properties_exp,
                    **create_kwargs,
                ),
                quote_identifiers=self.QUOTE_IDENTIFIERS_IN_VIEWS,
            )

        self._clear_data_object_cache(view_name)

    def _build_materialized_view_schema_exp(
        self,
        table: exp.Table,
        *,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
    ) -> t.Union[exp.Table, exp.Schema]:
        """
        Build a StarRocks MV schema with column names + optional COMMENT only (no types).
        """
        columns: t.List[str] = []
        if target_columns_to_types:
            columns = list(target_columns_to_types)
        elif column_descriptions:
            columns = list(column_descriptions)

        if not columns:
            return table

        column_descriptions = column_descriptions or {}
        expressions: t.List[exp.Expression] = []
        for col in columns:
            constraints: t.List[exp.ColumnConstraint] = []
            comment = column_descriptions.get(col)
            if comment:
                constraints.append(
                    exp.ColumnConstraint(
                        kind=exp.CommentColumnConstraint(
                            this=exp.Literal.string(self._truncate_column_comment(comment))
                        )
                    )
                )
            expressions.append(
                exp.ColumnDef(
                    this=exp.to_identifier(col),
                    constraints=constraints,
                )
            )

        return exp.Schema(this=table, expressions=expressions)

    # ==================== Table Properties Builder (for Table and MV/VIew) ====================
    def _build_table_properties_exp(
        self,
        catalog_name: t.Optional[str] = None,
        table_format: t.Optional[str] = None,
        storage_format: t.Optional[str] = None,
        partitioned_by: t.Optional[t.List[exp.Expression]] = None,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        clustered_by: t.Optional[t.List[exp.Expression]] = None,
        table_properties: t.Optional[t.Dict[str, exp.Expression]] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        """
        Build table properties for StarRocks CREATE TABLE statement.

        Unified Model Parameter vs Physical Properties Handling:
        This method receives both model parameters (partitioned_by, clustered_by) and
        physical_properties (table_properties dict). Priority is handled as follows:

        1. primary_key / partitioned_by / clustered_by (ORDER BY)
           - Model parameter takes priority
           - Falls back to physical_properties.xxx
           - Handled in _build_partition_property

        2. special for primary_key:
           - Still need to be processed in _build_table_key_property

        3. Other key types (duplicate_key, unique_key, aggregate_key):
           - Only available via physical_properties
           - Handled in _build_table_key_property

        Handles:
        - Key constraints (PRIMARY KEY, DUPLICATE KEY, UNIQUE KEY)
        - Partition expressions (RANGE/LIST/EXPRESSION)
        - Distribution (HASH/RANDOM)
        - Order by (clustering)
        - Table comment
        - Other properties (replication_num, storage_medium, etc.)

        Args:
            partitioned_by: Partition columns/expression from model parameter (takes priority)
            clustered_by: Clustering columns from model parameter (takes priority)
            table_properties: Dictionary containing physical_properties:
                - primary_key/duplicate_key/unique_key/aggregate_key: Tuple/list of column names
                - partitioned_by(partition_by): Partition definition (fallback)
                - distributed_by: Tuple of EQ expressions (kind, expressions, buckets) or string
                - clustered_by(order_by): Clustering definition (fallback)
                - replication_num, storage_medium, etc.: Literal values
            table_description: Table comment
        """
        properties: t.List[exp.Expression] = []
        table_properties_copy = dict(table_properties) if table_properties else {}
        # logger.debug(
        #     "_build_table_properties_exp: table_properties=%s",
        #     table_properties.keys() if table_properties else [],
        # )

        is_mv = table_kind == "MATERIALIZED_VIEW"
        if is_mv:
            # Required for CREATE MATERIALIZED VIEW (SQLGlot uses this property to switch the keyword)
            properties.append(exp.MaterializedProperty())

        # Validate all property names at once
        PropertyValidator.check_all_invalid_names(table_properties_copy)

        # Check for mutually exclusive key types
        # Note: primary_key is already set into table_properties if model param is set
        active_key_type = PropertyValidator.check_at_most_one(
            property_name="key_type",
            property_description="key type",
            table_properties=table_properties_copy,
        )
        if is_mv and active_key_type:
            raise SQLMeshError(
                f"You can't specify the table type when the table is a materialized view. "
                f"Current specified key type '{active_key_type}'."
            )

        # 0. Extract key columns for partition/distribution validation (read-only, don't pop yet)
        key_type, key_columns = None, None
        if active_key_type:
            key_type = active_key_type
            key_expr = table_properties_copy[key_type]
            # Use validate_and_normalize_property to get List[exp.Column], then extract names
            normalized = PropertyValidator.validate_and_normalize_property(
                key_type, key_expr, preprocess_parentheses=True
            )
            key_columns = tuple(col.name for col in normalized)

        # 1. Handle key constraints (ALL types including PRIMARY KEY)
        key_prop = self._build_table_key_property(table_properties_copy, active_key_type)
        if key_prop:
            properties.append(key_prop)

        # 2. Add table comment (it must be ahead of other properties except the talbe key/type)
        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        # 3. Handle partitioned_by (PARTITION BY RANGE/LIST/EXPRESSION)
        partition_prop = self._build_partition_property(
            partitioned_by,
            partition_interval_unit,
            target_columns_to_types,
            catalog_name,
            table_properties_copy,
            key_type,
            key_columns,
        )
        if partition_prop:
            properties.append(partition_prop)

        # 4. Handle distributed_by (DISTRIBUTED BY HASH/RANDOM)
        distributed_prop = self._build_distributed_by_property(table_properties_copy, key_columns)
        if distributed_prop:
            properties.append(distributed_prop)

        # 5. Handle refresh_property (REFRESH ...)
        if is_mv:
            refresh_prop = self._build_refresh_property(table_properties_copy)
            if refresh_prop:
                properties.append(refresh_prop)

        # 6. Handle order_by/clustered_by (ORDER BY ...)
        order_prop = self._build_order_by_property(table_properties_copy, clustered_by or None)
        if order_prop:
            properties.append(order_prop)

        # 5. Handle other properties (replication_num, storage_medium, etc.)
        other_props = self._build_other_properties(table_properties_copy)
        properties.extend(other_props)

        return exp.Properties(expressions=properties) if properties else None

    def _build_view_properties_exp(
        self,
        view_properties: t.Optional[t.Dict[str, exp.Expression]] = None,
        table_description: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        """
        Build CREATE VIEW properties for StarRocks.

        Supports StarRocks view SECURITY syntax: SECURITY {NONE | INVOKER}
        via exp.SecurityProperty (renders as `SECURITY <value>`).
        """
        properties: t.List[exp.Expression] = []

        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        if view_properties:
            view_properties_copy = dict(view_properties)
            security = view_properties_copy.pop("security", None)
            if security is not None:
                security_text = PropertyValidator.validate_and_normalize_property(
                    "security", security
                )
                # exp.SecurityProperty renders as `SECURITY <value>` (no '=')
                properties.append(exp.SecurityProperty(this=exp.Var(this=security_text)))

            properties.extend(self._table_or_view_properties_to_expressions(view_properties_copy))

        if properties:
            return exp.Properties(expressions=properties)
        return None

    def _build_table_key_property(
        self, table_properties: t.Dict[str, t.Any], active_key_type: t.Optional[str]
    ) -> t.Optional[exp.Expression]:
        """
        Build key constraint property for ALL key types including PRIMARY KEY.

        Unlike other databases where PRIMARY KEY is handled by base class in schema,
        StarRocks requires ALL key types (PRIMARY KEY, DUPLICATE KEY, UNIQUE KEY, AGGREGATE KEY)
        to be in POST_SCHEMA location (properties section after columns).

        Handles:
        - PRIMARY KEY
        - DUPLICATE KEY
        - UNIQUE KEY
        - AGGREGATE KEY (when implemented)

        Args:
            table_properties: Dictionary containing key definitions (will be modified)
            active_key_type: The active key type or None

        Returns:
            Key property expression for the active key type, or None
        """
        if not active_key_type:
            return None

        # Configuration: key_name -> Property class (excluding primary_key)
        KEY_PROPERTY_CLASSES: t.Dict[str, t.Type[exp.Expression]] = {
            "primary_key": exp.PrimaryKey,
            "duplicate_key": exp.DuplicateKeyProperty,
            "unique_key": exp.UniqueKeyProperty,
            # "aggregate_key": exp.AggregateKeyProperty,  # Not implemented yet
        }

        property_class = KEY_PROPERTY_CLASSES.get(active_key_type)
        key_value = table_properties.pop(active_key_type, None)
        if not property_class:
            # Aggregate key requires special handling
            if active_key_type == "aggregate_key":
                raise SQLMeshError(
                    "AGGREGATE KEY tables are not currently supported. "
                    "AGGREGATE KEY requires specifying aggregation functions (SUM/MAX/MIN/REPLACE) "
                    "for value columns, which is not supported in the current model configuration syntax. "
                    "Please use PRIMARY KEY, UNIQUE KEY, or DUPLICATE KEY instead."
                )
            # Unknown key type
            logger.warning(f"[StarRocks] Unknown key type: {active_key_type}")
            return None
        if key_value is None:
            logger.error(f"Failed to get the parameter value for {active_key_type!r}")
            return None

        logger.debug(
            "_build_table_key_property: input key=%s value=%s",
            active_key_type,
            key_value,
        )

        # Validate and normalize
        # preprocess_parentheses=True handles string preprocessing like 'id, dt' -> '(id, dt)'
        normalized = PropertyValidator.validate_and_normalize_property(
            active_key_type, key_value, preprocess_parentheses=True
        )
        # normalized is List[exp.Column] as defined in TableKeyInputSpec
        result = property_class(expressions=list(normalized))
        return result

    def _build_partition_property(
        self,
        partitioned_by: t.Optional[t.List[exp.Expression]],
        partition_interval_unit: t.Optional["IntervalUnit"],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]],
        catalog_name: t.Optional[str],
        table_properties: t.Dict[str, t.Any],
        key_type: t.Optional[str],
        key_columns: t.Optional[t.Tuple[str, ...]],
    ) -> t.Optional[exp.Expression]:
        """
        Build partition property expression.

        StarRocks supports:
        - PARTITION BY RANGE (cols) - for time-based partitions
        - PARTITION BY LIST (cols) - for categorical partitions
        - PARTITION BY (exprs) - for expression partitions, can also be `exprs` (without `(`, and `)`)

        Args:
            partitioned_by: Partition column expressions from parameter
            partition_interval_unit: Optional time unit for automatic partitioning
            target_columns_to_types: Column definitions
            catalog_name: Catalog name (if applicable)
            table_properties: Dictionary containing partitioned_by/partitions (will be modified)
            key_type: Table key type (for validation)
            key_columns: Table key columns (partition columns must be subset)

        Returns:
            Partition property expression or None
        """
        # Priority: parameter > partition_by (alias) > partitioned_by
        # Use PropertyValidator to check mutual exclusion between parameter and properties
        partition_param_name = PropertyValidator.check_at_most_one(
            property_name="partitioned_by",
            property_description="partition definition",
            table_properties=table_properties,
            parameter_value=partitioned_by or None,
        )

        # If parameter was provided, it takes priority
        if not partitioned_by and partition_param_name:
            # Get from table_properties
            partitioned_by = table_properties.pop(partition_param_name, None)
        if not partitioned_by:
            return None

        # Parse partition expressions to extract columns and kind (RANGE/LIST)
        partition_kind, partition_cols = self._parse_partition_expressions(partitioned_by)
        logger.debug(
            "_build_partition_property: partition_kind=%s, partition_cols=%s",
            partition_kind,
            partition_cols,
        )

        def extract_column_name(expr: exp.Expression) -> t.Optional[str]:
            if isinstance(expr, exp.Column):
                return str(expr.name)
            elif isinstance(expr, (exp.Anonymous, exp.Func)):  # noqa: RET505
                return None  # not implemented
            else:
                return str(expr)

        # Validate partition columns are in key columns (StarRocks requirement)
        if key_columns:
            partition_col_names = set(extract_column_name(expr) for expr in partition_cols) - {None}
            key_cols_set = set(key_columns)
            not_in_key = partition_col_names - key_cols_set
            if not_in_key:
                logger.warning(
                    f"[StarRocks] Partition columns {not_in_key} not in {key_type} columns {key_cols_set}. "
                    "StarRocks requires partition columns to be part of the table key."
                )

        # Get partition definitions (RANGE/LIST partitions)
        # Note: Expression-based partitioning (partition_kind=None) does not support pre-created partitions
        if partitions := table_properties.pop("partitions", None):
            if partition_kind is None:
                logger.warning(
                    "[StarRocks] 'partitions' parameter is ignored for expression-based partitioning. "
                    "Expression partitioning creates partitions automatically and does not support "
                    "pre-created partition definitions."
                )
                partitions = None  # Ignore partitions for expression-based partitioning
            else:
                partitions = PropertyValidator.validate_and_normalize_property(
                    "partitions", partitions
                )

        # Build partition expression using base class method
        result = self._build_partitioned_by_exp(
            partition_cols,
            partition_interval_unit=partition_interval_unit,
            target_columns_to_types=target_columns_to_types,
            catalog_name=catalog_name,
            partitions=partitions,
            partition_kind=partition_kind,
        )
        return result

    def _parse_partition_expressions(
        self, partitioned_by: t.List[exp.Expression]
    ) -> t.Tuple[t.Optional[str], t.List[exp.Expression]]:
        """
        Parse partition expressions and extract partition kind (RANGE/LIST).

        Uses PartitionedByInputSpec to validate and normalize the entire list,
        then extracts RANGE/LIST kind from function expressions.

        The SPEC output is List[exp.Column | exp.Anonymous | exp.Func], where:
        - exp.Column: Regular column reference
        - exp.Anonymous: Function call like RANGE(col), LIST(col), and other datetime related functions
        - exp.Func: date_trunc(), and other built-in functions

        Args:
            partitioned_by: List of partition expressions

        Returns:
            Tuple of (partition_kind, normalized_columns)
            - partition_kind: "RANGE", "LIST", or None
            - normalized_columns: List of Column expressions, or function expressions
        """
        parsed_cols: t.List[exp.Expression] = []
        partition_kind: t.Optional[str] = None

        normalized = PropertyValidator.validate_and_normalize_property(
            "partitioned_by", partitioned_by, preprocess_parentheses=True
        )
        # Process each normalized expression
        for norm_expr in normalized:
            # Check if it's a RANGE function (exp.Anonymous)
            if isinstance(norm_expr, exp.Anonymous) and norm_expr.this:
                func_name = str(norm_expr.this).upper()
                if func_name in ("RANGE", "LIST"):
                    partition_kind = func_name
                    # Extract column expressions from function arguments
                    for arg in norm_expr.expressions:
                        if isinstance(arg, exp.Column):
                            parsed_cols.append(arg)
                        else:
                            parsed_cols.append(exp.to_column(str(arg)))
                    continue

            # Check if it's a LIST expression (SQLGlot parses LIST(...) as exp.List)
            if isinstance(norm_expr, exp.List):
                partition_kind = "LIST"
                # Extract column expressions from list items
                for item in norm_expr.expressions:
                    if isinstance(item, exp.Column):
                        parsed_cols.append(item)
                    else:
                        parsed_cols.append(exp.to_column(str(item)))
                continue

            # Regular column or other function (date_trunc, etc.)
            parsed_cols.append(norm_expr)

        return partition_kind, parsed_cols

    def _build_partitioned_by_exp(
        self,
        partitioned_by: t.List[exp.Expression],
        *,
        partition_interval_unit: t.Optional["IntervalUnit"] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        catalog_name: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[
        t.Union[
            exp.PartitionedByProperty,
            exp.PartitionByRangeProperty,
            exp.PartitionByListProperty,
            exp.Property,
        ]
    ]:
        """
        Build StarRocks partitioning expression.

        - partition_kind: RANGE/LIST/None (passed via kwargs, None as expression partitioning)
        - partitioned_by: normalized partition column/func/anonymous expressions
        - partitions: partition definitions as List[str] (passed via kwargs)

        Supports both RANGE and LIST partition syntaxes, and expression partition syntax.

        Args:
            partitioned_by: List of partition column expressions
            partition_interval_unit: Optional time unit (unused for now)
            target_columns_to_types: Column definitions (unused for now)
            catalog_name: Catalog name (unused for now)
            **kwargs: Must contain 'partition_kind' and optionally 'partitions'

        Returns:
            PartitionByRangeProperty, PartitionByListProperty, or None
        """
        partition_kind = kwargs.get("partition_kind")
        partitions: t.Optional[t.List[str]] = kwargs.get("partitions")

        # Process partitions to create_expressions
        # partitions is already List[str] after SPEC normalization
        create_expressions: t.Optional[t.List[exp.Var]] = None
        if partitions:
            create_expressions = [exp.Var(this=p, quoted=False) for p in partitions]

        # Build partition expression
        if partition_kind == "LIST":
            return exp.PartitionByListProperty(
                partition_expressions=partitioned_by,
                create_expressions=create_expressions,
            )
        elif partition_kind == "RANGE":  # noqa: RET505
            return exp.PartitionByRangeProperty(
                partition_expressions=partitioned_by,
                create_expressions=create_expressions,
            )
        elif partition_kind is None:
            return exp.PartitionedByProperty(this=exp.Schema(expressions=partitioned_by))

        return None

    def _build_distributed_by_property(
        self,
        table_properties: t.Dict[str, t.Any],
        key_columns: t.Optional[t.Tuple[str, ...]],
    ) -> t.Optional[exp.DistributedByProperty]:
        """
        Build DISTRIBUTED BY property from table_properties.

        Supports:
        1. Structured tuple: (kind='HASH', columns=(id, dt), buckets=10)
        2. String format: "HASH(id)", "RANDOM", "HASH(id) BUCKETS 10"
        3. None: Returns None (no default distribution)

        For complex string like "HASH(id) BUCKETS 10", uses split-and-combine:
        - Split on 'BUCKETS' to separate HASH part and bucket count
        - Parse HASH part via DistributedByInputSpec
        - Parse bucket count as number
        - Combine into unified dict

        Args:
            table_properties: Dictionary containing distributed_by (will be modified)
            key_columns: Table key columns (used for default distribution)

        Returns:
            DistributedByProperty or None
        """
        distributed_by = table_properties.pop("distributed_by", None)

        # No default - if not set, return None
        if distributed_by is None:
            return None

        # Try to parse complex string with BUCKETS first
        unified = self._parse_distribution_with_buckets(distributed_by)
        if unified is None:
            # Fall back to SPEC-based parsing
            normalized = PropertyValidator.validate_and_normalize_property(
                "distributed_by", distributed_by
            )
            # Convert to unified dict format
            unified = DistributionTupleOutputType.to_unified_dict(normalized)

        logger.debug(
            "_build_distributed_by_property: normalized to kind=%s, columns=%s, buckets=%s",
            unified.get("kind"),
            unified.get("columns"),
            unified.get("buckets"),
        )

        # Build expression
        kind_expr = exp.Var(this=unified["kind"])
        # Convert columns to expressions
        columns: t.List[exp.Column] = unified.get("columns", [])
        expressions_list: t.List[exp.Expression] = []
        for col in columns:
            if isinstance(col, exp.Expression):
                expressions_list.append(col)
            else:
                expressions_list.append(exp.to_column(str(col)))
        # Build buckets expression
        buckets: t.Optional[t.Any] = unified.get("buckets")
        buckets_expr: t.Optional[exp.Expression] = None
        if buckets is not None:
            if isinstance(buckets, exp.Literal):
                buckets_expr = buckets
            else:
                buckets_expr = exp.Literal.number(int(buckets))

        result = exp.DistributedByProperty(
            kind=kind_expr,
            expressions=expressions_list,
            buckets=buckets_expr,
            order=None,
        )
        return result

    def _build_refresh_property(
        self, table_properties: t.Dict[str, t.Any]
    ) -> t.Optional[exp.RefreshTriggerProperty]:
        """
        Build StarRocks MV REFRESH clause as exp.RefreshTriggerProperty.

        Input (from physical_properties):
        - refresh_moment: IMMEDIATE | DEFERRED (optional)
        - refresh_scheme: MANUAL | ASYNC [START (<start_time>)] EVERY (INTERVAL <n> <unit>) (optional)

        Output mapping (to match sqlglot StarRocks generator refreshtriggerproperty_sql):
        - method: refresh_moment when provided; otherwise a sentinel that won't render
        - kind: ASYNC | MANUAL
        - starts/every/unit: parsed from refresh_scheme if present
        """
        refresh_moment = table_properties.pop("refresh_moment", None)
        refresh_scheme = table_properties.pop("refresh_scheme", None)
        if refresh_moment is None and refresh_scheme is None:
            return None

        # method is required by exp.RefreshTriggerProperty, but StarRocks syntax does NOT support AUTO.
        # We use a sentinel value that the StarRocks generator will not render (it only renders
        # IMMEDIATE/DEFERRED).
        method_expr = None
        if refresh_moment is not None:
            refresh_moment_text = PropertyValidator.validate_and_normalize_property(
                "refresh_moment", refresh_moment
            )
            method_expr = exp.Var(this=refresh_moment_text)

        kind_expr: t.Optional[exp.Expression] = None
        starts_expr: t.Optional[exp.Expression] = None
        every_expr: t.Optional[exp.Expression] = None
        unit_expr: t.Optional[exp.Expression] = None

        if refresh_scheme is not None:
            scheme_text = PropertyValidator.validate_and_normalize_property(
                "refresh_scheme", refresh_scheme
            )
            if isinstance(scheme_text, exp.Var):
                kind_expr = scheme_text
            else:
                kind_expr, starts_expr, every_expr, unit_expr = self._parse_refresh_scheme(
                    scheme_text
                )

        return exp.RefreshTriggerProperty(
            method=method_expr,
            kind=kind_expr,
            starts=starts_expr,
            every=every_expr,
            unit=unit_expr,
        )

    def _parse_refresh_scheme(
        self, refresh_scheme: str
    ) -> t.Tuple[
        t.Optional[exp.Expression],
        t.Optional[exp.Expression],
        t.Optional[exp.Expression],
        t.Optional[exp.Expression],
    ]:
        """
        Parse StarRocks refresh_scheme text into (kind, starts, every, unit).

        parsing simple and robust. We only extract:
        - kind: ASYNC | MANUAL (must appear at the beginning), None if not provided
        - starts: START (<start_time>) where <start_time> is treated as a raw string
        - every/unit: EVERY (INTERVAL <n> <unit>)
        """
        text = (refresh_scheme or "").strip()
        if not text:
            return None, None, None, None

        m_kind = re.match(r"^(MANUAL|ASYNC)\b", text, flags=re.IGNORECASE)
        if not m_kind:
            raise SQLMeshError(
                f"[StarRocks] Invalid refresh_scheme {refresh_scheme!r}. Expected to start with MANUAL or ASYNC."
            )
        kind = m_kind.group(1).upper()
        kind_expr: t.Optional[exp.Expression] = exp.Var(this=kind)

        starts_expr: t.Optional[exp.Expression] = None
        every_expr: t.Optional[exp.Expression] = None
        unit_expr: t.Optional[exp.Expression] = None
        m_start = re.search(
            r"\bSTART\s*\(\s*(?:'([^']*)'|\"([^\"]*)\"|([^)]*))\s*\)", text, flags=re.IGNORECASE
        )
        if m_start:
            start_inner = (m_start.group(1) or m_start.group(2) or m_start.group(3) or "").strip()
            starts_expr = exp.Literal.string(start_inner)
        m_every = re.search(
            r"\bEVERY\s*\(\s*INTERVAL\s+(\d+)\s+(\w+)\s*\)", text, flags=re.IGNORECASE
        )
        if m_every:
            every_expr = exp.Literal.number(int(m_every.group(1)))
            unit_expr = exp.Var(this=m_every.group(2).upper())
        return kind_expr, starts_expr, every_expr, unit_expr

    def _parse_distribution_with_buckets(
        self, distributed_by: t.Any
    ) -> t.Optional[t.Dict[str, t.Any]]:
        """
        Parse complex distribution expressions like 'HASH(id) BUCKETS 10'.

        Since SQLGlot cannot parse 'HASH(id) BUCKETS 10' directly, we:
        1. Detect if input is a string containing 'BUCKETS'
        2. Split into HASH part and BUCKETS part
        3. Parse HASH part via DistributedByInputSpec
        4. Extract bucket count as number
        5. Combine into unified dict

        Args:
            distributed_by: The distribution value (may be string, expression, etc.)

        Returns:
            Unified dict with keys: kind, columns, buckets
            Returns None if not a complex BUCKETS expression
            (The output function will still handle "HASH(id)" without BUCKETS)
        """
        # Only handle string or Literal string values
        if isinstance(distributed_by, str):
            text = distributed_by
        elif isinstance(distributed_by, exp.Literal) and distributed_by.is_string:
            text = str(distributed_by.this)
        else:
            return None

        # Check if contains BUCKETS keyword (case-insensitive)
        if "BUCKETS" not in text.upper():
            return None

        # Split on BUCKETS (case-insensitive)
        match = re.match(r"^(.+?)\s+BUCKETS\s+(\d+)\s*$", text.strip(), flags=re.IGNORECASE)
        if not match:
            return None

        hash_part = match.group(1).strip()
        buckets_str = match.group(2)

        # Parse the HASH/RANDOM part via SPEC
        normalized = PropertyValidator.validate_and_normalize_property("distributed_by", hash_part)

        return DistributionTupleOutputType.to_unified_dict(normalized, int(buckets_str))

    def _build_order_by_property(
        self,
        table_properties: t.Dict[str, t.Any],
        clustered_by: t.Optional[t.List[exp.Expression]],
    ) -> t.Optional[exp.Cluster]:
        """
        Build ORDER BY (clustering) property.

        Supports both:
        - clustered_by parameter (from create_table call)
        - order_by in table_properties (backward compatibility alias)

        Priority: clustered_by parameter > order_by in table_properties

        Args:
            table_properties: Dictionary containing optional order_by (will be modified)
            clustered_by: Clustering columns from parameter

        Returns:
            Cluster expression (generates ORDER BY) or None
        """
        # Priority: clustered_by parameter > order_by in table_properties
        # Use PropertyValidator to check mutual exclusion between parameter and property
        order_by_param_name = PropertyValidator.check_at_most_one(
            property_name="clustered_by",
            property_description="clustering definition",
            table_properties=table_properties,
            parameter_value=clustered_by,
        )

        # If parameter was provided, it takes priority
        if clustered_by is None and order_by_param_name:
            # Get order_by from table_properties (already validated by check_at_most_one)
            order_by = table_properties.pop(order_by_param_name, None)
            if order_by is not None:
                normalized = PropertyValidator.validate_and_normalize_property(
                    "clustered_by", order_by, preprocess_parentheses=True
                )
                clustered_by = list(normalized)

        if clustered_by:
            result = exp.Cluster(expressions=clustered_by)
            return result
        else:  # noqa: RET505
            return None

    def _build_other_properties(self, table_properties: t.Dict[str, t.Any]) -> t.List[exp.Property]:
        """
        Build other literal properties (replication_num, storage_medium, etc.).

        Uses validate_and_normalize_property for validation and ensures output is string,
        as StarRocks PROPERTIES syntax requires all values to be strings.

        Args:
            table_properties: Dictionary containing properties (will be modified)

        Returns:
            List of Property expressions
        """
        other_props = []

        for key, value in list(table_properties.items()):
            # Skip special keys handled elsewhere
            if key in PropertyValidator.IMPORTANT_PROPERTY_NAMES:
                logger.warning(f"[StarRocks] {key!r} should have been processed already, skipping")
                continue

            # Remove from properties
            table_properties.pop(key)

            # Validate and normalize to string
            # All other properties are treated as generic string properties
            try:
                normalized = PropertyValidator.validate_and_normalize_property(key, value)
                other_props.append(
                    exp.Property(
                        this=exp.to_identifier(key),
                        value=exp.Literal.string(str(normalized)),
                    )
                )
            except SQLMeshError as e:
                logger.warning("[StarRocks] skipping property %s due to error: %s", key, e)

        return other_props

    def _extract_and_validate_key_columns(
        self,
        table_properties: t.Dict[str, t.Any],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
    ) -> t.Tuple[t.Optional[str], t.Optional[t.Tuple[str, ...]]]:
        """
        Extract and validate key columns from table_properties.

        All key types require:
        - Key columns must be the first N columns in CREATE TABLE
        - Column order must match the KEY clause order

        Priority:
        - Parameter primary_key > table_properties primary_key
        - Only one key type allowed per table

        Args:
            table_properties: Table properties dictionary (lowercase keys expected)
            primary_key: Primary key from method parameter (highest priority)

        Returns:
            Tuple of (key_type, key_columns)
            - key_type: One of 'primary_key', 'unique_key', 'duplicate_key', 'aggregate_key', None
            - key_columns: Tuple of column names, or None

        Raises:
            SQLMeshError: If multiple key types are defined or column extraction fails
        """
        # Use PropertyValidator to check mutual exclusion
        active_key_type = PropertyValidator.check_at_most_one(
            property_name="key_type",  # dummy
            property_description="table key type",
            table_properties=table_properties,
            parameter_value=primary_key,
        )

        # If parameter primary_key was provided, return it
        if primary_key:
            return ("primary_key", primary_key)

        # Extract from table_properties
        if not active_key_type:
            return (None, None)

        # Get the key expression and normalize via SPEC
        key_expr = table_properties[active_key_type]  # Read without popping
        # Use validate_and_normalize_property to get List[exp.Column], then extract names
        normalized = PropertyValidator.validate_and_normalize_property(
            active_key_type, key_expr, preprocess_parentheses=True
        )
        key_columns = tuple(col.name for col in normalized)

        return (active_key_type, key_columns)

    def _reorder_columns_for_key(
        self,
        target_columns_to_types: t.Dict[str, exp.DataType],
        key_columns: t.Tuple[str, ...],
        key_type: str = "key",
    ) -> t.Dict[str, exp.DataType]:
        """
        Reorder columns to place key columns first.

        StarRocks Constraint (ALL Table Types):
        Key columns (PRIMARY/UNIQUE/DUPLICATE/AGGREGATE) MUST be the first N columns
        in the CREATE TABLE statement, in the same order as defined in the KEY clause.

        Example:
            Input:
                columns = {"customer_id": INT, "order_id": BIGINT, "event_date": DATE}
                key_columns = ("order_id", "event_date")
                key_type = "primary_key"

            Output:
                {"order_id": BIGINT, "event_date": DATE, "customer_id": INT}

        Args:
            target_columns_to_types: Original column order (from SELECT)
            key_columns: Key column names in desired order
            key_type: Type of key for logging (primary_key, unique_key, etc.)

        Returns:
            Reordered columns with key columns first

        Raises:
            SQLMeshError: If a key column is not found in target_columns_to_types
        """
        # Validate that all key columns exist
        missing_key_cols = set(key_columns) - set(target_columns_to_types.keys())
        if missing_key_cols:
            raise SQLMeshError(
                f"{key_type} columns {missing_key_cols} not found in table columns. "
                f"Available columns: {list(target_columns_to_types.keys())}"
            )

        # Build new ordered dict: key columns first, then remaining columns
        reordered = {}

        # 1. Add key columns in key order
        for key_col in key_columns:
            reordered[key_col] = target_columns_to_types[key_col]

        # 2. Add remaining columns (preserve original order)
        for col_name, col_type in target_columns_to_types.items():
            if col_name not in key_columns:
                reordered[col_name] = col_type

        logger.info(
            f"Reordered columns for {key_type.upper()}: "
            f"Original order: {list(target_columns_to_types.keys())}, "
            f"New order: {list(reordered.keys())}"
        )

        return reordered

    def _build_create_comment_table_exp(
        self, table: exp.Table, table_comment: str, table_kind: str = "TABLE"
    ) -> str:
        """
        Build ALTER TABLE COMMENT SQL for table comment modification.

        StarRocks uses non-standard syntax for table comments:
            ALTER TABLE {table} COMMENT = '{comment}'

        Note: This method is typically NOT called for StarRocks because:
        - COMMENT_CREATION_TABLE = IN_SCHEMA_DEF_CTAS
        - Comments are included directly in CREATE TABLE via SchemaCommentProperty

        However, this override is provided for potential future use cases:
        - Modifying comments on existing tables via ALTER TABLE
        - View comments (if COMMENT_CREATION_VIEW changes)

        Args:
            table: Table expression
            table_comment: The comment to add
            table_kind: Type of object (TABLE, VIEW, etc.)

        Returns:
            SQL string for ALTER TABLE COMMENT
        """
        table_sql = table.sql(dialect=self.dialect, identify=True)
        comment_sql = exp.Literal.string(self._truncate_table_comment(table_comment)).sql(
            dialect=self.dialect
        )
        return f"ALTER TABLE {table_sql} COMMENT = {comment_sql}"

    def _build_create_comment_column_exp(
        self,
        table: exp.Table,
        column_name: str,
        column_comment: str,
        table_kind: str = "TABLE",
    ) -> str:
        """
        Build ALTER TABLE MODIFY COLUMN SQL for column comment modification.

        StarRocks requires column type in MODIFY COLUMN statement:
            ALTER TABLE {table} MODIFY COLUMN {column} {type} COMMENT '{comment}'

        Note: This method is typically NOT called for StarRocks because:
        - COMMENT_CREATION_TABLE = IN_SCHEMA_DEF_CTAS
        - Column comments are included directly in CREATE TABLE DDL

        However, this override is provided for potential future use cases:
        - Modifying column comments on existing tables via ALTER TABLE

        Args:
            table: Table expression
            column_name: Name of the column
            column_comment: The comment to add
            table_kind: Type of object (TABLE, VIEW, etc.)

        Returns:
            SQL string for ALTER TABLE MODIFY COLUMN with COMMENT
        """
        table_sql = table.sql(dialect=self.dialect, identify=True)
        column_sql = exp.to_identifier(column_name).sql(dialect=self.dialect, identify=True)

        comment_sql = exp.Literal.string(self._truncate_column_comment(column_comment)).sql(
            dialect=self.dialect
        )

        return f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} COMMENT {comment_sql}"

    # ==================== Methods NOT Needing Override (Base Class Works) ====================
    # The following methods work correctly with base class implementation:
    # - columns(): Query column definitions via DESCRIBE TABLE
    # - table_exists(): Check if table exists via information_schema
    # - insert_append(): Standard INSERT INTO ... SELECT
    # - insert_overwrite_by_time_partition(): Uses DELETE_INSERT strategy (handled by base)
    # - fetchall() / fetchone(): Standard query execution
    # - execute(): Base SQL execution. (Modifyed for `FOR UPDATE` lock operation only)
    # - create_table_properties(): Delegate to _build_table_properties_exp()
