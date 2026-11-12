-- SPDX-License-Identifier: Apache-2.0
MODEL (
  name e2e.events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column ds,
    batch_size 1
  ),
  start '2024-01-01',
  cron '@daily',
  audits (not_null(columns := (id)))
);

SELECT CAST(@start_ds AS DATE) AS ds, 1 AS id
