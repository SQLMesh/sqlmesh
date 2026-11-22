import os
import pytest

from sqlmesh import Context
from sqlmesh.core.model import schema
import concurrent.futures


# Run each fork test in a fresh pytest process to avoid inheriting native process state from
# earlier tests.
pytestmark = [pytest.mark.isolated, pytest.mark.fork_isolated]


def test_parallel_load(assert_exp_eq, mocker):
    mocker.patch("sqlmesh.core.constants.MAX_FORK_WORKERS", 2)

    spy_update_schemas = mocker.spy(schema, "_update_model_schemas")
    process_pool_executor = mocker.spy(concurrent.futures.ProcessPoolExecutor, "__init__")
    as_completed = mocker.spy(concurrent.futures, "as_completed")

    context = Context(paths="examples/sushi")

    if hasattr(os, "fork"):
        process_pool_executor.assert_called()
        as_completed.assert_called()
        executor_args = process_pool_executor.call_args
        assert executor_args[1]["max_workers"] == 2

    assert len(context.models) == 20
    spy_update_schemas.assert_called()
    assert_exp_eq(
        context.render("sushi.customers"),
        """
WITH "current_marketing_outer" AS (
  SELECT
    "marketing"."customer_id" AS "customer_id",
    "marketing"."status" AS "status"
  FROM "memory"."sushi"."marketing" AS "marketing"
  WHERE
    "marketing"."valid_to" IS NULL
)
SELECT DISTINCT
  CAST("o"."customer_id" AS INT) AS "customer_id", /* this comment should not be registered */
  "m"."status" AS "status",
  "d"."zip" AS "zip"
FROM "memory"."sushi"."orders" AS "o"
LEFT JOIN (
  WITH "current_marketing" AS (
    SELECT
      "current_marketing_outer"."customer_id" AS "customer_id",
      "current_marketing_outer"."status" AS "status",
      2 AS "another_column"
    FROM "current_marketing_outer" AS "current_marketing_outer"
  )
  SELECT
    "current_marketing"."customer_id" AS "customer_id",
    "current_marketing"."status" AS "status",
    "current_marketing"."another_column" AS "another_column"
  FROM "current_marketing" AS "current_marketing"
  WHERE
  "current_marketing"."customer_id" <> 100
) AS "m"
  ON "m"."customer_id" = "o"."customer_id"
LEFT JOIN "memory"."raw"."demographics" AS "d"
  ON "d"."customer_id" = "o"."customer_id"
  WHERE
  "o"."customer_id" > 0
        """,
    )

    context.plan(no_prompts=True, auto_apply=True)


def test_parallel_load_multi_repo(assert_exp_eq, mocker):
    mocker.patch("sqlmesh.core.constants.MAX_FORK_WORKERS", 2)

    process_pool_executor = mocker.spy(concurrent.futures.ProcessPoolExecutor, "__init__")
    context = Context(paths=["examples/multi/repo_1", "examples/multi/repo_2"], gateway="memory")

    if hasattr(os, "fork"):
        executor_args = process_pool_executor.call_args
        assert executor_args[1]["max_workers"] == 2
    assert len(context.models) == 5

    assert_exp_eq(
        context.render("memory.bronze.a"),
        'SELECT 1 AS "col_a", \'b\' AS "col_b", 1 AS "one", \'repo_1\' AS "dup"',
    )

    context.plan(no_prompts=True, auto_apply=True)
