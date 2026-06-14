from lsprotocol.types import Position
from sqlmesh.core.context import Context
from sqlmesh.lsp.context import LSPContext, ModelTarget, AuditTarget
from sqlmesh.lsp.reference import (
    get_model_find_all_references,
    get_model_definitions_for_a_path,
)
from sqlmesh.lsp.uri import URI
from tests.lsp.test_reference_cte import find_ranges_from_regex


def test_find_references_for_model_usages():
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    # Find customers model which uses sushi.orders
    customers_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and "sushi.customers" in info.names
    )

    with open(customers_path, "r", encoding="utf-8") as file:
        read_file = file.readlines()

    # Find sushi.orders reference
    ranges = find_ranges_from_regex(read_file, r"sushi\.orders")
    assert len(ranges) >= 1, "Should find at least one reference to sushi.orders"

    # Click on the model reference
    position = Position(line=ranges[0].start.line, character=ranges[0].start.character + 6)
    references = get_model_find_all_references(lsp_context, URI.from_path(customers_path), position)
    assert len(references) >= 6, (
        f"Expected at least 6 references to sushi.orders (including column prefix), found {len(references)}"
    )

    # Verify expected files are present
    reference_files = {str(ref.path) for ref in references}
    expected_patterns = [
        "orders",
        "customers",
        "customer_revenue_by_day",
        "customer_revenue_lifetime",
        "latest_order",
        "waiter_revenue_by_day",
    ]
    for pattern in expected_patterns:
        assert any(pattern in uri for uri in reference_files), (
            f"Missing reference in file containing '{pattern}'"
        )

    # Verify exact ranges for each reference pattern
    # Note: customers file has multiple references due to column prefix support
    expected_ranges = {
        "orders": [(0, 0, 0, 0)],  # the start for the model itself
        "customers": [(30, 7, 30, 19)],  # FROM clause
        "waiter_revenue_by_day": [(19, 5, 19, 17)],
        "customer_revenue_lifetime": [(38, 7, 38, 19)],
        "customer_revenue_by_day": [(33, 5, 33, 17)],
        "latest_order": [(12, 5, 12, 17)],
    }

    # Group references by file pattern
    refs_by_pattern = {}
    for ref in references:
        matched_pattern = None
        for pattern in expected_patterns:
            if pattern in str(ref.path):
                matched_pattern = pattern
                break

        if matched_pattern:
            if matched_pattern not in refs_by_pattern:
                refs_by_pattern[matched_pattern] = []
            refs_by_pattern[matched_pattern].append(ref)

    # Verify each pattern has the expected references
    for pattern, expected_range_list in expected_ranges.items():
        assert pattern in refs_by_pattern, f"Missing references for pattern '{pattern}'"

        actual_refs = refs_by_pattern[pattern]
        assert len(actual_refs) == len(expected_range_list), (
            f"Expected {len(expected_range_list)} references for {pattern}, found {len(actual_refs)}"
        )

        # Sort both actual and expected by line number for consistent comparison
        actual_refs_sorted = sorted(
            actual_refs, key=lambda r: (r.range.start.line, r.range.start.character)
        )
        expected_sorted = sorted(expected_range_list, key=lambda r: (r[0], r[1]))

        for i, (ref, expected_range) in enumerate(zip(actual_refs_sorted, expected_sorted)):
            expected_start_line, expected_start_char, expected_end_line, expected_end_char = (
                expected_range
            )

            assert ref.range.start.line == expected_start_line, (
                f"Expected {pattern} reference #{i + 1} start line {expected_start_line}, found {ref.range.start.line}"
            )
            assert ref.range.start.character == expected_start_char, (
                f"Expected {pattern} reference #{i + 1} start character {expected_start_char}, found {ref.range.start.character}"
            )
            assert ref.range.end.line == expected_end_line, (
                f"Expected {pattern} reference #{i + 1} end line {expected_end_line}, found {ref.range.end.line}"
            )
            assert ref.range.end.character == expected_end_char, (
                f"Expected {pattern} reference #{i + 1} end character {expected_end_char}, found {ref.range.end.character}"
            )


def test_find_references_for_marketing_model():
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    customers_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and "sushi.customers" in info.names
    )

    with open(customers_path, "r", encoding="utf-8") as file:
        read_file = file.readlines()

    # Find sushi.marketing reference
    marketing_ranges = find_ranges_from_regex(read_file, r"sushi\.marketing")
    assert len(marketing_ranges) >= 1, "Should find at least one reference to sushi.marketing"

    position = Position(
        line=marketing_ranges[0].start.line, character=marketing_ranges[0].start.character + 8
    )
    references = get_model_find_all_references(lsp_context, URI.from_path(customers_path), position)

    # sushi.marketing should have exactly 2 references: model itself + customers usage
    assert len(references) == 2, (
        f"Expected exactly 2 references to sushi.marketing, found {len(references)}"
    )

    # Verify files are present
    reference_files = {str(ref.path) for ref in references}
    expected_patterns = ["marketing", "customers"]
    for pattern in expected_patterns:
        assert any(pattern in uri for uri in reference_files), (
            f"Missing reference in file containing '{pattern}'"
        )


def test_find_references_for_python_model():
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    # Start from customer_revenue_by_day which references sushi.items
    revenue_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and "sushi.customer_revenue_by_day" in info.names
    )

    with open(revenue_path, "r", encoding="utf-8") as file:
        revenue_file = file.readlines()

    # Find sushi.items reference
    items_ranges = find_ranges_from_regex(revenue_file, r"sushi\.items")
    assert len(items_ranges) >= 1, "Should find at least one reference to sushi.items"

    position = Position(
        line=items_ranges[0].start.line, character=items_ranges[0].start.character + 6
    )
    references = get_model_find_all_references(lsp_context, URI.from_path(revenue_path), position)
    assert len(references) == 5

    # Verify expected files
    reference_files = {str(ref.path) for ref in references}

    # Models and also the Audit which references it: assert_item_price_above_zero
    expected_patterns = [
        "items",
        "customer_revenue_by_day",
        "customer_revenue_lifetime",
        "waiter_revenue_by_day",
        "assert_item_price_above_zero",
    ]
    for pattern in expected_patterns:
        assert any(pattern in uri for uri in reference_files), (
            f"Missing reference in file containing '{pattern}'"
        )


def test_waiter_revenue_by_day_multiple_references():
    # Test sushi.waiter_revenue_by_day which is referenced 3 times in top_waiters
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    top_waiters_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and "sushi.top_waiters" in info.names
    )

    with open(top_waiters_path, "r", encoding="utf-8") as file:
        top_waiters_file = file.readlines()

    # Find multiple references to sushi.waiter_revenue_by_day
    waiter_revenue_ranges = find_ranges_from_regex(
        top_waiters_file, r"sushi\.waiter_revenue_by_day"
    )
    assert len(waiter_revenue_ranges) >= 2, (
        "Should find at least 2 references to sushi.waiter_revenue_by_day in top_waiters"
    )

    # Click on the first reference
    position = Position(
        line=waiter_revenue_ranges[0].start.line,
        character=waiter_revenue_ranges[0].start.character + 10,
    )
    references = get_model_find_all_references(
        lsp_context, URI.from_path(top_waiters_path), position
    )

    # Should find model definition + 3 references in top_waiters = 4 total
    assert len(references) == 4, (
        f"Expected exactly 4 references to sushi.waiter_revenue_by_day, found {len(references)}"
    )

    # Count references in top_waiters file
    top_waiters_refs = [ref for ref in references if "top_waiters" in str(ref.path)]
    assert len(top_waiters_refs) == 3, (
        f"Expected exactly 3 references in top_waiters, found {len(top_waiters_refs)}"
    )

    # Verify model definition is included
    assert any("waiter_revenue_by_day" in str(ref.path) for ref in references), (
        "Should include model definition"
    )


def test_precise_character_positions():
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    customers_path = next(
        path
        for path, info in lsp_context.map.items()
        if isinstance(info, ModelTarget) and "sushi.customers" in info.names
    )

    # Test clicking on different parts of "sushi.orders" reference at line 31

    # Click on 's' in "sushi" - should work
    position = Position(line=30, character=7)
    references = get_model_find_all_references(lsp_context, URI.from_path(customers_path), position)
    assert len(references) > 0, "Should find references when clicking on 's' in 'sushi'"

    # Click on '.' between sushi and orders - should work
    position = Position(line=30, character=12)
    references = get_model_find_all_references(lsp_context, URI.from_path(customers_path), position)
    assert len(references) > 0, "Should find references when clicking on '.' separator"

    # Click on 'o' in "orders" - should work
    position = Position(line=30, character=13)
    references = get_model_find_all_references(lsp_context, URI.from_path(customers_path), position)
    assert len(references) > 0, "Should find references when clicking on 'o' in 'orders'"

    # Click just before "sushi" - should not work
    position = Position(line=30, character=6)
    references = get_model_find_all_references(lsp_context, URI.from_path(customers_path), position)
    assert len(references) == 0, "Should not find references when clicking just before 'sushi'"

    # Click just after "orders" - should not work
    position = Position(line=30, character=21)
    references = get_model_find_all_references(lsp_context, URI.from_path(customers_path), position)
    assert len(references) == 0, "Should not find references when clicking just after 'orders'"


def test_audit_model_references():
    # Tests finding model references in audits
    context = Context(paths=["examples/sushi"])
    lsp_context = LSPContext(context)

    # Find audit files
    audit_paths = [path for path, info in lsp_context.map.items() if isinstance(info, AuditTarget)]

    if audit_paths:
        audit_path = audit_paths[0]
        refs = get_model_definitions_for_a_path(lsp_context, URI.from_path(audit_path))

        # Audits can reference models
        if refs:
            # Click on the first reference which is: sushi.items
            first_ref = refs[0]
            position = Position(
                line=first_ref.range.start.line, character=first_ref.range.start.character + 1
            )
            references = get_model_find_all_references(
                lsp_context, URI.from_path(audit_path), position
            )

            assert len(references) == 5, "Should find references from audit files as well"

            reference_files = {str(ref.path) for ref in references}

            # Models and also the Audit which references it: assert_item_price_above_zero
            expected_patterns = [
                "items",
                "customer_revenue_by_day",
                "customer_revenue_lifetime",
                "waiter_revenue_by_day",
                "assert_item_price_above_zero",
            ]
            for pattern in expected_patterns:
                assert any(pattern in uri for uri in reference_files), (
                    f"Missing reference in file containing '{pattern}'"
                )
