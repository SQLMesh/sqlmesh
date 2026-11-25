# Ordinary-run rewrite protection prototype

This prototype records unresolved rewrites separately from successful interval
coverage. Confirmed local failures repair their exact ranges before downstream
evaluation. Uncertain writes and lost executors leave a durable hold.

It is based on v0.236.4, for discussion of
[issue #5843](https://github.com/SQLMesh/sqlmesh/issues/5843).
Integration with current `main` remains pending.

## Run the checks

Use Python 3.11 and a disposable PostgreSQL 16 cluster. The test user needs
permission to create databases. Tests retain their databases for inspection.
Do not point these checks at a shared or production cluster.

From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e . pytest time-machine psycopg2-binary
export SQLMESH_REWRITE_TEST_HOST=/path/to/private/postgres/socket
export SQLMESH_REWRITE_TEST_PORT=5432
export SQLMESH_REWRITE_TEST_USER=postgres
PYTHONPATH=. python -m pytest -q examples/rewrite_protection/test_recovery.py
```

An optional `SQLMESH_REWRITE_TEST_PASSWORD` supplies the password. Without a host,
database-dependent checks skip. The suite covers ordinary-run repair, downstream
ordering, uncertain writes, executor death, competing PostgreSQL owners, atomic
completion, retained evaluators, outer transactions, and native CLI exits.

## Supported scope

Bootstrap first, then call
`context.state_sync.initialize_rewrite_protection()` while clients are quiescent.
Initialization uses the scratch schema `sqlmesh_rewrite_prototype`, without a
release migration. There is no supported disable operation.

Protected runs require PostgreSQL state, one persistent local DuckDB warehouse,
existing deployable built-in SQL time-range tables, and `skip_janitor=True`.
Protected plans and lifecycle operations are refused. Signals, pre/post
statements, custom materializations, gateway overrides, and attached catalogs
are unsupported.

Trino execution, remote-query fencing, automatic crash adoption, and timeout
takeover are unimplemented. Raw adapter SQL, unbound evaluators, older clients,
and other state databases are outside protection. This prototype is not ready
for production.
