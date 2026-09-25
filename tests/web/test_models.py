# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from sqlmesh.core.context import Context
from web.server.api.endpoints.models import get_models

pytestmark = pytest.mark.web


def test_get_models_multi_repo() -> None:
    """Models of every project are serialized, not just those of the first one.

    `context.path` is the first configured project, so it is not an ancestor of the models
    defined in any of the others.
    """
    context = Context(paths=["examples/multi/repo_1", "examples/multi/repo_2"], gateway="memory")

    paths_by_name = {model.name: model.path for model in get_models(context)}

    # Each model is reported relative to the project that defines it.
    assert paths_by_name["bronze.a"] == "models/a.sql"
    assert paths_by_name["silver.c"] == "models/c.sql"
