from __future__ import annotations

import fnmatch
import itertools
import pathlib
import typing as t

import ruamel

from sqlmesh.utils import unique
from sqlmesh.utils.pydantic import PydanticModel
from sqlmesh.core.dialect import normalize_model_name


class ModelTestMetadata(PydanticModel):
    path: pathlib.Path
    test_name: str
    body: t.Union[t.Dict, ruamel.yaml.comments.CommentedMap]

    @property
    def fully_qualified_test_name(self) -> str:
        return f"{self.path}::{self.test_name}"

    @property
    def model_name(self) -> str:
        return self.body.get("model", "")

    def __hash__(self) -> int:
        return self.fully_qualified_test_name.__hash__()


def filter_tests_by_patterns(
    tests: list[ModelTestMetadata], patterns: list[str]
) -> list[ModelTestMetadata]:
    """Filter out tests whose filename or name does not match a pattern.

    Args:
        tests: A list of ModelTestMetadata named tuples to match.
        patterns: A list of patterns to match against.

    Returns:
        A list of ModelTestMetadata named tuples.
    """
    return unique(
        test
        for test, pattern in itertools.product(tests, patterns)
        if ("*" in pattern and fnmatch.fnmatchcase(test.fully_qualified_test_name, pattern))
        or pattern in test.fully_qualified_test_name
    )


def filter_tests_by_model_names(
    tests: list[ModelTestMetadata],
    model_names: set[str],
    *,
    default_catalog: t.Optional[str] = None,
    dialect: t.Optional[str] = None,
) -> list[ModelTestMetadata]:
    """Keep tests whose YAML ``model:`` resolves to one of the given model names.

    Args:
        tests: Loaded test metadata.
        model_names: Model FQNs / names to keep (typically from a plan change set).
        default_catalog: Catalog used when normalizing short model names.
        dialect: Dialect used when normalizing model names.

    Returns:
        Tests that target a model in ``model_names``.
    """

    normalized_models = {
        normalize_model_name(name, default_catalog=default_catalog, dialect=dialect)
        for name in model_names
    }
    return [
        test
        for test in tests
        if test.model_name
        and normalize_model_name(test.model_name, default_catalog=default_catalog, dialect=dialect)
        in normalized_models
    ]
