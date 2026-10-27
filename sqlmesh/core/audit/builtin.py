from __future__ import annotations

import inspect
import sys

from sqlglot import exp

from sqlmesh.core.audit.definition import Audit, ModelAudit


def create_non_blocking_copy(audit: Audit) -> Audit:
    return audit.copy(update={"name": f"{audit.name}_non_blocking", "blocking": False})


# not_null(columns=(column_1, column_2))
not_null_audit = ModelAudit(
    name="not_null",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(
  @REDUCE(
    @EACH(
      @columns,
      c -> c IS NULL
    ),
    (l, r) -> l OR r
  ),
  @condition,
)
    """,
)

# unique_values(columns=(column_1, column_2))
unique_values_audit = ModelAudit(
    name="unique_values",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM (
  SELECT
    @EACH(
      @columns,
      c -> row_number() OVER (PARTITION BY c ORDER BY c) AS rank_@c
    )
  FROM @this_model
  WHERE @condition
)
WHERE @REDUCE(
  @EACH(
    @columns,
    c -> rank_@c > 1
  ),
  (l, r) -> l OR r
)
    """,
)

# accepted_values(column=column_name, is_in=(1, 2, 3))
accepted_values_audit = ModelAudit(
    name="accepted_values",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(@column NOT IN @is_in, @condition)
""",
)

# number_of_rows(threshold=100)
number_of_rows_audit = ModelAudit(
    name="number_of_rows",
    defaults={"condition": exp.true()},
    query="""
SELECT COUNT(*)
FROM (
  SELECT 1
  FROM @this_model
  WHERE @condition
  LIMIT @threshold + 1
)
HAVING COUNT(*) <= @threshold
    """,
)

# forall(criteria=(
#   column_1 > 0,
#   column_2 in (1, 2, 3),
#   column_3 is not null
# ))
forall_audit = ModelAudit(
    name="forall",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE
  @AND(
    @REDUCE(
      @EACH(
        @criteria,
        c -> NOT (c)
      ),
      (l, r) -> l OR r
    ),
    @condition,
  )
    """,
)

# accepted_range(column=age, min_v=0, max_v=100)
# accepted_range(column=age, min_v=10)
# accepted_range(column=age, max_v=50)
accepted_range_audit = ModelAudit(
    name="accepted_range",
    defaults={
        "min_v": exp.null(),
        "max_v": exp.null(),
        "inclusive": exp.true(),
        "condition": exp.true(),
    },
    query="""
SELECT *
FROM @this_model
WHERE
  @AND(
    @OR(
      @IF(@min_v IS NOT NULL AND @inclusive, @column < @min_v, NULL),
      @IF(@min_v IS NOT NULL AND NOT @inclusive, @column <= @min_v, NULL),
      @IF(@max_v IS NOT NULL AND @inclusive, @column > @max_v, NULL),
      @IF(@max_v IS NOT NULL AND NOT @inclusive, @column >= @max_v, NULL),
    ),
    @condition,
  )
    """,
)

# at_least_one(column=column_name)
at_least_one_audit = ModelAudit(
    name="at_least_one",
    defaults={"condition": exp.true()},
    query="""
SELECT 1
FROM @this_model
WHERE @condition
GROUP BY 1
HAVING COUNT(@column) = 0
    """,
)

# not_constant(column=column_name)
not_constant_audit = ModelAudit(
    name="not_constant",
    defaults={"condition": exp.true()},
    query="""
SELECT 1
FROM (
  SELECT COUNT(DISTINCT @column) AS t_cardinality
  FROM @this_model
  WHERE @condition
) AS r
WHERE r.t_cardinality <= 1
    """,
)

# not_empty_string(column=column_name)
not_empty_string_audit = ModelAudit(
    name="not_empty_string",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(@column = '', @condition)
    """,
)

# not_null_proportion(column=column_name, threshold=0.9)
not_null_proportion_audit = ModelAudit(
    name="not_null_proportion",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM (
  SELECT
    count(*) as cnt_tot,
    count(@column) as cnt_not_null,
    count(*) - count(@column) as cnt_null
  FROM @this_model
  WHERE @condition
) AS s
WHERE s.cnt_not_null <= s.cnt_tot * @threshold
    """,
)

# not_accepted_values(column=column_name, is_in=(1, 2, 3))
not_accepted_values_audit = ModelAudit(
    name="not_accepted_values",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(@column IN @is_in, @condition)
""",
)

# sequential_values(column=column_name, interval=1)
# TODO: support grouping
sequential_values_audit = ModelAudit(
    name="sequential_values",
    defaults={"interval": exp.Literal.number(1), "condition": exp.true()},
    query="""
WITH windowed AS (
  SELECT
    @column,
    LAG(@column) OVER (
      ORDER BY @column
    ) AS prv
  FROM @this_model
  WHERE @condition
), validation_errors AS (
    SELECT *
    FROM windowed
    WHERE NOT (@column = prv + @interval)
)

SELECT *
FROM validation_errors
    """,
)

# unique_combination_of_columns(columns=(column_1, column_2))
unique_combination_of_columns_audit = ModelAudit(
    name="unique_combination_of_columns",
    defaults={"condition": exp.true()},
    query="""
SELECT @EACH(@columns, c -> c)
FROM @this_model
WHERE @condition
GROUP BY @EACH(@columns, c -> c)
HAVING COUNT(*) > 1
    """,
)

# mutually_exclusive_ranges(lower_bound_column=date_from, upper_bound_column=date_to)
# TODO: make inclusivity configurable
mutually_exclusive_ranges_audit = ModelAudit(
    name="mutually_exclusive_ranges",
    defaults={"partition_clause": exp.false(), "condition": exp.true()},
    query="""
WITH window_functions AS (
  SELECT
    @lower_bound_column AS lower_bound,
    @upper_bound_column AS upper_bound,
    LEAD(@lower_bound_column) OVER (
      @if(@partition_clause, @partition_clause)
      ORDER BY @lower_bound_column, @upper_bound_column
    ) AS next_lower_bound,
    row_number() OVER (
      @if(@partition_clause, @partition_clause)
      ORDER BY @lower_bound_column desc, @upper_bound_column desc
    ) = 1 AS is_last_record
  FROM @this_model
  WHERE @condition
), calc AS (
  SELECT
    *,
    COALESCE(
      lower_bound <= upper_bound,
      False
    ) AS lower_bound_lte_upper_bound,
    COALESCE(
      upper_bound <= next_lower_bound,
      is_last_record,
      False
    ) AS upper_bound_lte_next_lower_bound
  FROM window_functions
), validation_errors AS (
  SELECT *
  FROM calc
  WHERE NOT (
    lower_bound_lte_upper_bound
    AND upper_bound_lte_next_lower_bound
  )
)

SELECT *
FROM validation_errors
    """,
)

# valid_uuid(column=column_name)
valid_uuid_audit = ModelAudit(
    name="valid_uuid",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(
  NOT REGEXP_LIKE(LOWER(@column), '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
  @condition,
)

    """,
)

# valid_url(column=column_name)
valid_url_audit = ModelAudit(
    name="valid_url",
    defaults={"condition": exp.true()},
    query=r"""
SELECT *
FROM @this_model
WHERE @AND(NOT REGEXP_LIKE(@column, '^(https?|ftp)://[^\s/$.?#].[^\s]*$'), @condition)
    """,
)

# valid_http_method(column=column_name)
valid_http_method_audit = ModelAudit(
    name="valid_http_method",
    query="""
SELECT *
FROM @this_model
WHERE NOT @column IN ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'TRACE', 'CONNECT')
    """,
)

# valid_email(column=column_name)
valid_email_audit = ModelAudit(
    name="valid_email",
    defaults={"condition": exp.true()},
    query=r"""
SELECT *
FROM @this_model
WHERE @AND(NOT REGEXP_LIKE(@column, '^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'), @condition)
    """,
)

# match_regex_pattern_list(column=column_name, patterns=('^pattern_1', 'pattern_2$'))
match_regex_pattern_list_audit = ModelAudit(
    name="match_regex_pattern_list",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(
  @REDUCE(
    @EACH(
      @patterns,
      c -> NOT REGEXP_LIKE(@column, c)
    ),
    (l, r) -> l AND r
  ),
  @condition,
)
    """,
)

# not_match_regex_pattern_list(column=column_name, patterns=('^pattern_1', 'pattern_2$'))
not_match_regex_pattern_list_audit = ModelAudit(
    name="not_match_regex_pattern_list",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(
  @REDUCE(
    @EACH(
      @patterns,
      c -> REGEXP_LIKE(@column, c)
    ),
    (l, r) -> l OR r
  ),
  @condition,
)
    """,
)

# match_like_pattern_list(column=column_name, patterns=('%pattern_1%', 'pattern_2%'))
match_like_pattern_list = ModelAudit(
    name="match_like_pattern_list",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(
  @REDUCE(
    @EACH(
      @patterns,
      c -> NOT @column LIKE c
    ),
    (l, r) -> l AND r
  ),
  @condition,
)
    """,
)

# not_match_like_pattern_list(column=column_name, patterns=('%pattern_1%', 'pattern_2%'))
not_match_like_pattern_list_audit = ModelAudit(
    name="not_match_like_pattern_list",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(
  @REDUCE(
    @EACH(
      @patterns,
      c -> @column LIKE c
    ),
    (l, r) -> l OR r
  ),
  @condition,
)
    """,
)

# z_score(column=column_name, threshold=3)
z_score_audit = ModelAudit(
    name="z_score",
    defaults={"condition": exp.true()},
    query="""
WITH stats AS (
  SELECT
    AVG(@column) AS mean_@column,
    STDDEV(@column) AS stddev_@column
  FROM @this_model
  WHERE @condition
)
SELECT
  @column,
  (@column - mean_@column) / NULLIF(stddev_@column, 0) AS z_score
FROM @this_model, stats
WHERE ABS((@column - mean_@column) / NULLIF(stddev_@column, 0)) > @threshold
    """,
)

# string_length_between(column=column_name, max_v=22)
string_length_between_audit = ModelAudit(
    name="string_length_between",
    defaults={
        "min_v": exp.null(),
        "max_v": exp.null(),
        "inclusive": exp.true(),
        "condition": exp.true(),
    },
    query="""
SELECT *
FROM @this_model
WHERE
  @AND(
    @OR(
      @IF(@min_v IS NOT NULL AND @inclusive, LENGTH(@column) < @min_v, NULL),
      @IF(@min_v IS NOT NULL AND NOT @inclusive, LENGTH(@column) <= @min_v, NULL),
      @IF(@max_v IS NOT NULL AND @inclusive, LENGTH(@column) > @max_v, NULL),
      @IF(@max_v IS NOT NULL AND NOT @inclusive, LENGTH(@column) >= @max_v, NULL),
    ),
    @condition,
  )
    """,
)

# string_length_equal(column=column_name, v=22)
string_length_equal_audit = ModelAudit(
    name="string_length_equal",
    defaults={"condition": exp.true()},
    query="""
SELECT *
FROM @this_model
WHERE @AND(LENGTH(@column) != @v, @condition)
    """,
)

# stddev_in_range(column=age, min_v=2.5, max_v=25)
stddev_in_range_audit = ModelAudit(
    name="stddev_in_range",
    defaults={
        "min_v": exp.null(),
        "max_v": exp.null(),
        "inclusive": exp.true(),
        "condition": exp.true(),
    },
    query="""
SELECT *
FROM (
  SELECT STDDEV(@column) AS stddev_@column
  FROM @this_model
  WHERE @condition
)
WHERE
  @OR(
    @IF(@min_v IS NOT NULL AND @inclusive, stddev_@column < @min_v, NULL),
    @IF(@min_v IS NOT NULL AND NOT @inclusive, stddev_@column <= @min_v, NULL),
    @IF(@max_v IS NOT NULL AND @inclusive, stddev_@column > @max_v, NULL),
    @IF(@max_v IS NOT NULL AND NOT @inclusive, stddev_@column >= @max_v, NULL),
  )
    """,
)

# mean_in_range(column=age, min_v=2.5, max_v=25)
mean_in_range_audit = ModelAudit(
    name="mean_in_range",
    defaults={
        "min_v": exp.null(),
        "max_v": exp.null(),
        "inclusive": exp.true(),
        "condition": exp.true(),
    },
    query="""
SELECT *
FROM (
  SELECT AVG(@column) AS mean_@column
  FROM @this_model
  WHERE @condition
)
WHERE
  @OR(
    @IF(@min_v IS NOT NULL AND @inclusive, mean_@column < @min_v, NULL),
    @IF(@min_v IS NOT NULL AND NOT @inclusive, mean_@column <= @min_v, NULL),
    @IF(@max_v IS NOT NULL AND @inclusive, mean_@column > @max_v, NULL),
    @IF(@max_v IS NOT NULL AND NOT @inclusive, mean_@column >= @max_v, NULL),
  )
    """,
)

# kl_divergence(column=age, target_column=normalized_age, threshold=0.1)
kl_divergence_audit = ModelAudit(
    name="kl_divergence",
    defaults={"condition": exp.true()},
    query="""
WITH
  table_a AS (
    SELECT
      @source_column,
      COUNT(*) AS num_rows
    FROM @this_model
    WHERE @condition
    GROUP BY @source_column
  ),
  table_b AS (
    SELECT
      @target_column,
      COUNT(*) AS num_rows
    FROM @this_model
    WHERE @condition
    GROUP BY @target_column
  ),
  table_a_with_p AS (
    SELECT
      @source_column,
      num_rows,
      num_rows / SUM(num_rows) OVER () AS p
    FROM table_a
  ),
  table_b_with_q AS (
    SELECT
      @target_column,
      num_rows,
      num_rows / SUM(num_rows) OVER () AS q
    FROM table_b
  ),
  table_a_with_q AS (
    SELECT
      @source_column,
      num_rows,
      p,
      COALESCE(q, 0) AS q
    FROM table_a_with_p
    LEFT JOIN table_b_with_q USING (@source_column)
  ),
  table_b_with_p AS (
    SELECT
      @target_column,
      num_rows,
      q,
      COALESCE(p, 0) AS p
    FROM table_b_with_q
    LEFT JOIN table_a_with_p USING (@target_column)
  ),
  table_a_with_kl AS (
    SELECT
      @source_column,
      num_rows,
      p,
      q,
      p * LOG(p / NULLIF(q, 0)) AS kl
    FROM table_a_with_q
  ),
  table_b_with_kl AS (
    SELECT
      @target_column,
      num_rows,
      p,
      q,
      q * LOG(q / NULLIF(p, 0)) AS kl
    FROM table_b_with_p
  ),
  unioned AS (
    SELECT *
    FROM table_a_with_kl
    UNION ALL
    SELECT *
    FROM table_b_with_kl
  )
SELECT
  @source_column,
  @target_column,
  SUM(kl) AS kl_divergence
FROM unioned
GROUP BY @source_column, @target_column
HAVING kl_divergence > @threshold
    """,
)

# chi_square(column_a=account_tier, column_b=account_user_segment, dependent=true, critical_value=9.48773)
# Use the following to get the critical value for a given p-value:
# from scipy.stats import chi2
# chi2.ppf(0.95, 1) where 0.95 is the p-value and 1 is the degrees of freedom
# https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.chi2.html
# or use a table like https://www.medcalc.org/manual/chi-square-table.php
chi_square_audit = ModelAudit(
    name="chi_square",
    expressions=[
        "@def(c, (SELECT COUNT(DISTINCT x_a) FROM contingency_table))",
        "@def(r, (SELECT COUNT(DISTINCT x_b) FROM contingency_table))",
        "@def(E, (tot_a * tot_b / g_t))",
    ],
    defaults={"dependent": exp.true(), "condition": exp.true()},
    query="""
WITH
  samples AS (
    SELECT
      @column_a AS x_a,
      @column_b AS x_b,
    FROM @this_model
    WHERE @AND(@column_a IS NOT NULL AND @column_b IS NOT NULL, @condition)
  ),
  contingency_table AS (
    SELECT
      x_a,
      x_b,
      COUNT(*) as observed,
      (SELECT COUNT(*) FROM samples AS t WHERE r.x_a = t.x_a) as tot_a,
      (SELECT COUNT(*) FROM samples AS t WHERE r.x_b = t.x_b) as tot_b,
      (SELECT COUNT(*) FROM samples) as g_t -- g_t is the grand total
    FROM samples AS r
    GROUP BY x_a, x_b
  )

SELECT
  (@c - 1) * (@r - 1) as degrees_of_freedom,
  SUM((observed - @E) * (observed - @E) / @E) as chi_square
FROM contingency_table
-- H0: the two variables are independent
-- H1: the two variables are dependent
-- if chi_square > critical_value, reject H0
-- if chi_square <= critical_value, fail to reject H0
HAVING NOT @IF(@dependent, chi_square > @critical_value, chi_square <= @critical_value)
    """,
)

# The following audits are not yet implemented
# we are awaiting a first class way to express cross-model audits

# cardinality_equality(source_column=column_1, target_column=column_2, to=some.model)
# cardinality_equality_audit = ModelAudit(
#     name="cardinality_equality",
#     query="""
# WITH table_a AS (
#   SELECT
#     @source_column,
#     count(*) AS num_rows
#   FROM @this_model
#   GROUP BY @source_column
# ),
# table_b AS (
#   SELECT
#     @target_column,
#     count(*) AS num_rows
#   FROM @to
#   GROUP BY @target_column
# ),
# except_a AS (
#   SELECT *
#   FROM table_a
#   EXCEPT
#   SELECT *
#   FROM table_b
# ),
# except_b AS (
#   SELECT *
#   FROM table_b
#   EXCEPT
#   SELECT *
#   FROM table_a
# ),
# unioned AS (
#   SELECT *
#   FROM except_a
#   UNION ALL
#   SELECT *
#   FROM except_b
# )
# SELECT * FROM unioned
#     """,
# )


# relationship(to=some.model, source_column=id, target_column=id)
# relationship_audit = ModelAudit(
#     name="relationship",
#     defaults={"from_condition": exp.true(), "to_condition": exp.true()},
#     query="""
# SELECT
#   child.source_column
# FROM (
#   SELECT @source_column as source_column
#   FROM @this_model
#   WHERE NOT @source_column IS NULL
#   AND @from_condition
# ) AS child
# LEFT JOIN (
#   SELECT @target_column AS target_column
#   FROM @to
#   WHERE @to_condition
# ) AS parent
# ON child.source_column = parent.target_column
# WHERE parent.target_column IS NULL
#     """,
# )

# equality(columns=(column_1, column_2), to=some.model)
# equality_audit = ModelAudit(
#     name="equality",
#     query="""
# SELECT @EACH(@columns, c -> c)
# FROM @this_model
# EXCEPT
# SELECT @EACH(@columns, c -> c)
# FROM @to
#     """,
# )

# equal_row_count(to=some.model)
# equal_row_count_audit = ModelAudit(
#     name="equal_row_count",
#     query="""
# SELECT 1
# FROM (
#   SELECT count(*) as cnt
#   FROM @this_model
# ) AS src
# INNER JOIN (
#   SELECT count(*) as cnt
#   FROM @to
# ) AS tgt ON src.cnt != tgt.cnt
#     """,
# )

# fewer_rows_than(to=some.model)
# fewer_rows_than_audit = ModelAudit(
#     name="fewer_rows_than",
#     query="""
# SELECT 1
# FROM (
#   SELECT count(*) as cnt
#   FROM @this_model
# ) AS src
# INNER JOIN (
#   SELECT count(*) as cnt
#   FROM @to
# ) AS tgt ON src.cnt >= tgt.cnt
#     """,
# )


BUILT_IN_AUDITS = {
    audit.name: audit
    for _, model_audit in inspect.getmembers(
        sys.modules[__name__], lambda v: isinstance(v, ModelAudit)
    )
    for audit in (model_audit, create_non_blocking_copy(model_audit))
}
