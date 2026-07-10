# Plan history guide

Sometimes something looks off about how SQLMesh behaved during a plan: a model materialized with unexpected data, a step seemed to hang, or a backfill took far longer than expected. Answering "what did SQLMesh actually run, and did it succeed?" usually means context-switching into the warehouse's own query history UI and manually correlating queries by time.

The `sqlmesh history` command does that correlation for you. Give it a plan id and it prints every SQL statement SQLMesh executed for that plan, in order, along with each statement's status, duration, and rows/bytes processed &mdash; or export the SQL to a file for offline inspection.

`history` is read-only. It doesn't touch SQLMesh state or your data; it only reads the query engine's own job/query history for the queries SQLMesh ran.

!!! note
    `sqlmesh history` currently supports **BigQuery only**. Running it against a project configured with another engine prints a message that the engine isn't yet supported.

## How it works

Every query SQLMesh runs for a plan is tagged with that plan's correlation id &mdash; a BigQuery job label on the query job itself. `sqlmesh history` looks up the plan (either the one you specify or one you pick from a menu), then queries BigQuery's job history for every job carrying that label and renders the results chronologically.

Because it's driven by the query engine's job history rather than SQLMesh state, it only shows what actually executed against the warehouse. It's the fastest way to answer "did this step run, how long did it take, and if it failed, why?" without leaving your terminal.

## Selecting a plan

### Interactively

Run `sqlmesh history` with no arguments to get a numbered menu of recent plans, most recent first:

```bash linenums="1"
$ sqlmesh history

Select a plan to inspect:
[1] prod  plan 3f9a2c1e  applied 2026-07-10 02:03PM  (42 models)
[2] prod  plan a17b90ff  applied 2026-07-09 09:12AM  (42 models)
[3] dev_sung  plan 9c02d5e1  applied 2026-07-10 01:50PM  (3 models)
```

Each row shows the environment, the first 8 characters of the plan id, when the plan was applied (or `in progress` if it hasn't finished), and the number of models it touched. The menu is built from SQLMesh state &mdash; the current and previous plan for each environment. Type a number to inspect that plan's history.

Use `--environment`/`--env` to restrict the menu to a single environment, which is useful when a lot of plans have been applied recently:

```bash linenums="1"
$ sqlmesh history --env prod
```

### By plan id

If you already know the plan id (for example, from `sqlmesh plan` output or the interactive menu), pass it directly to skip the menu:

```bash linenums="1"
$ sqlmesh history 3f9a2c1e
```

You only need to type enough of the plan id to uniquely identify it &mdash; the 8-character short id shown in the menu is usually enough.

## Reading the output

`sqlmesh history <plan_id>` prints a table with one row per query, in the order SQLMesh ran them:

```bash linenums="1"
$ sqlmesh history 3f9a2c1e

                    History · plan 3f9a2c1e · 41 queries · 38 ✓  2 ✗  1 running
┏━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Time     ┃ Status ┃ Operation      ┃ Duration ┃   Bytes/Rows ┃
┡━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ 14:03:11 │ ✓      │ CREATE TABLE   │ 1200 ms  │  1,048,576 bytes │
│ 14:03:14 │ ✓      │ INSERT         │ 14800 ms │  8,912,441 bytes │
│ 14:03:29 │ ✗      │ INSERT         │ 420 ms   │            - │
│          │        │ Syntax error: unexpected identifier 'PRICE' at [1:34] │       │              │
│ 14:04:02 │ …      │ MERGE          │ -        │            - │
└──────────┴────────┴────────────────┴──────────┴──────────────┘
2 failed · re-run with `-o failures.sql` to export the SQL.
```

The header line summarizes the plan: the short plan id, total query count, and a breakdown of how many queries succeeded (`✓`), failed (`✗`), or are still running (`…`).

- **Time** &mdash; when the query started.
- **Status** &mdash; `✓` succeeded, `✗` failed, `…` still running.
- **Operation** &mdash; the leading SQL keyword (`CREATE TABLE`, `INSERT`, `MERGE`, `CREATE VIEW`, `ALTER TABLE`, etc.), so you can scan for the kind of work each query did without reading the full statement.
- **Duration** &mdash; wall-clock time the query took. Shown as `-` for queries that haven't finished.
- **Bytes/Rows** &mdash; bytes processed if BigQuery reported them, otherwise rows affected, otherwise `-`.

A failed query is followed by an indented line with the error message BigQuery returned, so you can see why it failed without opening the warehouse UI.

## Exporting the SQL

Pass `-o`/`--output-file` to write the executed SQL to a file instead of printing the table. This is handy for replaying a failed step locally, diffing what ran between two plans, or attaching the SQL to a bug report:

```bash linenums="1"
$ sqlmesh history 3f9a2c1e -o plan_3f9a2c1e.sql
```

Each query becomes one entry, preceded by a header comment with the timestamp, status, duration, and (for failures) the error:

```sql linenums="1"
-- [2026-07-10T14:03:29] FAILED (420 ms) error: Syntax error: unexpected identifier 'PRICE' at [1:34]
INSERT INTO sqlmesh__sushi.items__9f01 SELECT ...;
```

The file is one statement per entry, in execution order, so it can be read top to bottom or replayed a statement at a time.

## Limitations

- **BigQuery only.** Other engines aren't supported yet; `history` prints a clear message rather than partial or incorrect results.
- **Requires `bigquery.jobs.listAll`.** Reading every job in the project (not just your own) needs this permission. If it's missing, `history` fails with:

    ```
    Error: Permission denied reading BigQuery job history for project 'acme-analytics'. Reading all jobs in the project requires the 'bigquery.jobs.listAll' permission.

    To grant access, run:
      gcloud projects add-iam-policy-binding acme-analytics --member='user:YOUR_EMAIL' --role='roles/bigquery.resourceViewer'

    Docs: https://cloud.google.com/bigquery/docs/information-schema-jobs#required_permissions
    ```

- **Ingestion latency and retention.** BigQuery's job history views have some delay before a query appears and don't retain jobs forever, so a plan applied moments ago or a very old plan may show no rows.
- **Labeled query jobs only.** SQLMesh labels the query jobs it runs, but some operations &mdash; like loading a seed model's dataframe into BigQuery &mdash; use a load job rather than a labeled query job, so they won't appear in the history.
- **Plans only.** Scheduled `sqlmesh run` executions aren't included, only `sqlmesh plan` applications.

## See also

The [CLI reference](../reference/cli.md#history) has the full list of options.
