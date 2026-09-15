# Custom materializations guide

SQLMesh supports a variety of [model kinds](../concepts/models/model_kinds.md) that reflect the most common approaches to evaluating and materializing data transformations.

Sometimes, however, a specific use case cannot be addressed with an existing model kind. For scenarios like this, SQLMesh allows users to create their own materialization implementation using Python.

__NOTE__: this is an advanced feature and should only be considered if all other approaches have been exhausted. If you're at this decision point, we recommend you reach out to our team in the [community slack](https://tobikodata.com/community.html) before investing time building a custom materialization. If an existing model kind can solve your problem, we want to clarify the SQLMesh documentation; if an existing kind can _almost_ solve your problem, we want to consider modifying the kind so all SQLMesh users can solve the problem as well.

## Background

A SQLMesh model kind consists of methods for executing and managing the outputs of data transformations - collectively, these are the kind's "materialization."

Some materializations are relatively simple. For example, the SQL [FULL model kind](../concepts/models/model_kinds.md#full) completely replaces existing data each time it is run, so its materialization boils down to executing `CREATE OR REPLACE [table name] AS [your model query]`.

The materializations for other kinds, such as [INCREMENTAL BY TIME RANGE](../concepts/models/model_kinds.md#incremental_by_time_range), require additional logic to process the correct time intervals and replace/insert their results into an existing table.

A model kind's materialization may differ based on the SQL engine executing the model. For example, PostgreSQL does not support `CREATE OR REPLACE TABLE`, so `FULL` model kinds instead `DROP` the existing table then `CREATE` a new table. SQLMesh already contains the logic needed to materialize existing model kinds on all [supported engines](../integrations/overview.md#execution-engines).

## Overview

Custom materializations are analogous to new model kinds. Users [specify them by name](#using-custom-materializations-in-models) in a model definition's `MODEL` block, and they may accept user-specified arguments.

A custom materialization must:

- Be written in Python code
- Be a Python class that inherits the SQLMesh `CustomMaterialization` base class
- Use or override the `insert` method from the SQLMesh [`MaterializableStrategy`](https://github.com/SQLMesh/sqlmesh/blob/034476e7f64d261860fd630c3ac56d8a9c9f3e3a/sqlmesh/core/snapshot/evaluator.py#L1146) class/subclasses
- Be loaded or imported by SQLMesh at runtime

A custom materialization may:

- Use or override methods from the SQLMesh [`MaterializableStrategy`](https://github.com/SQLMesh/sqlmesh/blob/034476e7f64d261860fd630c3ac56d8a9c9f3e3a/sqlmesh/core/snapshot/evaluator.py#L1146) class/subclasses
- Use or override methods from the SQLMesh [`EngineAdapter`](https://github.com/SQLMesh/sqlmesh/blob/034476e7f64d261860fd630c3ac56d8a9c9f3e3a/sqlmesh/core/engine_adapter/base.py#L67) class/subclasses
- Execute arbitrary SQL code and fetch results with the engine adapter `execute` and related methods

A custom materialization may perform arbitrary Python processing with Pandas or other libraries, but in most cases that logic should reside in a [Python model](../concepts/models/python_models.md) instead of the materialization.

A SQLMesh project will automatically load any custom materializations present in its `materializations/` directory. Alternatively, the materialization may be bundled into a [Python package](#python-packaging) and installed with standard methods.

## Creating a custom materialization

Create a new custom materialization by adding a `.py` file containing the implementation to the `materializations/` folder in the project directory. SQLMesh will automatically import all Python modules in this folder at project load time and register the custom materializations. (Find more information about sharing and packaging custom materializations [below](#sharing-custom-materializations).)

A custom materialization must be a class that inherits the `CustomMaterialization` base class and provides an implementation for the `insert` method.

For example, a minimal full-refresh custom materialization might look like the following:

```python linenums="1"
from sqlmesh import CustomMaterialization # required

# argument typing: strongly recommended but optional best practice
from __future__ import annotations
from sqlmesh import Model
import typing as t
if t.TYPE_CHECKING:
    from sqlmesh import QueryOrDF

class CustomFullMaterialization(CustomMaterialization):
    NAME = "my_custom_full"

    def insert(
        self,
        table_name: str, # ": str" is optional argument typing
        query_or_df: QueryOrDF,
        model: Model,
        is_first_insert: bool,
        render_kwargs: t.Dict[str, t.Any],
        **kwargs: t.Any,
    ) -> None:
        self.adapter.replace_query(table_name, query_or_df)

```

Let's unpack this materialization:

* `NAME` - name of the custom materialization. This name is used to specify the materialization in a model definition `MODEL` block. If not specified in the custom materialization, the name of the class is used in the `MODEL` block instead.
* The `insert` method has the following arguments:
    * `table_name` - the name of a target table or view into which the data should be inserted
    * `query_or_df` - a query (of SQLGlot expression type) or DataFrame (Pandas, PySpark, or Snowpark) instance to be inserted
    * `model` - the model definition object used to access model parameters and user-specified materialization arguments
    * `is_first_insert` - whether this is the first insert for the current version of the model (used with batched or multi-step inserts)
    * `render_kwargs` - a dictionary of arguments used to render the model query
    * `kwargs` - additional and future arguments
* The `self.adapter` instance is used to interact with the target engine. It comes with a set of useful high-level APIs like `replace_query`, `columns`, and `table_exists`, but also supports executing arbitrary SQL expressions with its `execute` method.

You can control how data objects (tables, views, etc.) are created and deleted by overriding the `MaterializableStrategy` class's `create` and `delete` methods:

```python linenums="1"
from sqlmesh import CustomMaterialization # required

# argument typing: strongly recommended but optional best practice
from __future__ import annotations
from sqlmesh import Model
import typing as t

class CustomFullMaterialization(CustomMaterialization):
    # NAME and `insert` method code here
    ...

    def create(
        self,
        table_name: str,
        model: Model,
        is_table_deployable: bool,
        render_kwargs: t.Dict[str, t.Any],
        **kwargs: t.Any,
    ) -> None:
        # Custom table/view creation logic.
        # Likely uses `self.adapter` methods like `create_table`, `create_view`, or `ctas`.

    def delete(self, name: str, **kwargs: t.Any) -> None:
        # Custom table/view deletion logic.
        # Likely uses `self.adapter` methods like `drop_table` or `drop_view`.
```

## Using a custom materialization

Specify the model kind `CUSTOM` in a model definition `MODEL` block to use the custom materialization. Specify the `NAME` from the custom materialization code in the `materialization` attribute of the `CUSTOM` kind:

```sql linenums="1"
MODEL (
  name my_db.my_model,
  kind CUSTOM (
      materialization 'my_custom_full'
  )
);
```

A custom materialization may accept arguments specified in an array of key-value pairs in the `CUSTOM` kind's `materialization_properties` attribute:

```sql linenums="1" hl_lines="5-7"
MODEL (
  name my_db.my_model,
  kind CUSTOM (
    materialization 'my_custom_full',
    materialization_properties (
      'config_key' = 'config_value'
    )
  )
);
```

The custom materialization implementation accesses the `materialization_properties` via the `model` object's `custom_materialization_properties` dictionary:

```python linenums="1" hl_lines="12"
class CustomFullMaterialization(CustomMaterialization):
    NAME = "my_custom_full"

    def insert(
        self,
        table_name: str,
        query_or_df: QueryOrDF,
        model: Model,
        is_first_insert: bool,
        render_kwargs: t.Dict[str, t.Any],
        **kwargs: t.Any,
    ) -> None:
        config_value = model.custom_materialization_properties["config_key"]
        # Proceed with implementing the insertion logic.
        # Example existing materialization for look and feel: https://github.com/SQLMesh/sqlmesh/blob/main/sqlmesh/core/snapshot/evaluator.py
```

## Extending `CustomKind`

!!! warning
    This is even lower level usage that contains a bunch of extra complexity and relies on knowledge of the SQLMesh internals.
    If you dont need this level of complexity, stick with the method described above.

In many cases, the above usage of a custom materialization will suffice.

However, you may still want tighter integration with SQLMesh's internals:

- You may want to validate custom properties are correct before any database connections are made
- You may want to leverage existing functionality of SQLMesh that relies on specific properties being present

In this case, you can provide a subclass of `CustomKind` for SQLMesh to use instead of `CustomKind` itself.
During project load, SQLMesh will instantiate your *subclass* instead of `CustomKind`.

This allows you to run custom validators at load time rather than having to perform extra validation when `insert()` is invoked on your `CustomMaterialization`.

You can also define standard Python `@property` methods to "hoist" properties declared inside `materialization_properties` to the top level on your `Kind` object. This can make using them from within your custom materialization easier.

To extend `CustomKind`, first you define a subclass like so:

```python linenums="1" hl_lines="7"
from typing_extensions import Self
from pydantic import field_validator, ValidationInfo
from sqlmesh import CustomKind
from sqlmesh.utils.pydantic import list_of_fields_validator
from sqlmesh.utils.errors import ConfigError

class MyCustomKind(CustomKind):

    _primary_key: t.List[exp.Expression]

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        self._primary_key = list_of_fields_validator(
            self.materialization_properties.get("primary_key"),
            { "dialect": self.dialect }
        )
        if not self.primary_key:
            raise ConfigError("primary_key must be specified")
        return self

    @property
    def primary_key(self) -> t.List[exp.Expression]:
        return self._primary_key

```

To use it within a model, we can do something like:

```sql linenums="1" hl_lines="4"
MODEL (
  name my_db.my_model,
  kind CUSTOM (
    materialization 'my_custom_full',
    materialization_properties (
        primary_key = (col1, col2)
    )
  )
);
```

To indicate to SQLMesh that it should use the `MyCustomKind` subclass instead of `CustomKind`, specify it as a generic type parameter on your custom materialization class like so:

```python linenums="1" hl_lines="1 16"
class CustomFullMaterialization(CustomMaterialization[MyCustomKind]):
    NAME = "my_custom_full"

    def insert(
        self,
        table_name: str,
        query_or_df: QueryOrDF,
        model: Model,
        is_first_insert: bool,
        render_kwargs: t.Dict[str, t.Any],
        **kwargs: t.Any,
    ) -> None:
        assert isinstance(model.kind, MyCustomKind)

        self.adapter.merge(
            ...,
            unique_key=model.kind.primary_key
        )
```

When SQLMesh loads your custom materialization, it will inspect the Python type signature for generic parameters that are subclasses of `CustomKind`. If it finds one, it will instantiate your subclass when building `model.kind` instead of using the default `CustomKind` class.

In this example, this means that:

- Validation for `primary_key` happens at load time instead of evaluation time. So if there is an issue, you can abort early rather than halfway through applying a plan.
- When your custom materialization is called to load data into tables, `model.kind` will resolve to your custom kind object so you can access the extra properties you defined without first needing to validate them / coerce them to a usable type.


## Sharing custom materializations

### Copying files

The simplest (but least robust) way to use a custom materialization in multiple SQLMesh projects is for each project to place a copy of the materialization's Python code in its `materializations/` directory.

If you use this approach, we strongly recommend storing the materialization code in a version-controlled repository and creating a reliable method of notifying users when it is updated.

This approach may be appropriate for smaller organizations, but it is not robust.

### Python packaging

A more complex (but robust) way to use a custom materialization in multiple SQLMesh projects is to create and publish a Python package containing the implementation.

One scenario that requires Python packaging is when a SQLMesh project uses Airflow or other external schedulers, and the scheduler cluster does not have the `materializations/` folder available. The cluster will use standard Python package installation methods to import the custom materialization.

Package and expose custom materializations with the [setuptools entrypoints](https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/#using-package-metadata) mechanism. Once the package is installed, SQLMesh will automatically load custom materializations from the entrypoint list.

For example, if your custom materialization class is defined in the `my_package/my_materialization.py` module, you can expose it as an entrypoint in the `pyproject.toml` file as follows:

```toml
[project.entry-points."sqlmesh.materializations"]
my_materialization = "my_package.my_materialization:CustomFullMaterialization"
```

Or in `setup.py`:

```python
setup(
    ...,
    entry_points={
        "sqlmesh.materializations": [
            "my_materialization = my_package.my_materialization:CustomFullMaterialization",
        ],
    },
)
```

Refer to the SQLMesh Github [custom_materializations](https://github.com/SQLMesh/sqlmesh/tree/main/examples/custom_materializations) example for more details on Python packaging.
