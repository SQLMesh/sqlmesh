from pathlib import Path

from sqlmesh.cli.project_init import init_example_project
from sqlmesh.core.context import Context
from sqlmesh.core.linter.rule import Position
from sqlmesh.lsp.context import LSPContext, ModelTarget
from sqlmesh.lsp.reference import get_all_references
from sqlmesh.lsp.uri import URI
from tests.lsp.test_reference_cte import find_ranges_from_regex


def test_model_reference_with_column_prefix():
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    sushi_customers_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and "sushi.customers" in info.names
    )

    with open(sushi_customers_path, "r", encoding="utf-8") as file:
        read_file = file.readlines()

    # Test finding references for "sushi.orders"
    ranges = find_ranges_from_regex(read_file, r"sushi\.orders")

    # Click on the table reference in FROM clause (should be the second occurrence)
    from_clause_range = None
    for r in ranges:
        line_content = read_file[r.start.line].strip()
        if "FROM" in line_content:
            from_clause_range = r
            break

    assert from_clause_range is not None, "Should find FROM clause with sushi.orders"

    position = Position(
        line=from_clause_range.start.line, character=from_clause_range.start.character + 6
    )

    model_refs = get_all_references(lsp_context, URI.from_path(sushi_customers_path), position)

    assert len(model_refs) >= 6

    # Verify that we have the FROM clause reference
    assert any(ref.range.start.line == from_clause_range.start.line for ref in model_refs), (
        "Should find FROM clause reference"
    )


def test_column_prefix_references_are_found():
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    sushi_customers_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and "sushi.customers" in info.names
    )

    with open(sushi_customers_path, "r", encoding="utf-8") as file:
        read_file = file.readlines()

    # Find all occurrences of sushi.orders in the file
    ranges = find_ranges_from_regex(read_file, r"sushi\.orders")

    # Should find exactly 1 in FROM clause with column prefix
    assert len(ranges) == 1, f"Expected 1 occurrence of 'sushi.orders', found {len(ranges)}"

    # Verify we have the expected lines
    line_contents = [read_file[r.start.line].strip() for r in ranges]

    # Should find FROM clause
    assert any("FROM sushi.orders" in content for content in line_contents), (
        "Should find FROM clause with sushi.orders"
    )


def test_quoted_uppercase_table_and_column_references(tmp_path: Path):
    # Initialize example project in temporary directory with case sensitive normalization
    init_example_project(
        tmp_path, engine_type="duckdb", dialect="duckdb,normalization_strategy=case_sensitive"
    )

    # Create a model with quoted uppercase schema and table names
    models_dir = tmp_path / "models"

    # First, create the uppercase SUSHI.orders model that will be referenced
    uppercase_orders_path = models_dir / "uppercase_orders.sql"
    uppercase_orders_path.write_text("""MODEL (
  name "SUSHI".orders,
  kind FULL
);

SELECT
  1 as id,
  1 as customer_id,
  1 as item_id""")

    # Second, create the lowercase sushi.orders model that will be referenced
    lowercase_orders_path = models_dir / "lowercase_orders.sql"
    lowercase_orders_path.write_text("""MODEL (
  name sushi.orders,
  kind FULL
);

SELECT
  1 as id,
  1 as customer_id""")

    quoted_test_path = models_dir / "quoted_test.sql"
    quoted_test_path.write_text("""MODEL (
  name "SUSHI".quoted_test,
  kind FULL
);

SELECT
  o.id,
  o.customer_id,
  o.item_id,
  c.item_id as c_item_id
FROM "SUSHI".orders AS o, sushi.orders as c
WHERE "SUSHI".orders.id > 0
  AND "SUSHI".orders.customer_id IS NOT NULL
  AND sushi.orders.id > 0""")

    context = Context(paths=tmp_path)
    lsp_context = LSPContext(context)

    # Find the quoted test model
    quoted_test_model_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and '"SUSHI".quoted_test' in info.names
    )

    with open(quoted_test_model_path, "r", encoding="utf-8") as file:
        read_file = file.readlines()

    # Test finding references for quoted "SUSHI".orders
    ranges = find_ranges_from_regex(read_file, r'"SUSHI"\.orders')

    # Should find 3 occurrences: FROM clause and 2 in WHERE clause with column prefix
    assert len(ranges) == 3, f"Expected 3 occurrences of '\"SUSHI\".orders', found {len(ranges)}"

    # Click on the table reference in FROM clause
    from_clause_range = None
    for r in ranges:
        line_content = read_file[r.start.line].strip()
        if "FROM" in line_content:
            from_clause_range = r
            break

    assert from_clause_range is not None, 'Should find FROM clause with "SUSHI".orders'

    position = Position(
        line=from_clause_range.start.line, character=from_clause_range.start.character + 5
    )

    model_refs = get_all_references(lsp_context, URI.from_path(quoted_test_model_path), position)

    # Should find only references to "SUSHI".orders (3 total: FROM clause and 2 column prefixes in WHERE)
    # The lowercase sushi.orders should NOT be included if case sensitivity is working
    assert len(model_refs) == 4, (
        f'Expected exactly 3 references for "SUSHI".orders, found {len(model_refs)}'
    )

    # Verify that we have all 3 references
    ref_lines = [ref.range.start.line for ref in model_refs]

    # Count how many references are on each line
    from_line = from_clause_range.start.line
    where_lines = [r.start.line for r in ranges if r.start.line != from_line]

    assert from_line in ref_lines, "Should find FROM clause reference"
    for where_line in where_lines:
        assert where_line in ref_lines, f"Should find WHERE clause reference on line {where_line}"

    # Now test that lowercase sushi.orders references are separate
    lowercase_ranges = find_ranges_from_regex(read_file, r"sushi\.orders")

    # Should find 2 occurrences: FROM clause and 1 in WHERE clause
    assert len(lowercase_ranges) == 2, (
        f"Expected 2 occurrences of 'sushi.orders', found {len(lowercase_ranges)}"
    )

    # Click on the lowercase table reference
    lowercase_from_range = None
    for r in lowercase_ranges:
        line_content = read_file[r.start.line].strip()
        if "FROM" in line_content:
            lowercase_from_range = r
            break

    assert lowercase_from_range is not None, "Should find FROM clause with sushi.orders"

    lowercase_position = Position(
        line=lowercase_from_range.start.line, character=lowercase_from_range.start.character + 5
    )

    lowercase_refs = get_all_references(
        lsp_context, URI.from_path(quoted_test_model_path), lowercase_position
    )

    # Should find only references to lowercase sushi.orders, NOT the uppercase ones
    assert len(lowercase_refs) == 3, (
        f"Expected exactly 2 references for sushi.orders, found {len(lowercase_refs)}"
    )
