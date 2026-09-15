# Python API

SQLMesh is built in Python, and its complete Python API reference is located [here](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh.html).

The Python API reference is comprehensive and includes the internal components of SQLMesh. Those components are likely only of interest if you want to modify SQLMesh itself.

If you want to use SQLMesh via its Python API, the best approach is to study how the SQLMesh [CLI](./cli.md) calls it behind the scenes. The CLI implementation code shows exactly which Python methods are called for each CLI command and can be viewed [on Github](https://github.com/SQLMesh/sqlmesh/blob/main/sqlmesh/cli/main.py). For example, the Python code executed by the `plan` command is located [here](https://github.com/SQLMesh/sqlmesh/blob/15c8788100fa1cfb8b0cc1879ccd1ad21dc3e679/sqlmesh/cli/main.py#L302).

Almost all the relevant Python methods are in the [SQLMesh `Context` class](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/context.html#Context).
