from __future__ import annotations

import gzip
import logging
import pickle
import shutil
import typing as t
from pathlib import Path

from sqlglot import __version__ as SQLGLOT_VERSION

from sqlmesh.utils import sanitize_name
from sqlmesh.utils.date import to_datetime
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.windows import IS_WINDOWS, fix_windows_path

logger = logging.getLogger(__name__)

T = t.TypeVar("T")


SQLGLOT_VERSION_TUPLE = tuple(SQLGLOT_VERSION.split("."))
SQLGLOT_MAJOR_VERSION = SQLGLOT_VERSION_TUPLE[0]
SQLGLOT_MINOR_VERSION = SQLGLOT_VERSION_TUPLE[1]


class FileCache(t.Generic[T]):
    """Generic file-based cache implementation.

    Args:
        path: The path to the cache folder.
        entry_class: The type of cached entries.
        prefix: The prefix shared between all entries to distinguish them from other entries
            stored in the same cache folder.
    """

    def __init__(self, path: Path, prefix: t.Optional[str] = None):
        self._path = path / prefix if prefix else path

        from sqlmesh.core.state_sync.base import SCHEMA_VERSION

        try:
            from sqlmesh._version import __version_tuple__

            major, minor = __version_tuple__[0], __version_tuple__[1]
        except ImportError:
            major, minor = 0, 0

        self._cache_version = "_".join(
            [
                str(major),
                str(minor),
                SQLGLOT_MAJOR_VERSION,
                SQLGLOT_MINOR_VERSION,
                str(SCHEMA_VERSION),
            ]
        )

        threshold = to_datetime("1 week ago").timestamp()
        # delete all old cache files
        for file in self._path.glob("*"):
            if IS_WINDOWS:
                # the file.stat() call below will fail on windows if the :file name is longer than 260 chars
                file = fix_windows_path(file)

            try:
                stat_result = file.stat()
                if not file.stem.startswith(self._cache_version) or stat_result.st_atime < threshold:
                    file.unlink(missing_ok=True)
            except FileNotFoundError:
                # File was deleted between glob() and stat() — skip stale cache entries gracefully
                continue

    def get_or_load(self, name: str, entry_id: str = "", *, loader: t.Callable[[], T]) -> T:
        """Returns an existing cached entry or loads and caches a new one.

        Args:
            name: The name of the entry.
            entry_id: The unique entry identifier. Used for cache invalidation.
            loader: Used to load a new entry when no cached instance was found.

        Returns:
            The entry.
        """
        cached_entry = self.get(name, entry_id)
        if cached_entry is not None:
            return cached_entry

        loaded_entry = loader()
        self.put(name, entry_id, value=loaded_entry)
        return loaded_entry

    def get(self, name: str, entry_id: str = "") -> t.Optional[T]:
        """Returns a cached entry if exists.

        Args:
            name: The name of the entry.
            entry_id: The unique entry identifier. Used for cache invalidation.

        Returns:
            The entry or None if no entry was found in the cache.
        """
        cache_entry_path = self._cache_entry_path(name, entry_id)
        if cache_entry_path.exists():
            with gzip.open(cache_entry_path, "rb") as fd:
                try:
                    return pickle.load(fd)
                except Exception as ex:
                    logger.warning("Failed to load a cache entry '%s': %s", name, ex)

        return None

    def put(self, name: str, entry_id: str = "", *, value: T) -> None:
        """Stores the given value in the cache.

        Args:
            name: The name of the entry.
            entry_id: The unique entry identifier. Used for cache invalidation.
            value: The value to store in the cache.
        """
        self._path.mkdir(parents=True, exist_ok=True)
        if not self._path.is_dir():
            raise SQLMeshError(f"Cache path '{self._path}' is not a directory.")

        with gzip.open(self._cache_entry_path(name, entry_id), "wb", compresslevel=1) as fd:
            pickle.dump(value, fd)

    def exists(self, name: str, entry_id: str = "") -> bool:
        """Returns true if the cache entry with the given name and ID exists, false otherwise.

        Args:
            name: The name of the entry.
            entry_id: The unique entry identifier. Used for cache invalidation.
        """
        return self._cache_entry_path(name, entry_id).exists()

    def clear(self) -> None:
        try:
            shutil.rmtree(str(self._path.absolute()))
        except Exception:
            pass

    def _cache_entry_path(self, name: str, entry_id: str = "") -> Path:
        entry_file_name = "__".join(p for p in (self._cache_version, name, entry_id) if p)
        full_path = self._path / sanitize_name(entry_file_name, include_unicode=True)
        if IS_WINDOWS:
            # handle paths longer than 260 chars
            full_path = fix_windows_path(full_path)
        return full_path
