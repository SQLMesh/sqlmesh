from __future__ import annotations

import copy
import importlib
import logging
import os
import random
import re
import string
import sys
import time
import traceback
import types
import typing as t
import uuid
from dataclasses import dataclass
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from enum import IntEnum, Enum
from functools import lru_cache, reduce, wraps
from pathlib import Path

import unicodedata
from sqlglot import exp
from sqlglot.dialects.dialect import Dialects

logger = logging.getLogger(__name__)

T = t.TypeVar("T")
KEY = t.TypeVar("KEY", bound=t.Hashable)
VALUE = t.TypeVar("VALUE")
ITEM = t.TypeVar("ITEM")
GROUP = t.TypeVar("GROUP")
DECORATOR_RETURN_TYPE = t.TypeVar("DECORATOR_RETURN_TYPE")

ALPHANUMERIC = string.ascii_lowercase + string.digits


def optional_import(name: str) -> t.Optional[types.ModuleType]:
    """Optionally import a module.

    Args:
        name: The name of the module to import.
    Returns:
        The module if it is installed.
    """
    try:
        module = importlib.import_module(name)
    except ImportError:
        return None
    return module


def major_minor(version: str) -> t.Tuple[int, int]:
    """Returns a tuple of just the major.minor for a version string (major.minor.patch)."""
    return t.cast(t.Tuple[int, int], tuple(int(part) for part in version.split(".")[0:2]))


def unique(iterable: t.Iterable[T], by: t.Callable[[T], t.Any] = lambda i: i) -> t.List[T]:
    return list({by(i): None for i in iterable})


def random_id(short: bool = False) -> str:
    if short:
        return "".join(random.choices(ALPHANUMERIC, k=8))

    return uuid.uuid4().hex


class UniqueKeyDict(t.Dict[KEY, VALUE]):
    """Dict that raises when a duplicate key is set."""

    def __init__(self, name: str, *args: t.Dict[KEY, VALUE], **kwargs: VALUE) -> None:
        self.name = name
        super().__init__(*args, **kwargs)

    def __setitem__(self, k: KEY, v: VALUE) -> None:
        if k in self:
            raise ValueError(
                f"Duplicate key '{k}' found in UniqueKeyDict<{self.name}>. Call dict.update(...) if this is intentional."
            )
        super().__setitem__(k, v)


class AttributeDict(dict, t.Mapping[KEY, VALUE]):
    def __getattr__(self, key: t.Any) -> t.Optional[VALUE]:
        if key.startswith("__") and not hasattr(self, key):
            raise AttributeError
        return self.get(key)

    def set(self, field: str, value: t.Any) -> str:
        self[field] = value
        # Return an empty string, so that this method can be used within Jinja
        return ""

    def __deepcopy__(self, memo: t.Dict[t.Any, AttributeDict]) -> AttributeDict:
        copy: AttributeDict = AttributeDict()
        memo[id(self)] = copy
        for k, v in self.items():
            copy[k] = deepcopy(v, memo)
        return copy

    def __call__(self, **kwargs: t.Dict[str, t.Any]) -> str:
        self.update(**kwargs)
        # Return an empty string, so that this method can be used within Jinja
        return ""

    def __getstate__(self) -> t.Optional[t.Dict[t.Any, t.Any]]:
        return None


class registry_decorator:
    """A decorator that registers itself."""

    registry_name = ""
    _registry: t.Optional[UniqueKeyDict] = None

    @classmethod
    def registry(cls) -> UniqueKeyDict:
        if cls._registry is None:
            cls._registry = UniqueKeyDict(cls.registry_name)
        return cls._registry

    def __init__(self, name: str = "") -> None:
        self.name = name

    def __call__(
        self, func: t.Callable[..., DECORATOR_RETURN_TYPE]
    ) -> t.Callable[..., DECORATOR_RETURN_TYPE]:
        self.func = func

        registry = self.registry()
        func_name = (self.name or func.__name__).lower()

        try:
            registry[func_name] = self
        except ValueError:
            # No need to raise due to duplicate key if the functions are identical
            if func.__code__.co_code != registry[func_name].func.__code__.co_code:
                raise ValueError(f"Duplicate name: '{func_name}'.")

        @wraps(func)
        def wrapper(*args: t.Any, **kwargs: t.Any) -> DECORATOR_RETURN_TYPE:
            return func(*args, **kwargs)

        return wrapper

    @classmethod
    def get_registry(cls) -> UniqueKeyDict:
        """Get a copy of the registry"""
        return UniqueKeyDict(cls.registry_name, **(cls._registry or {}))

    @classmethod
    def set_registry(cls, registry: UniqueKeyDict) -> None:
        """Set the registry."""
        cls._registry = registry


@contextmanager
def sys_path(*paths: Path) -> t.Iterator[None]:
    """A context manager to temporarily add a path to 'sys.path'."""
    inserted = set()

    for path in paths:
        path_str = str(path.absolute())

        if path_str not in sys.path:
            sys.path.insert(0, path_str)
            inserted.add(path_str)

    try:
        yield
    finally:
        for path_str in inserted:
            sys.path.remove(path_str)


def format_exception(exception: BaseException) -> t.List[str]:
    if sys.version_info < (3, 10):
        return traceback.format_exception(type(exception), exception, exception.__traceback__)  # type: ignore
    return traceback.format_exception(exception)  # type: ignore


def word_characters_only(s: str, replacement_char: str = "_") -> str:
    """
    Replace all non-word characters in string with the replacement character.
    Reference SO: https://stackoverflow.com/questions/1276764/stripping-everything-but-alphanumeric-chars-from-a-string-in-python/70310018#70310018

    >>> word_characters_only("Hello, world!")
    'Hello__world_'
    >>> word_characters_only("Hello, world! 123", '')
    'Helloworld123'
    """
    return re.sub(r"\W", replacement_char, s)


def str_to_bool(s: t.Optional[str]) -> bool:
    """
    Convert a string to a boolean. disutils is being deprecated and it is recommended to implement your own version:
    https://peps.python.org/pep-0632/

    Unlike disutils, this actually returns a bool and never raises. If a value cannot be determined to be true
    then false is returned.
    """
    if not s:
        return False
    return s.lower() in ("true", "1", "t", "y", "yes", "on")


_debug_mode_enabled: bool = False


def enable_debug_mode() -> None:
    global _debug_mode_enabled
    _debug_mode_enabled = True


def debug_mode_enabled() -> bool:
    return _debug_mode_enabled or str_to_bool(os.environ.get("SQLMESH_DEBUG"))


def ttl_cache(ttl: int = 60, maxsize: int = 128000) -> t.Callable:
    """Caches a function that clears whenever the current epoch / ttl seconds changes.

    TTL is not exact, it is used as a salt. So by default, at every minute mark, the cache will be cleared.
    This is done for simplicity.

    Args:
        ttl: The number of seconds to hold the cache for.
        maxsize: The maximum size of the cache.
    """

    def decorator(func: t.Callable) -> t.Any:
        @lru_cache(maxsize=maxsize)
        def cache(tick: int, *args: t.Any, **kwargs: t.Any) -> t.Any:
            return func(*args, **kwargs)

        @wraps(func)
        def wrap(*args: t.Any, **kwargs: t.Any) -> t.Any:
            return cache(int(time.time() / ttl), *args, **kwargs)

        return wrap

    return decorator


class classproperty(property):
    """Similar to a normal property but works for class methods"""

    def __get__(self, obj: t.Any, owner: t.Any = None) -> t.Any:
        return classmethod(self.fget).__get__(None, owner)()  # type: ignore


@contextmanager
def env_vars(environ: dict[str, str]) -> t.Iterator[None]:
    """A context manager to temporarily modify environment variables."""
    old_environ = os.environ.copy()
    os.environ.update(environ)

    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_environ)


def merge_dicts(*args: t.Dict) -> t.Dict:
    """
    Merges dicts. Just does key collision replacement
    """

    def merge(a: t.Dict, b: t.Dict) -> t.Dict:
        for b_key, b_value in b.items():
            a_value = a.get(b_key)
            if isinstance(a_value, dict) and isinstance(b_value, dict):
                merge(a_value, b_value)
            elif isinstance(b_value, dict):
                a[b_key] = copy.deepcopy(b_value)
            else:
                a[b_key] = b_value
        return a

    return reduce(merge, args, {})


def sqlglot_dialects() -> str:
    return "'" + "', '".join(Dialects.__members__.values()) + "'"


NON_ALNUM = re.compile(r"[^a-zA-Z0-9_]")

NON_ALUM_INCLUDE_UNICODE = re.compile(r"\W", flags=re.UNICODE)


def sanitize_name(name: str, *, include_unicode: bool = False) -> str:
    if include_unicode:
        s = unicodedata.normalize("NFC", name)
        s = NON_ALUM_INCLUDE_UNICODE.sub("_", s)
        return s
    return NON_ALNUM.sub("_", name)


def groupby(
    items: t.Iterable[ITEM],
    func: t.Callable[[ITEM], GROUP],
) -> t.DefaultDict[GROUP, t.List[ITEM]]:
    grouped = defaultdict(list)
    for item in items:
        grouped[func(item)].append(item)
    return grouped


def columns_to_types_to_struct(
    columns_to_types: t.Union[t.Dict[str, exp.DataType], t.Dict[str, str]],
) -> exp.DataType:
    """
    Converts a dict of column names to types to a struct.
    """
    return exp.DataType(
        this=exp.DataType.Type.STRUCT,
        expressions=[
            exp.ColumnDef(this=exp.to_identifier(k), kind=v) for k, v in columns_to_types.items()
        ],
        nested=True,
    )


def type_is_known(d_type: t.Union[exp.DataType, exp.ColumnDef]) -> bool:
    """Checks that a given column type is known and not NULL."""
    if isinstance(d_type, exp.ColumnDef):
        if not d_type.kind:
            return False
        d_type = d_type.kind
    if isinstance(d_type, exp.DataTypeParam):
        return True
    if d_type.is_type(exp.DataType.Type.UNKNOWN, exp.DataType.Type.NULL):
        return False
    if d_type.expressions:
        return all(type_is_known(expression) for expression in d_type.expressions)
    return True


def columns_to_types_all_known(columns_to_types: t.Dict[str, exp.DataType]) -> bool:
    """Checks that all column types are known and not NULL."""
    return all(type_is_known(expression) for expression in columns_to_types.values())


class Verbosity(IntEnum):
    """Verbosity levels for SQLMesh output."""

    DEFAULT = 0
    VERBOSE = 1
    VERY_VERBOSE = 2

    @property
    def is_default(self) -> bool:
        return self == Verbosity.DEFAULT

    @property
    def is_verbose(self) -> bool:
        return self == Verbosity.VERBOSE

    @property
    def is_very_verbose(self) -> bool:
        return self == Verbosity.VERY_VERBOSE


class CompletionStatus(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NOTHING_TO_DO = "nothing_to_do"

    @property
    def is_success(self) -> bool:
        return self == CompletionStatus.SUCCESS

    @property
    def is_failure(self) -> bool:
        return self == CompletionStatus.FAILURE

    @property
    def is_nothing_to_do(self) -> bool:
        return self == CompletionStatus.NOTHING_TO_DO


def to_snake_case(name: str) -> str:
    return "".join(
        f"_{c.lower()}" if c.isupper() and idx != 0 else c.lower() for idx, c in enumerate(name)
    )


class JobType(Enum):
    PLAN = "SQLMESH_PLAN"
    RUN = "SQLMESH_RUN"


@dataclass(frozen=True)
class CorrelationId:
    """ID that is added to each query in order to identify the job that created it."""

    job_type: JobType
    job_id: str

    def __str__(self) -> str:
        return f"{self.job_type.value}: {self.job_id}"

    @classmethod
    def from_plan_id(cls, plan_id: str) -> CorrelationId:
        return CorrelationId(JobType.PLAN, plan_id)


def get_source_columns_to_types(
    columns_to_types: t.Dict[str, exp.DataType],
    source_columns: t.Optional[t.List[str]],
) -> t.Dict[str, exp.DataType]:
    source_column_lookup = set(source_columns) if source_columns else None
    return {
        k: v
        for k, v in columns_to_types.items()
        if not source_column_lookup or k in source_column_lookup
    }
