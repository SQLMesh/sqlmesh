from __future__ import annotations

import datetime
import multiprocessing as mp
import os
import typing as t
from pathlib import Path

SQLMESH = "sqlmesh"
SQLMESH_MANAGED = "sqlmesh_managed"
SQLMESH_PATH = Path(os.getenv("SQLMESH_HOME") or Path.home() / ".sqlmesh")

PROD = "prod"
"""Prod"""
DEV = "dev"
"""Dev"""

SNAPSHOTS_PATH = "snapshots"
"""Snapshots path"""
DEFAULT_SNAPSHOT_TTL = "in 1 week"
"""Default snapshot TTL"""
DEFAULT_ENVIRONMENT_TTL = "in 1 week"
"""Default environment TTL"""
IGNORE_PATTERNS = [
    ".ipynb_checkpoints/*",
]
"""Ignore patterns"""
DATA_VERSION_LIMIT = 10
"""Data version limit"""
DEFAULT_TIME_COLUMN_FORMAT = "%Y-%m-%d"
"""Default time column format"""
MAX_MODEL_DEFINITION_SIZE = 10000
"""Maximum number of characters in a model definition"""


# The maximum number of fork processes, used for loading projects
# None means default to process pool, 1 means don't fork, :N is number of processes
# Factors in the number of available CPUs even if the process is bound to a subset of them
# (e.g. via taskset) to avoid oversubscribing the system and causing kill signals
if hasattr(os, "fork") and not mp.current_process().daemon:
    try:
        MAX_FORK_WORKERS: t.Optional[int] = int(os.getenv("MAX_FORK_WORKERS"))  # type: ignore
    except TypeError:
        MAX_FORK_WORKERS = (
            len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None  # type: ignore
        )
else:
    MAX_FORK_WORKERS = 1

EPOCH = datetime.date(1970, 1, 1)

DEFAULT_MAX_LIMIT = 1000
"""The default maximum row limit that is used when evaluating a model."""

DEFAULT_LOG_LIMIT = 20
"""The default number of logs to keep."""

DEFAULT_LOG_FILE_DIR = "logs"
"""The default directory for log files."""

AUDITS = "audits"
CACHE = ".cache"
EXTERNAL_MODELS = "external_models"
LINTER = "linter"
MACROS = "macros"
MATERIALIZATIONS = "materializations"
METRICS = "metrics"
MODELS = "models"
SEEDS = "seeds"
SIGNALS = "signals"
TESTS = "tests"

EXTERNAL_MODELS_YAML = "external_models.yaml"
EXTERNAL_MODELS_DEPRECATED_YAML = "schema.yaml"
REQUIREMENTS = "sqlmesh-requirements.lock"

DEFAULT_SCHEMA = "default"

SQLMESH_VARS = "__sqlmesh__vars__"
SQLMESH_VARS_METADATA = "__sqlmesh__vars__metadata__"
SQLMESH_BLUEPRINT_VARS = "__sqlmesh__blueprint__vars__"
SQLMESH_BLUEPRINT_VARS_METADATA = "__sqlmesh__blueprint__vars__metadata__"

VAR = "var"
BLUEPRINT_VAR = "blueprint_var"
GATEWAY = "gateway"

SQLMESH_MACRO = "__sqlmesh__macro__"
SQLMESH_BUILTIN = "__sqlmesh__builtin__"
SQLMESH_METADATA = "__sqlmesh__metadata__"


BUILTIN = "builtin"
DBT = "dbt"
NATIVE = "native"
HYBRID = "hybrid"

DISABLE_SQLMESH_STATE_MIGRATION = "SQLMESH__AIRFLOW__DISABLE_STATE_MIGRATION"

LIQUID_CLUSTERING_KEYWORDS: frozenset = frozenset({"AUTO", "NONE"})
