# Plan history guide

Sometimes something looks off about how SQLMesh behaved during a plan: a model materialized with unexpected data, a step seemed to hang, or a backfill took far longer than expected. Answering "what did SQLMesh actually run, and did it succeed?" usually means context-switching into the warehouse's own query history UI and manually correlating queries by time.

The `sqlmesh history` command does that correlation for you. Give it a plan id and it prints every SQL statement SQLMesh executed for that plan, in order, along with each statement's status, duration, and rows/bytes processed &mdash; or export the SQL to a file for offline inspection.

`history` is read-only. It doesn't touch SQLMesh state or your data; it only reads the query engine's own job/query history for the queries SQLMesh ran.

!!! note
    `sqlmesh history` currently supports **BigQuery only**. Running it against a project configured with another engine prints a message that the engine isn't yet supported.

## How it works

Every job SQLMesh runs for a plan &mdash; both query jobs and the load jobs used to ingest Python-model dataframes and seeds &mdash; is tagged with that plan's correlation id as a BigQuery job label. `sqlmesh history` looks up the plan (either the one you specify or one you pick from a menu), then queries BigQuery's job history for every job carrying that label and renders the results chronologically.

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

History · plan 3f9a2c1e · prod · 9 queries · 7 ✓  1 ✗  1 running

Time      Status  Operation      Target                            Duration   Bytes/Rows
────────  ──────  ─────────────  ────────────────────────────────  ────────   ───────────────
14:03:11  ✓       CREATE TABLE   sqlmesh__sushi.orders__2837a1c     1200 ms                  -
14:03:14  ✓       INSERT         sqlmesh__sushi.orders__2837a1c     14800 ms   8,912,441 bytes
14:03:22  ✓       LOAD           sqlmesh__sushi.customers__4b90fc   640 ms        12,004 bytes
14:03:29  ✗       INSERT         sqlmesh__sushi.items__9f01b2d      420 ms                   -
                  └─ Column not found: name PRICE cannot be resolved in sushi.items [at 1:34]
14:03:31  …       MERGE          sqlmesh__sushi.order_items__7c3a   -                        -

1 failed · re-run with `-o failures.sql` to export the SQL.
```

(In your terminal this is rendered as a bordered table.) The header line summarizes the plan: the short plan id, environment, total job count, and a breakdown of how many succeeded (`✓`), failed (`✗`), or are still running (`…`).

- **Time** &mdash; when the job started.
- **Status** &mdash; `✓` succeeded, `✗` failed, `…` still running.
- **Operation** &mdash; the leading SQL keyword (`CREATE TABLE`, `INSERT`, `MERGE`, `CREATE VIEW`, `ALTER TABLE`, `LOAD`, etc.), so you can scan the kind of work each job did without reading the full statement.
- **Target** &mdash; the physical table the job wrote to, so you can tell which model (and which step) each row belongs to. This is how you follow a Python model whose steps are interleaved with SQL models, or attribute a `LOAD`/`INSERT`/`MERGE` to the right table. Shown as `-` for jobs with no destination (for example a standalone audit `SELECT`).
- **Duration** &mdash; wall-clock time the job took. Shown as `-` for jobs that haven't finished.
- **Bytes/Rows** &mdash; bytes processed if BigQuery reported them, otherwise rows affected, otherwise `-`.

A failed job is followed by an indented line with the error message BigQuery returned, so you can see why it failed without opening the warehouse UI.

Because the rows are ordered by execution time, the steps of a **Python model** (its physical `CREATE TABLE`, the `LOAD` of its dataframe, and the `INSERT`/`MERGE` that publishes it) slot chronologically in between the SQL models' steps, and **hooks** &mdash; model pre/post-statements and environment `before_all`/`after_all` statements &mdash; appear as their own rows in position. Use the **Target** column to tell them apart.

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
- **Signals and pure-Python logic aren't shown.** Signals are Python readiness checks that gate whether a model runs; they execute no SQL, so they don't appear as rows. Their effect shows up as an *absence* &mdash; a signal-blocked model simply has no `INSERT` for that interval. To inspect what a signal is holding back, use [`sqlmesh check_intervals`](../reference/cli.md#check_intervals), which reports pending intervals while respecting signals. Likewise, any purely in-memory work inside a Python model (API calls, dataframe transforms) runs no SQL and won't appear &mdash; only the jobs that touch the warehouse do.
- **Plans only.** Scheduled `sqlmesh run` executions aren't included, only `sqlmesh plan` applications.

## See also

The [CLI reference](../reference/cli.md#history) has the full list of options.
