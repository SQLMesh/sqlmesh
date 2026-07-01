from __future__ import annotations

from base64 import b64encode

from sqlmesh.utils import AttributeDict, yaml
from sqlmesh.utils.jinja import (
    ENVIRONMENT,
    JinjaMacroRegistry,
    MacroExtractor,
    MacroReference,
    MacroReturnVal,
    call_name,
    nodes,
)


def test_macro_registry_render():
    package_a = "{% macro macro_a_a() %}macro_a_a{% endmacro %}"

    package_b = """
{% macro macro_b_a() %}macro_b_a{% endmacro %}

{% macro macro_b_b() %}
{{ package_a.macro_a_a() }}
{{ package_b.macro_b_a() }}
{{ macro_b_a() }}
macro_b_b
{% endmacro %}"""

    local_macros = "{% macro local_macro() %}{{ package_b.macro_b_b() }}{% endmacro %}"

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    registry.add_macros(extractor.extract(local_macros))
    registry.add_macros(extractor.extract(package_a), package="package_a")
    registry.add_macros(extractor.extract(package_b), package="package_b")

    rendered = (
        registry.build_environment()
        .from_string("{{ local_macro() }}{{ package_a.macro_a_a() }}")
        .render()
    )
    rendered = [r for r in rendered.split("\n") if r]

    assert rendered == [
        "macro_a_a",
        "macro_b_a",
        "macro_b_a",
        "macro_b_b",
        "macro_a_a",
    ]

    assert (
        extractor.extract("""{% set foo = bar | replace("'", "\\"") %}""", dialect="bigquery") == {}
    )


def test_macro_registry_render_nested_self_package_references():
    package_a = """
{% macro macro_a_a() %}macro_a_a{% endmacro %}

{% macro macro_a_b() %}{{ package_a.macro_a_a() }}{% endmacro %}

{% macro macro_a_c() %}{{ package_a.macro_a_b() }}{% endmacro %}
"""

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    registry.add_macros(extractor.extract(package_a), package="package_a")

    rendered = registry.build_environment().from_string("{{ package_a.macro_a_c() }}").render()
    assert rendered == "macro_a_a"


def test_macro_registry_render_private_macros():
    package_a = """
{% macro _macro_a_a(flag) %}{% if not flag %}macro_a_a{% else %}{{ _macro_a_a(False) }}{% endif %}{% endmacro %}

{% macro macro_a_b() %}{{ package_a._macro_a_a(True) }}{% endmacro %}
"""

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    registry.add_macros(extractor.extract(package_a), package="package_a")

    rendered = registry.build_environment().from_string("{{ package_a.macro_a_b() }}").render()
    assert rendered == "macro_a_a"


def test_macro_registry_render_different_vars():
    package_a = "{% macro macro_a_a() %}{{ external() }}{% endmacro %}"

    local_macros = "{% macro local_macro() %}{{ package_a.macro_a_a() }}{% endmacro %}"

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    registry.add_macros(extractor.extract(local_macros))
    registry.add_macros(extractor.extract(package_a), package="package_a")

    rendered = (
        registry.build_environment(external=lambda: "test_a")
        .from_string("{{ local_macro() }}")
        .render()
    )
    assert rendered == "test_a"

    rendered = (
        registry.build_environment(external=lambda: "test_b")
        .from_string("{{ local_macro() }}")
        .render()
    )
    assert rendered == "test_b"


def test_macro_registry_trim():
    package_a = """
{% macro macro_a_a() %}macro_a_a{% endmacro %}
{% macro macro_a_b() %}macro_a_b{% endmacro %}
{% macro macro_a_c() %}macro_a_c{% endmacro %}
"""

    package_b = """
{% macro macro_b_a() %}{{ package_a.macro_a_a() }}{% endmacro %}

{% macro macro_b_b() %}{{ package_b.macro_b_a() }}{% endmacro %}

{% macro macro_b_c() %}{{ package_a.macro_a_c() }}{% endmacro %}
"""

    package_c = """
{% macro macro_c_a() %}macro_c_a{% endmacro %}
"""

    local_macros = """
{% macro local_macro_a() %}{{ package_b.macro_b_b() }}{% endmacro %}

{% macro local_macro_b() %}local_macro_b{% endmacro %}
"""

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    registry.add_macros(extractor.extract(local_macros))
    registry.add_macros(extractor.extract(package_a), package="package_a")
    registry.add_macros(extractor.extract(package_b), package="package_b")
    registry.add_macros(extractor.extract(package_c), package="package_c")

    trimmed_registry = registry.trim(
        [
            MacroReference(name="local_macro_a"),
            MacroReference(package="package_a", name="macro_a_b"),
        ]
    )

    assert set(trimmed_registry.packages) == {"package_a", "package_b"}
    assert set(trimmed_registry.packages["package_a"]) == {"macro_a_a", "macro_a_b"}
    assert set(trimmed_registry.packages["package_b"]) == {"macro_b_a", "macro_b_b"}
    assert set(trimmed_registry.root_macros) == {"local_macro_a"}

    rendered = (
        trimmed_registry.build_environment()
        .from_string("{{ local_macro_a() }} {{ package_a.macro_a_b() }}")
        .render()
    )
    assert rendered == "macro_a_a macro_a_b"

    trimmed_registry_for_package_b = registry.trim(
        [MacroReference(name="macro_b_b")], package="package_b"
    )
    assert set(trimmed_registry_for_package_b.packages) == {"package_a", "package_b"}
    assert set(trimmed_registry_for_package_b.packages["package_a"]) == {"macro_a_a"}
    assert set(trimmed_registry_for_package_b.packages["package_b"]) == {"macro_b_a", "macro_b_b"}
    assert not trimmed_registry_for_package_b.root_macros


def test_macro_return():
    macros = "{% macro test_return() %}{{ macro_return([1, 2, 3]) }}{% endmacro %}"

    def macro_return(val):
        raise MacroReturnVal(val)

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    registry.add_macros(extractor.extract(macros))

    rendered = (
        registry.build_environment(macro_return=macro_return)
        .from_string("{{ test_return() }}")
        .render()
    )
    assert rendered == "[1, 2, 3]"


def test_global_objs():
    original_registry = JinjaMacroRegistry(global_objs={"target": AttributeDict({"test": "value"})})

    deserialized_registry = JinjaMacroRegistry.parse_raw(original_registry.json())
    assert deserialized_registry.global_objs["target"].test == "value"


def test_macro_registry_recursion():
    macros = """
{% macro macro_a(n) %} {{ macro_b(n) }} {% endmacro %}

{% macro macro_b(n) %}
{% if n <= 0 %}
  end
{% else %}
  {{ macro_a(n - 1) }}
{% endif %}
{% endmacro %}
"""

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    registry.add_macros(extractor.extract(macros))

    rendered = registry.build_environment().from_string("{{ macro_a(4) }}").render()
    assert rendered.strip() == "end"

    assert registry.trim([MacroReference(name="macro_a")]).root_macros.keys() == {
        "macro_a",
        "macro_b",
    }


def test_macro_registry_recursion_with_package():
    macros = """
{% macro macro_a(n) %}{{ sushi.macro_b(n) }}{% endmacro %}
j
{% macro macro_b(n) %}
{% if n <= 0 %}
end
{% else %}
{{ sushi.macro_a(n - 1) }}
{% endif %}
{% endmacro %}
"""

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry(root_package_name="sushi")

    registry.add_macros(extractor.extract(macros))

    rendered = registry.build_environment().from_string("{{ macro_a(4) }}").render()
    assert rendered.strip() == "end"


def test_macro_registry_top_level_packages():
    package_a = """
{% macro macro_a_a() %}
macro_a_a
{% endmacro %}"""

    local_macros = "{% macro local_macro() %}{{ macro_a_a() }}{% endmacro %}"

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry(top_level_packages=["package_a"])

    registry.add_macros(extractor.extract(local_macros))
    registry.add_macros(extractor.extract(package_a), package="package_a")

    rendered = (
        registry.build_environment()
        .from_string("{{ local_macro() }}{{ package_a.macro_a_a() }}")
        .render()
    )
    rendered = [r for r in rendered.split("\n") if r]

    assert rendered == [
        "macro_a_a",
        "macro_a_a",
    ]


def test_find_call_names():
    jinja_str = "{{ local_macro() }}{{ package.package_macro() }}{{ 'stringval'.function() }}"
    [call_name(node) for node in ENVIRONMENT.parse(jinja_str).find_all(nodes.Call)] == [
        ("local_macro",),
        ("package", "package_macro"),
        ("'stringval'", "function"),
    ]


def test_dbt_adapter_macro_scope():
    package_a = """
{% macro spark__macro_a() %}
macro_a
{% endmacro %}"""

    extractor = MacroExtractor()
    registry = JinjaMacroRegistry()

    macros = extractor.extract(package_a)
    macros["spark__macro_a"].is_top_level = True

    registry.add_macros(macros, package="package_a")

    rendered = registry.build_environment().from_string("{{ spark__macro_a() }}").render()
    assert rendered.strip() == "macro_a"


def test_macro_registry_to_expressions_sorted():
    refs = AttributeDict(
        {
            "payments": {
                "database": "jaffle_shop",
                "schema": "main",
                "nested": {"foo": "bar", "baz": "bing"},
            },
            "orders": {"schema": "main", "database": "jaffle_shop", "nested_list": ["b", "a", "c"]},
        }
    )

    registry = JinjaMacroRegistry()
    registry.add_globals({"sources": {}, "refs": refs})

    # Ensure that the AttributeDict string representation is sorted
    # in order to prevent an unexpected *visual* diff in ModelDiff
    # (note that the actual diff is based on the data hashes, so this is purely visual)
    expressions = registry.to_expressions()
    assert len(expressions) == 1
    assert (
        expressions[0].sql(dialect="duckdb")
        == "refs = {'orders': {'database': 'jaffle_shop', 'nested_list': ['a', 'b', 'c'], 'schema': 'main'}, 'payments': {'database': 'jaffle_shop', 'nested': {'baz': 'bing', 'foo': 'bar'}, 'schema': 'main'}}\n"
        "sources = {}"
    )


def test_builtin_base64_filters():
    encoded = b64encode(b"secret").decode("utf-8")

    env = JinjaMacroRegistry().build_environment()
    assert env.from_string("{{ value | b64decode }}").render(value=encoded) == "secret"
    assert env.from_string("{{ 'secret' | b64encode }}").render() == encoded
    assert env.from_string("{{ 'secret' | b64encode | b64decode }}").render() == "secret"

    # The same filters are available when rendering Jinja in config YAML files.
    config = yaml.load(f'env_vars:\n  TOKEN: "{{{{ "{encoded}" | b64decode }}}}"')
    assert config == {"env_vars": {"TOKEN": "secret"}}


def test_builtin_b64decode_with_env_var(monkeypatch):
    # Real-world use case: a base64-encoded secret stored in an environment variable
    # is decoded inline in config YAML via env_var(...) piped through b64decode.
    monkeypatch.setenv("SNOWFLAKE_PW_B64", b64encode(b"super-secret-pw").decode("utf-8"))

    config = yaml.load("password: \"{{ env_var('SNOWFLAKE_PW_B64') | b64decode }}\"")
    assert config == {"password": "super-secret-pw"}
