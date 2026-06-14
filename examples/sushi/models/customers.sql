MODEL (
  name sushi.customers,
  kind FULL,
  owner jen,
  cron '@daily',
  tags (pii, fact),
  grain customer_id,
  description 'Sushi customer data',
  column_descriptions (
    customer_id = 'customer_id uniquely identifies customers'
  )
);

CREATE SCHEMA IF NOT EXISTS raw;
DROP VIEW IF EXISTS raw.demographics;
CREATE VIEW raw.demographics AS (
  SELECT 1 AS customer_id, '00000' AS zip
);

WITH current_marketing_outer AS (
  SELECT
    customer_id,
    status
  FROM sushi.marketing
  WHERE valid_to is null
)
SELECT DISTINCT
  o.customer_id::INT AS customer_id, -- this comment should not be registered
  m.status,
  d.zip
  FROM sushi.orders AS o
LEFT JOIN (
  WITH current_marketing AS (
    SELECT
      customer_id,
      status,
      @ADD_ONE(1) AS another_column,
    FROM current_marketing_outer
  )
  SELECT current_marketing.* FROM current_marketing WHERE current_marketing.customer_id != 100
) AS m
  ON o.customer_id = m.customer_id
LEFT JOIN raw.demographics AS d
  ON o.customer_id = d.customer_id
WHERE o.customer_id > 0