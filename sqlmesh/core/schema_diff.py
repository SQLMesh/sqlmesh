from __future__ import annotations

import abc
import logging
import typing as t
from dataclasses import dataclass
from collections import defaultdict
from enum import Enum

from pydantic import Field
from sqlglot import exp
from sqlglot.helper import ensure_list, seq_get

from sqlmesh.utils import columns_to_types_to_struct
from sqlmesh.utils.pydantic import PydanticModel
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import TableName

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableAlterOperation(abc.ABC):
    target_table: exp.Table

    @property
    @abc.abstractmethod
    def is_destructive(self) -> bool:
        pass

    @property
    @abc.abstractmethod
    def is_additive(self) -> bool:
        pass

    @property
    @abc.abstractmethod
    def _alter_actions(self) -> t.List[exp.Expr]:
        pass

    @property
    def expression(self) -> exp.Alter:
        return exp.Alter(
            this=self.target_table,
            kind="TABLE",
            actions=self._alter_actions,
        )


@dataclass(frozen=True)
class TableAlterColumnOperation(TableAlterOperation, abc.ABC):
    column_parts: t.List[TableAlterColumn]
    expected_table_struct: exp.DataType
    array_element_selector: str

    @property
    def column_identifiers(self) -> t.List[exp.Identifier]:
        results = []
        for column in self.column_parts:
            results.append(column.identifier)
            if (
                column.is_array_of_struct
                and len(self.column_parts) > 1
                and self.array_element_selector
            ):
                results.append(exp.to_identifier(self.array_element_selector))
        return results

    @property
    def column(self) -> t.Union[exp.Dot, exp.Identifier]:
        columns = self.column_identifiers
        if len(columns) == 1:
            return columns[0]
        return exp.Dot.build(columns)


@dataclass(frozen=True)
class TableAlterTypedColumnOperation(TableAlterColumnOperation, abc.ABC):
    column_type: exp.DataType

    @property
    def column_def(self) -> exp.ColumnDef:
        if not self.column_type:
            raise SQLMeshError("Tried to access column type when it shouldn't be needed")
        return exp.ColumnDef(
            this=self.column,
            kind=self.column_type,
        )


@dataclass(frozen=True)
class TableAlterAddColumnOperation(TableAlterTypedColumnOperation):
    position: t.Optional[TableAlterColumnPosition] = None
    is_part_of_destructive_change: bool = False

    @property
    def is_additive(self) -> bool:
        return not self.is_part_of_destructive_change

    @property
    def is_destructive(self) -> bool:
        return self.is_part_of_destructive_change

    @property
    def _alter_actions(self) -> t.List[exp.Expr]:
        column_def = exp.ColumnDef(
            this=self.column,
            kind=self.column_type,
        )
        if self.position:
            column_def.set("position", self.position.column_position_node)
        return [column_def]


@dataclass(frozen=True)
class TableAlterDropColumnOperation(TableAlterColumnOperation):
    cascade: bool = False

    @property
    def is_additive(self) -> bool:
        return False

    @property
    def is_destructive(self) -> bool:
        return True

    @property
    def _alter_actions(self) -> t.List[exp.Expr]:
        return [exp.Drop(this=self.column, kind="COLUMN", cascade=self.cascade)]


@dataclass(frozen=True)
class TableAlterChangeColumnTypeOperation(TableAlterTypedColumnOperation):
    current_type: exp.DataType
    is_part_of_destructive_change: bool = False

    @property
    def is_additive(self) -> bool:
        return not self.is_part_of_destructive_change

    @property
    def is_destructive(self) -> bool:
        return self.is_part_of_destructive_change

    @property
    def _alter_actions(self) -> t.List[exp.Expr]:
        return [
            exp.AlterColumn(
                this=self.column,
                dtype=self.column_type,
            )
        ]


@dataclass(frozen=True)
class TableAlterColumn:
    name: str
    is_struct: bool
    is_array_of_struct: bool
    is_array_of_primitive: bool
    quoted: bool = False

    @classmethod
    def primitive(cls, name: str, quoted: bool = False) -> TableAlterColumn:
        return cls(
            name=name,
            is_struct=False,
            is_array_of_struct=False,
            is_array_of_primitive=False,
            quoted=quoted,
        )

    @classmethod
    def struct(cls, name: str, quoted: bool = False) -> TableAlterColumn:
        return cls(
            name=name,
            is_struct=True,
            is_array_of_struct=False,
            is_array_of_primitive=False,
            quoted=quoted,
        )

    @classmethod
    def array_of_struct(cls, name: str, quoted: bool = False) -> TableAlterColumn:
        return cls(
            name=name,
            is_struct=False,
            is_array_of_struct=True,
            is_array_of_primitive=False,
            quoted=quoted,
        )

    @classmethod
    def array_of_primitive(cls, name: str, quoted: bool = False) -> TableAlterColumn:
        return cls(
            name=name,
            is_struct=False,
            is_array_of_struct=False,
            is_array_of_primitive=True,
            quoted=quoted,
        )

    @classmethod
    def from_struct_kwarg(cls, struct: exp.ColumnDef) -> TableAlterColumn:
        name = struct.alias_or_name
        quoted = struct.this.quoted
        kwarg_type = struct.args["kind"]

        if kwarg_type.is_type(exp.DataType.Type.STRUCT):
            return cls.struct(name, quoted=quoted)
        if kwarg_type.is_type(exp.DataType.Type.ARRAY):
            if kwarg_type.expressions and kwarg_type.expressions[0].is_type(
                exp.DataType.Type.STRUCT
            ):
                return cls.array_of_struct(name, quoted=quoted)
            return cls.array_of_primitive(name, quoted=quoted)
        return cls.primitive(name, quoted=quoted)

    @property
    def is_array(self) -> bool:
        return self.is_array_of_struct or self.is_array_of_primitive

    @property
    def is_primitive(self) -> bool:
        return not self.is_struct and not self.is_array

    @property
    def is_nested(self) -> bool:
        return not self.is_primitive

    @property
    def identifier(self) -> exp.Identifier:
        return exp.to_identifier(self.name, quoted=self.quoted)


@dataclass(frozen=True)
class TableAlterColumnPosition:
    is_first: bool
    is_last: bool
    after: t.Optional[exp.Identifier] = None

    @classmethod
    def first(cls) -> TableAlterColumnPosition:
        return cls(is_first=True, is_last=False, after=None)

    @classmethod
    def last(
        cls, after: t.Optional[t.Union[str, exp.Identifier]] = None
    ) -> TableAlterColumnPosition:
        return cls(is_first=False, is_last=True, after=exp.to_identifier(after) if after else None)

    @classmethod
    def middle(cls, after: t.Union[str, exp.Identifier]) -> TableAlterColumnPosition:
        return cls(is_first=False, is_last=False, after=exp.to_identifier(after))

    @classmethod
    def create(
        cls,
        pos: int,
        current_kwargs: t.List[exp.ColumnDef],
        replacing_col: bool = False,
    ) -> TableAlterColumnPosition:
        is_first = pos == 0
        is_last = pos == len(current_kwargs) - int(replacing_col)
        after = None
        if not is_first:
            prior_kwarg = current_kwargs[pos - 1]
            after, _ = _get_name_and_type(prior_kwarg)
        return cls(is_first=is_first, is_last=is_last, after=after)

    @property
    def column_position_node(self) -> t.Optional[exp.ColumnPosition]:
        column = self.after if not self.is_last else None
        position = None
        if self.is_first:
            position = "FIRST"
        elif column and not self.is_last:
            position = "AFTER"
        return exp.ColumnPosition(this=column, position=position)


class NestedSupport(str, Enum):
    # Supports all nested data type operations
    ALL = "ALL"
    # Does not support any nested data type operations
    NONE = "NONE"
    # Supports nested data type operations except for those that require dropping a nested field
    ALL_BUT_DROP = "ALL_BUT_DROP"
    # Ignores all nested data type operations
    IGNORE = "IGNORE"

    @property
    def is_all(self) -> bool:
        return self == NestedSupport.ALL

    @property
    def is_none(self) -> bool:
        return self == NestedSupport.NONE

    @property
    def is_all_but_drop(self) -> bool:
        return self == NestedSupport.ALL_BUT_DROP

    @property
    def is_ignore(self) -> bool:
        return self == NestedSupport.IGNORE


class SchemaDiffer(PydanticModel):
    """
    Compares a source schema against a target schema and returns a list of alter statements to have the source
    match the structure of target. Some engines have constraints on the types of operations that can be performed
    therefore the final structure may not match the target exactly but it will be as close as possible. Two potential
    differences that can happen:
    1. Column order can be different if the engine doesn't support positional additions. Another reason for difference
    is if a column is just moved since we don't currently support fixing moves.
    2. Nested operations will be represented using a drop/add of the root column if the engine doesn't support nested
    operations. As a result historical data is lost.
    3. Column type changes will be reflected but it can be done through a drop/add if the change is not a compatible
    change. As a result historical data is lost.

    Potential future improvements:
    1. Support column moves. Databricks Delta supports moves and would allow exact matches.

    Args:
        support_positional_add: Whether the engine for which the diff is being computed supports adding columns in a
            specific position in the set of existing columns.
        nested_support: How the engine for which the diff is being computed supports nested types.
        compatible_types: Types that are compatible and automatically coerced in actions like UNION ALL. Dict key is data
            type, and value is the set of types that are compatible with it.
        coerceable_types: The mapping from a current type to all types that can be safely coerced to the current one without
            altering the column type. NOTE: usually callers should not specify this attribute manually and set the
            `support_coercing_compatible_types` flag instead. Some engines are inconsistent about their type coercion rules.
            For example, in BigQuery a BIGNUMERIC column can't be altered to be FLOAT64, while BIGNUMERIC values can be inserted
            into a FLOAT64 column just fine.
        support_coercing_compatible_types: Whether or not the engine for which the diff is being computed supports direct
            coercion of compatible types.
        drop_cascade: Whether to add CASCADE modifier when dropping a column.
        parameterized_type_defaults: Default values for parameterized data types. Dict key is a sqlglot exp.DataType.Type,
            but in the engine adapter specification we build it from the dialect string instead of specifying it directly.
            Example: `exp.DataType.build("STRING", dialect=DIALECT).this` instead of the underlying `exp.DataType.Type.TEXT`
            to which it parses. We do that because parameter default replacement will silently break if we specify type
            directly and SQLGlot changes the dialect's mapping of type string to exp.DataType.Type. Dict value is default
            values in a list, where the list index contains the remaining defaults given the number of parameter values
            provided by the user. Example: if user provides 0 parameters "DECIMAL", we return index 0 values for the two
            omitted parameters `(38, 9)` -> "DECIMAL(38,9)". Example: if user provides 1 parameters "DECIMAL(10)", we return
            index 1 value for the one omitted parameters `(0,)` -> "DECIMAL(10,0)".
        max_parameter_length: Numeric parameter values corresponding to "max". Example: `VARCHAR(max)` -> `VARCHAR(65535)`.
        types_with_unlimited_length: Data types that accept values of any length up to system limits. Any explicitly
            parameterized type can ALTER to its unlimited length version, along with different types in some engines.
        treat_alter_data_type_as_destructive: The SchemaDiffer will only output change data type operations if it
            concludes the change is compatible and won't result in data loss. If this flag is set to True, it will
            flag these data type changes as destructive. This was added for dbt adapter support and likely shouldn't
            be set outside of that context.
    """

    support_positional_add: bool = False
    nested_support: NestedSupport = NestedSupport.NONE
    array_element_selector: str = ""
    compatible_types: t.Dict[exp.DataType, t.Set[exp.DataType]] = {}
    coerceable_types_: t.Dict[exp.DataType, t.Set[exp.DataType]] = Field(
        default_factory=dict, alias="coerceable_types"
    )
    precision_increase_allowed_types: t.Optional[t.Set[exp.DType]] = None
    support_coercing_compatible_types: bool = False
    drop_cascade: bool = False
    parameterized_type_defaults: t.Dict[exp.DType, t.List[t.Tuple[t.Union[int, float], ...]]] = {}
    max_parameter_length: t.Dict[exp.DType, t.Union[int, float]] = {}
    types_with_unlimited_length: t.Dict[exp.DType, t.Set[exp.DType]] = {}
    treat_alter_data_type_as_destructive: bool = False

    _coerceable_types: t.Dict[exp.DataType, t.Set[exp.DataType]] = {}

    @property
    def coerceable_types(self) -> t.Dict[exp.DataType, t.Set[exp.DataType]]:
        if not self._coerceable_types:
            if not self.support_coercing_compatible_types or not self.compatible_types:
                return self.coerceable_types_
            coerceable_types: t.Dict[exp.DataType, t.Set[exp.DataType]] = defaultdict(set)
            coerceable_types.update(self.coerceable_types_)
            for source_type, target_types in self.compatible_types.items():
                for target_type in target_types:
                    coerceable_types[target_type].add(source_type)
            self._coerceable_types = coerceable_types
        return self._coerceable_types

    def _is_compatible_type(self, current_type: exp.DataType, new_type: exp.DataType) -> bool:
        # types are identical or both types are parameterized and new has higher precision
        # - default parameter values are automatically provided if not present
        if current_type == new_type or (
            self._is_precision_increase_allowed(current_type)
            and self._is_precision_increase(current_type, new_type)
        ):
            return True
        # types are un-parameterized and compatible
        if current_type in self.compatible_types:
            return new_type in self.compatible_types[current_type]
        # new type is un-parameterized and has unlimited length, current type is compatible
        if not new_type.expressions and new_type.this in self.types_with_unlimited_length:
            return current_type.this in self.types_with_unlimited_length[new_type.this]
        return False

    def _is_coerceable_type(self, current_type: exp.DataType, new_type: exp.DataType) -> bool:
        if current_type in self.coerceable_types:
            is_coerceable = new_type in self.coerceable_types[current_type]
            if is_coerceable:
                from sqlmesh.core.console import get_console

                get_console().log_warning(
                    f"Coercing type {current_type} to {new_type} which means an alter will not be performed and therefore the resulting table structure will not match what is in the query.\nUpdate your model to cast the value to {current_type} type in order to remove this warning."
                )

            return is_coerceable
        return False

    def _is_precision_increase_allowed(self, current_type: exp.DataType) -> bool:
        return (
            self.precision_increase_allowed_types is None
            or current_type.this in self.precision_increase_allowed_types
        )

    def _is_precision_increase(self, current_type: exp.DataType, new_type: exp.DataType) -> bool:
        if current_type.this == new_type.this and not current_type.is_type(
            *exp.DataType.NESTED_TYPES
        ):
            current_params = self.get_type_parameters(current_type)
            new_params = self.get_type_parameters(new_type)

            if len(current_params) != len(new_params):
                return False

            return all(new >= current for current, new in zip(current_params, new_params))
        return False

    def get_type_parameters(self, type: exp.DataType) -> t.List[t.Union[int, float]]:
        def _str_to_number(string: str, allows_max_param: bool) -> t.Union[int, float]:
            try:
                return int(string)
            except ValueError:
                try:
                    return float(string)
                except ValueError:
                    if allows_max_param and string.upper() == "MAX":
                        return self.max_parameter_length[type.this]
                    raise ValueError(f"Could not convert '{string}' to a number")

        # extract existing parameters
        params = [
            _str_to_number(param.this.this, type.this in self.max_parameter_length)
            for param in type.expressions
        ]

        # maybe get default parameter values
        param_defaults: t.Tuple[t.Union[int, float], ...] = ()
        if type.this in self.parameterized_type_defaults:
            param_defaults_list = self.parameterized_type_defaults[type.this]
            if len(params) < len(param_defaults_list):
                param_defaults = param_defaults_list[len(params)]

        return [*params, *param_defaults]

    def _get_matching_kwarg(
        self,
        current_kwarg: t.Union[str, exp.ColumnDef],
        new_struct: exp.DataType,
        current_pos: int,
    ) -> t.Tuple[t.Optional[int], t.Optional[exp.ColumnDef]]:
        current_name = (
            exp.to_identifier(current_kwarg)
            if isinstance(current_kwarg, str)
            else _get_name_and_type(current_kwarg)[0]
        )
        # First check if we have the same column in the same position to get O(1) complexity
        new_kwarg = seq_get(new_struct.expressions, current_pos)
        if new_kwarg:
            new_name, new_type = _get_name_and_type(new_kwarg)
            if current_name.this == new_name.this:
                return current_pos, new_kwarg
        # If not, check if we have the same column in all positions with O(n) complexity
        for i, new_kwarg in enumerate(new_struct.expressions):
            new_name, new_type = _get_name_and_type(new_kwarg)
            if current_name.this == new_name.this:
                return i, new_kwarg
        return None, None

    def _drop_operation(
        self,
        columns: t.Union[TableAlterColumn, t.List[TableAlterColumn]],
        struct: exp.DataType,
        pos: int,
        root_struct: exp.DataType,
        table_name: TableName,
    ) -> t.List[TableAlterColumnOperation]:
        columns = ensure_list(columns)
        operations: t.List[TableAlterColumnOperation] = []
        column_pos, column_kwarg = self._get_matching_kwarg(columns[-1].name, struct, pos)
        if column_pos is None or not column_kwarg:
            raise SQLMeshError(
                f"Cannot drop column '{columns[-1].name}' from table '{table_name}' - column not found. "
                f"This may indicate a mismatch between the expected and actual table schemas."
            )
        struct.expressions.pop(column_pos)
        operations.append(
            TableAlterDropColumnOperation(
                target_table=exp.to_table(table_name),
                column_parts=columns,
                expected_table_struct=root_struct.copy(),
                cascade=self.drop_cascade,
                array_element_selector=self.array_element_selector,
            )
        )
        return operations

    def _requires_drop_alteration(
        self, current_struct: exp.DataType, new_struct: exp.DataType
    ) -> bool:
        for current_pos, current_kwarg in enumerate(current_struct.expressions.copy()):
            new_pos, _ = self._get_matching_kwarg(current_kwarg, new_struct, current_pos)
            if new_pos is None:
                return True
        return False

    def _resolve_drop_operation(
        self,
        parent_columns: t.List[TableAlterColumn],
        current_struct: exp.DataType,
        new_struct: exp.DataType,
        root_struct: exp.DataType,
        table_name: TableName,
    ) -> t.List[TableAlterColumnOperation]:
        operations = []
        for current_pos, current_kwarg in enumerate(current_struct.expressions.copy()):
            new_pos, _ = self._get_matching_kwarg(current_kwarg, new_struct, current_pos)
            columns = parent_columns + [TableAlterColumn.from_struct_kwarg(current_kwarg)]
            if new_pos is None:
                operations.extend(
                    self._drop_operation(
                        columns, current_struct, current_pos, root_struct, table_name
                    )
                )
        return operations

    def _add_operation(
        self,
        columns: t.List[TableAlterColumn],
        new_pos: int,
        new_kwarg: exp.ColumnDef,
        current_struct: exp.DataType,
        root_struct: exp.DataType,
        table_name: TableName,
        is_part_of_destructive_change: bool = False,
    ) -> t.List[TableAlterColumnOperation]:
        if self.support_positional_add:
            col_pos = TableAlterColumnPosition.create(new_pos, current_struct.expressions)
            current_struct.expressions.insert(new_pos, new_kwarg)
        else:
            col_pos = None
            current_struct.expressions.append(new_kwarg)
        return [
            TableAlterAddColumnOperation(
                target_table=exp.to_table(table_name),
                column_parts=columns,
                column_type=new_kwarg.args["kind"],
                expected_table_struct=root_struct.copy(),
                position=col_pos,
                is_part_of_destructive_change=is_part_of_destructive_change,
                array_element_selector=self.array_element_selector,
            )
        ]

    def _resolve_add_operations(
        self,
        parent_columns: t.List[TableAlterColumn],
        current_struct: exp.DataType,
        new_struct: exp.DataType,
        root_struct: exp.DataType,
        table_name: TableName,
    ) -> t.List[TableAlterColumnOperation]:
        operations = []
        for new_pos, new_kwarg in enumerate(new_struct.expressions):
            possible_current_pos, _ = self._get_matching_kwarg(new_kwarg, current_struct, new_pos)
            if possible_current_pos is None:
                columns = parent_columns + [TableAlterColumn.from_struct_kwarg(new_kwarg)]
                operations.extend(
                    self._add_operation(
                        columns, new_pos, new_kwarg, current_struct, root_struct, table_name
                    )
                )
        return operations

    def _alter_operation(
        self,
        columns: t.List[TableAlterColumn],
        pos: int,
        struct: exp.DataType,
        new_type: exp.DataType,
        current_type: t.Union[str, exp.DataType],
        root_struct: exp.DataType,
        new_kwarg: exp.ColumnDef,
        table_name: TableName,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterColumnOperation]:
        # We don't copy on purpose here because current_type may need to be mutated inside
        # _get_operations (struct.expressions.pop and struct.expressions.insert)
        current_type = exp.DataType.build(current_type, copy=False)
        if not self.nested_support.is_none:
            if new_type.this == current_type.this == exp.DataType.Type.STRUCT:
                if self.nested_support.is_ignore:
                    return []
                if self.nested_support.is_all or not self._requires_drop_alteration(
                    current_type, new_type
                ):
                    return self._get_operations(
                        columns,
                        current_type,
                        new_type,
                        root_struct,
                        table_name,
                        ignore_destructive=ignore_destructive,
                        ignore_additive=ignore_additive,
                    )

            if new_type.this == current_type.this == exp.DataType.Type.ARRAY:
                # Some engines (i.e. Snowflake) don't support defining types on arrays
                if not new_type.expressions or not current_type.expressions:
                    return []
                new_array_type = new_type.expressions[0]
                current_array_type = current_type.expressions[0]
                if new_array_type.this == current_array_type.this == exp.DataType.Type.STRUCT:
                    if self.nested_support.is_ignore:
                        return []
                    if self.nested_support.is_all or not self._requires_drop_alteration(
                        current_array_type, new_array_type
                    ):
                        return self._get_operations(
                            columns,
                            current_array_type,
                            new_array_type,
                            root_struct,
                            table_name,
                            ignore_destructive=ignore_destructive,
                            ignore_additive=ignore_additive,
                        )
        if self._is_coerceable_type(current_type, new_type):
            return []
        if self._is_compatible_type(current_type, new_type):
            if ignore_additive:
                return []
            struct.expressions.pop(pos)
            struct.expressions.insert(pos, new_kwarg)
            return [
                TableAlterChangeColumnTypeOperation(
                    target_table=exp.to_table(table_name),
                    column_parts=columns,
                    column_type=new_type,
                    current_type=current_type,
                    expected_table_struct=root_struct.copy(),
                    array_element_selector=self.array_element_selector,
                    is_part_of_destructive_change=self.treat_alter_data_type_as_destructive,
                )
            ]
        if ignore_destructive:
            return []
        return self._drop_operation(
            columns,
            root_struct,
            pos,
            root_struct,
            table_name,
        ) + self._add_operation(
            columns,
            pos,
            new_kwarg,
            struct,
            root_struct,
            table_name,
            is_part_of_destructive_change=True,
        )

    def _resolve_alter_operations(
        self,
        parent_columns: t.List[TableAlterColumn],
        current_struct: exp.DataType,
        new_struct: exp.DataType,
        root_struct: exp.DataType,
        table_name: TableName,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterColumnOperation]:
        operations = []
        for current_pos, current_kwarg in enumerate(current_struct.expressions.copy()):
            _, new_kwarg = self._get_matching_kwarg(current_kwarg, new_struct, current_pos)
            if new_kwarg is None:
                if ignore_destructive:
                    continue
                raise ValueError("Cannot alter a column that is being dropped")
            _, new_type = _get_name_and_type(new_kwarg)
            _, current_type = _get_name_and_type(current_kwarg)
            columns = parent_columns + [TableAlterColumn.from_struct_kwarg(current_kwarg)]
            if new_type == current_type:
                continue
            operations.extend(
                self._alter_operation(
                    columns,
                    current_pos,
                    current_struct,
                    new_type,
                    current_type,
                    root_struct,
                    new_kwarg,
                    table_name,
                    ignore_destructive=ignore_destructive,
                    ignore_additive=ignore_additive,
                )
            )
        return operations

    def _get_operations(
        self,
        parent_columns: t.List[TableAlterColumn],
        current_struct: exp.DataType,
        new_struct: exp.DataType,
        root_struct: exp.DataType,
        table_name: TableName,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterColumnOperation]:
        root_struct = root_struct or current_struct
        parent_columns = parent_columns or []
        operations = []
        if not ignore_destructive:
            operations.extend(
                self._resolve_drop_operation(
                    parent_columns, current_struct, new_struct, root_struct, table_name
                )
            )
        if not ignore_additive:
            operations.extend(
                self._resolve_add_operations(
                    parent_columns, current_struct, new_struct, root_struct, table_name
                )
            )
        operations.extend(
            self._resolve_alter_operations(
                parent_columns,
                current_struct,
                new_struct,
                root_struct,
                ignore_destructive=ignore_destructive,
                ignore_additive=ignore_additive,
                table_name=table_name,
            )
        )
        return operations

    def _from_structs(
        self,
        current_struct: exp.DataType,
        new_struct: exp.DataType,
        table_name: TableName,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterColumnOperation]:
        return self._get_operations(
            [],
            current_struct,
            new_struct,
            current_struct,
            table_name=table_name,
            ignore_destructive=ignore_destructive,
            ignore_additive=ignore_additive,
        )

    def _compare_structs(
        self,
        table_name: t.Union[str, exp.Table],
        current: exp.DataType,
        new: exp.DataType,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterColumnOperation]:
        return self._from_structs(
            current,
            new,
            table_name=table_name,
            ignore_destructive=ignore_destructive,
            ignore_additive=ignore_additive,
        )

    def compare_columns(
        self,
        table_name: TableName,
        current: t.Dict[str, exp.DataType],
        new: t.Dict[str, exp.DataType],
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterColumnOperation]:
        return self._compare_structs(
            table_name,
            columns_to_types_to_struct(current),
            columns_to_types_to_struct(new),
            ignore_destructive=ignore_destructive,
            ignore_additive=ignore_additive,
        )


def has_drop_alteration(alter_operations: t.List[TableAlterOperation]) -> bool:
    return any(op.is_destructive for op in alter_operations)


def has_additive_alteration(alter_operations: t.List[TableAlterOperation]) -> bool:
    return any(op.is_additive for op in alter_operations)


def get_additive_changes(
    alter_operations: t.List[TableAlterOperation],
) -> t.List[TableAlterOperation]:
    return [x for x in alter_operations if x.is_additive]


def get_dropped_column_names(alter_expressions: t.List[TableAlterOperation]) -> t.List[str]:
    return [
        op.column.alias_or_name
        for op in alter_expressions
        if isinstance(op, TableAlterDropColumnOperation)
    ]


def get_additive_column_names(alter_expressions: t.List[TableAlterOperation]) -> t.List[str]:
    return [
        op.column.alias_or_name
        for op in alter_expressions
        if op.is_additive and isinstance(op, TableAlterColumnOperation)
    ]


def get_schema_differ(
    dialect: str, overrides: t.Optional[t.Dict[str, t.Any]] = None
) -> SchemaDiffer:
    """
    Returns the appropriate SchemaDiffer for a given dialect without initializing the engine adapter.

    Args:
        dialect: The dialect for which to get the schema differ.
        overrides: Optional dictionary of overrides to apply to the SchemaDiffer instance.

    Returns:
        The SchemaDiffer instance configured for the given dialect.
    """
    from sqlmesh.core.engine_adapter import (
        DIALECT_TO_ENGINE_ADAPTER,
        DIALECT_ALIASES,
        EngineAdapter,
    )

    dialect = dialect.lower()
    dialect = DIALECT_ALIASES.get(dialect, dialect)
    engine_adapter_class = DIALECT_TO_ENGINE_ADAPTER.get(dialect, EngineAdapter)
    return SchemaDiffer(
        **{
            **getattr(engine_adapter_class, "SCHEMA_DIFFER_KWARGS"),
            **(overrides or {}),
        }
    )


def _get_name_and_type(struct: exp.ColumnDef) -> t.Tuple[exp.Identifier, exp.DataType]:
    return struct.this, struct.args["kind"]
