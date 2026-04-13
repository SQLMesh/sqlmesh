from __future__ import annotations

import json
import typing as t
from datetime import tzinfo

import pydantic
from pydantic import ValidationInfo as ValidationInfo
from pydantic.fields import FieldInfo
from sqlglot import exp, parse_one
from sqlglot.helper import ensure_list
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_columns import quote_identifiers

from sqlmesh.core import dialect as d
from sqlmesh.utils import str_to_bool

if t.TYPE_CHECKING:
    from sqlglot._typing import E

    Model = t.TypeVar("Model", bound="PydanticModel")


T = t.TypeVar("T")
DEFAULT_ARGS = {"exclude_none": True, "by_alias": True}
PRIVATE_FIELDS = "__pydantic_private__"
PYDANTIC_MAJOR_VERSION, PYDANTIC_MINOR_VERSION = [int(p) for p in pydantic.__version__.split(".")][
    :2
]


def field_validator(*args: t.Any, **kwargs: t.Any) -> t.Callable[[t.Any], t.Any]:
    return pydantic.field_validator(*args, **kwargs)


def model_validator(*args: t.Any, **kwargs: t.Any) -> t.Callable[[t.Any], t.Any]:
    return pydantic.model_validator(*args, **kwargs)


def field_serializer(*args: t.Any, **kwargs: t.Any) -> t.Callable[[t.Any], t.Any]:
    return pydantic.field_serializer(*args, **kwargs)


def validation_data(info_or_data: t.Any) -> t.Dict[str, t.Any]:
    """Safely extract the validated-data dict from a ValidationInfo, dict, or None.

    Pydantic 2.13+ sets ValidationInfo.data to None during model_validate_json().
    This normalizes all inputs to a dict, returning an empty dict when data is unavailable.
    """
    if isinstance(info_or_data, dict):
        return info_or_data
    if info_or_data is not None:
        return info_or_data.data or {}
    return {}


def get_dialect(values: t.Any) -> str:
    """Extracts dialect from a dict or pydantic obj, defaulting to the globally set dialect.

    Python models allow users to instantiate pydantic models by hand. This is problematic
    because the validators kick in with the SQLGLot dialect. To instantiate Pydantic Models used
    in python models using the project default dialect, we set a class variable on the model
    registry and use that here.
    """

    from sqlmesh.core.model import model

    dialect = validation_data(values).get("dialect")
    return model._dialect if dialect is None else dialect  # type: ignore


def _expression_encoder(e: exp.Expr) -> str:
    return e.meta.get("sql") or e.sql(dialect=e.meta.get("dialect"))


AuditQueryTypes = t.Union[exp.Query, d.JinjaQuery]
ModelQueryTypes = t.Union[exp.Query, d.JinjaQuery, d.MacroFunc]


class PydanticModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        # Even though Pydantic v2 kept support for json_encoders, the functionality has been
        # crippled badly. Here we need to enumerate all different ways of how sqlglot expressions
        # show up in pydantic models.
        json_encoders={
            exp.Expr: _expression_encoder,
            exp.DataType: _expression_encoder,
            exp.Tuple: _expression_encoder,
            AuditQueryTypes: _expression_encoder,  # type: ignore
            ModelQueryTypes: _expression_encoder,  # type: ignore
            tzinfo: lambda tz: tz.key,
        },
        arbitrary_types_allowed=True,
        extra="forbid",
        protected_namespaces=(),
    )

    _hash_func_mapping: t.ClassVar[t.Dict[t.Type[t.Any], t.Callable[[t.Any], int]]] = {}

    def dict(self, **kwargs: t.Any) -> t.Dict[str, t.Any]:
        kwargs = {**DEFAULT_ARGS, **kwargs}
        return super().model_dump(**kwargs)  # type: ignore

    def json(
        self,
        **kwargs: t.Any,
    ) -> str:
        kwargs = {**DEFAULT_ARGS, **kwargs}
        # Pydantic v2 doesn't support arbitrary arguments for json.dump().
        if kwargs.pop("sort_keys", False):
            return json.dumps(super().model_dump(mode="json", **kwargs), sort_keys=True)

        return super().model_dump_json(**kwargs)

    def copy(self: "Model", **kwargs: t.Any) -> "Model":
        return super().model_copy(**kwargs)

    @property
    def fields_set(self: "Model") -> t.Set[str]:
        return self.__pydantic_fields_set__

    @classmethod
    def parse_obj(cls: t.Type["Model"], obj: t.Any) -> "Model":
        return super().model_validate(obj)

    @classmethod
    def parse_raw(cls: t.Type["Model"], b: t.Union[str, bytes], **kwargs: t.Any) -> "Model":
        return super().model_validate_json(b, **kwargs)

    @classmethod
    def missing_required_fields(
        cls: t.Type["PydanticModel"], provided_fields: t.Set[str]
    ) -> t.Set[str]:
        return cls.required_fields() - provided_fields

    @classmethod
    def extra_fields(cls: t.Type["PydanticModel"], provided_fields: t.Set[str]) -> t.Set[str]:
        return provided_fields - cls.all_fields()

    @classmethod
    def all_fields(cls: t.Type["PydanticModel"]) -> t.Set[str]:
        return cls._fields()

    @classmethod
    def all_field_infos(cls: t.Type["PydanticModel"]) -> t.Dict[str, FieldInfo]:
        return cls.model_fields

    @classmethod
    def required_fields(cls: t.Type["PydanticModel"]) -> t.Set[str]:
        return cls._fields(lambda field: field.is_required())

    @classmethod
    def _fields(
        cls: t.Type["PydanticModel"],
        predicate: t.Callable[[t.Any], bool] = lambda _: True,
    ) -> t.Set[str]:
        return {
            field_info.alias if field_info.alias else field_name
            for field_name, field_info in cls.all_field_infos().items()
            if predicate(field_info)
        }

    def __eq__(self, other: t.Any) -> bool:
        if (PYDANTIC_MAJOR_VERSION, PYDANTIC_MINOR_VERSION) < (2, 6):
            if isinstance(other, pydantic.BaseModel):
                return self.dict() == other.dict()
            return self.dict() == other
        return super().__eq__(other)

    def __hash__(self) -> int:
        if (PYDANTIC_MAJOR_VERSION, PYDANTIC_MINOR_VERSION) < (2, 6):
            obj = {k: v for k, v in self.__dict__.items() if k in self.all_field_infos()}
            return hash(self.__class__) + hash(tuple(obj.values()))

        from pydantic._internal._model_construction import make_hash_func  # type: ignore

        if self.__class__ not in PydanticModel._hash_func_mapping:
            PydanticModel._hash_func_mapping[self.__class__] = make_hash_func(self.__class__)

        return PydanticModel._hash_func_mapping[self.__class__](self)

    def __str__(self) -> str:
        args = []

        for k, info in self.all_field_infos().items():
            v = getattr(self, k)

            if type(v) != type(info.default) or v != info.default:
                args.append(f"{k}: {v}")

        return f"{self.__class__.__name__}<{', '.join(args)}>"

    def __repr__(self) -> str:
        return str(self)


def validate_list_of_strings(v: t.Any) -> t.List[str]:
    if isinstance(v, exp.Identifier):
        return [v.name]
    if isinstance(v, (exp.Tuple, exp.Array)):
        return [e.name for e in v.expressions]
    return [i.name if isinstance(i, exp.Identifier) else str(i) for i in v]


def validate_string(v: t.Any) -> str:
    if isinstance(v, exp.Expr):
        return v.name
    return str(v)


def validate_expression(expression: E, dialect: str) -> E:
    # this normalizes and quotes identifiers in the given expression according the specified dialect
    # it also sets expression.meta["dialect"] so that when we serialize for state, the expression is serialized in the correct dialect
    return _get_field(expression, {"dialect": dialect})  # type: ignore


def bool_validator(v: t.Any) -> bool:
    if isinstance(v, exp.Boolean):
        return v.this
    if isinstance(v, exp.Expr):
        return str_to_bool(v.name)
    return str_to_bool(str(v or ""))


def positive_int_validator(v: t.Any) -> int:
    if isinstance(v, exp.Expr) and v.is_int:
        v = int(v.name)
    if not isinstance(v, int):
        raise ValueError(f"Invalid num {v}. Value must be an integer value")
    if v <= 0:
        raise ValueError(f"Invalid num {v}. Value must be a positive integer")
    return v


def validation_error_message(error: pydantic.ValidationError, base: str) -> str:
    errors = "\n  ".join(_formatted_validation_errors(error))
    return f"{base}\n  {errors}"


def _formatted_validation_errors(error: pydantic.ValidationError) -> t.List[str]:
    result = []
    for e in error.errors():
        msg = e["msg"]
        loc: t.Optional[t.Tuple] = e.get("loc")
        loc_str = ".".join(loc) if loc else None
        result.append(f"Invalid field '{loc_str}':\n    {msg}" if loc_str else msg)
    return result


def _get_field(
    v: t.Any,
    values: t.Any,
) -> exp.Expr:
    dialect = get_dialect(values)

    if isinstance(v, exp.Expr):
        expression = v
    else:
        expression = parse_one(v, dialect=dialect)

    expression = exp.column(expression) if isinstance(expression, exp.Identifier) else expression
    expression = quote_identifiers(
        normalize_identifiers(expression, dialect=dialect), dialect=dialect
    )
    expression.meta["dialect"] = dialect

    return expression


def _get_fields(
    v: t.Any,
    values: t.Any,
) -> t.List[exp.Expr]:
    dialect = get_dialect(values)

    if isinstance(v, (exp.Tuple, exp.Array)):
        expressions: t.List[exp.Expr] = v.expressions
    elif isinstance(v, exp.Expr):
        expressions = [v]
    else:
        items: t.List[t.Any] = ensure_list(v)
        expressions = [
            parse_one(entry, dialect=dialect) if isinstance(entry, str) else entry  # type: ignore[misc]
            for entry in items
        ]

    results = []

    for expr in expressions:
        results.append(_get_field(expr, values))

    return results


def list_of_fields_validator(v: t.Any, values: t.Any) -> t.List[exp.Expr]:
    return _get_fields(v, values)


def column_validator(v: t.Any, values: t.Any) -> exp.Column:
    expression = _get_field(v, values)
    if not isinstance(expression, exp.Column):
        raise ValueError(f"Invalid column {expression}. Value must be a column")
    return expression


def list_of_fields_or_star_validator(
    v: t.Any, values: t.Any
) -> t.Union[exp.Star, t.List[exp.Expr]]:
    expressions = _get_fields(v, values)
    if len(expressions) == 1 and isinstance(expressions[0], exp.Star):
        return t.cast(exp.Star, expressions[0])
    return t.cast(t.List[exp.Expr], expressions)


def cron_validator(v: t.Any) -> str:
    if isinstance(v, exp.Expr):
        v = v.name

    from croniter import CroniterBadCronError, croniter

    if not isinstance(v, str):
        raise ValueError(f"Invalid cron expression '{v}'. Value must be a string.")

    try:
        croniter(v)
    except CroniterBadCronError:
        raise ValueError(f"Invalid cron expression '{v}'")
    return v


def get_concrete_types_from_typehint(typehint: type[t.Any]) -> set[type[t.Any]]:
    concrete_types = set()
    unpacked = t.get_origin(typehint)
    if unpacked is None:
        if type(typehint) == type(type):
            return {typehint}
    elif unpacked is t.Union:
        for item in t.get_args(typehint):
            if str(item).startswith("typing."):
                concrete_types |= get_concrete_types_from_typehint(item)
            else:
                concrete_types.add(item)
    else:
        concrete_types.add(unpacked)

    return concrete_types


if t.TYPE_CHECKING:
    SQLGlotListOfStrings = t.List[str]
    SQLGlotString = str
    SQLGlotBool = bool
    SQLGlotPositiveInt = int
    SQLGlotColumn = exp.Column
    SQLGlotListOfFields = t.List[exp.Expr]
    SQLGlotListOfFieldsOrStar = t.Union[SQLGlotListOfFields, exp.Star]
    SQLGlotCron = str
else:
    from pydantic.functional_validators import BeforeValidator

    SQLGlotListOfStrings = t.Annotated[t.List[str], BeforeValidator(validate_list_of_strings)]
    SQLGlotString = t.Annotated[str, BeforeValidator(validate_string)]
    SQLGlotBool = t.Annotated[bool, BeforeValidator(bool_validator)]
    SQLGlotPositiveInt = t.Annotated[int, BeforeValidator(positive_int_validator)]
    SQLGlotColumn = t.Annotated[exp.Expr, BeforeValidator(column_validator)]
    SQLGlotListOfFields = t.Annotated[t.List[exp.Expr], BeforeValidator(list_of_fields_validator)]
    SQLGlotListOfFieldsOrStar = t.Annotated[
        t.Union[SQLGlotListOfFields, exp.Star], BeforeValidator(list_of_fields_or_star_validator)
    ]
    SQLGlotCron = t.Annotated[str, BeforeValidator(cron_validator)]
