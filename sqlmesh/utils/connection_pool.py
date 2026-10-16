import abc
import logging
import typing as t
from collections import defaultdict
from threading import Lock, get_ident
from threading import enumerate as enumerate_threads

from sqlmesh.utils.errors import SQLMeshError

logger = logging.getLogger(__name__)

_BROKEN_TRANSACTION_MESSAGE = "The connection was closed while a transaction was in progress, so the transaction was rolled back."


class ConnectionPool(abc.ABC):
    @abc.abstractmethod
    def get_cursor(self) -> t.Any:
        """Returns cached cursor instance.

        Automatically creates a new instance if one is not available.

        Returns:
            A cursor instance.
        """

    @abc.abstractmethod
    def get(self) -> t.Any:
        """Returns cached connection instance.

        Automatically opens a new connection if one is not available.

        Returns:
            A connection instance.
        """

    @abc.abstractmethod
    def get_attribute(self, key: str) -> t.Optional[t.Any]:
        """Returns an attribute associated with the connection.

        Args:
            key: Attribute key.

        Returns:
            Attribute value or None if not found.
        """

    @abc.abstractmethod
    def set_attribute(self, key: str, value: t.Any) -> None:
        """Sets an attribute associated with the connection.

        Args:
            key: Attribute key.
            value: Attribute value.
        """

    @abc.abstractmethod
    def get_all_attributes(self, key: str) -> t.List[t.Any]:
        """Returns all attributes with the given key across all connections/threads.

        Args:
            key: Attribute key.

        Returns:
            List of attribute values from all connections/threads.
        """

    @abc.abstractmethod
    def begin(self) -> None:
        """Starts a new transaction."""

    @abc.abstractmethod
    def commit(self) -> None:
        """Commits the current transaction."""

    @abc.abstractmethod
    def rollback(self) -> None:
        """Rolls back the current transaction."""

    @property
    @abc.abstractmethod
    def is_transaction_active(self) -> bool:
        """Returns True if there is an active transaction and False otherwise."""

    @abc.abstractmethod
    def close_cursor(self) -> None:
        """Closes the current cursor instance if exists."""

    @abc.abstractmethod
    def close(self) -> None:
        """Closes the current connection instance if exists.

        Note: if there is a cursor instance available it will be closed as well.
        """

    @abc.abstractmethod
    def close_all(self, exclude_calling_thread: bool = False) -> None:
        """Closes all cached cursors and connections.

        Args:
            exclude_calling_thread: If set to True excludes cursors and connections associated
                with the calling thread.
        """


class _TransactionManagementMixin(ConnectionPool):
    def _do_begin(self) -> None:
        cursor = self.get_cursor()
        if hasattr(cursor, "begin"):
            cursor.begin()
        else:
            conn = self.get()
            if hasattr(conn, "begin"):
                conn.begin()

    def _do_commit(self) -> None:
        cursor = self.get_cursor()
        if hasattr(cursor, "commit"):
            cursor.commit()
        else:
            self.get().commit()

    def _do_rollback(self) -> None:
        cursor = self.get_cursor()
        if hasattr(cursor, "rollback"):
            cursor.rollback()
        else:
            self.get().rollback()


class _ThreadLocalBase(_TransactionManagementMixin):
    def __init__(
        self,
        connection_factory: t.Callable[[], t.Any],
        cursor_init: t.Optional[t.Callable[[t.Any], None]] = None,
    ):
        self._connection_factory = connection_factory
        self._thread_cursors: t.Dict[t.Hashable, t.Any] = {}
        self._thread_transactions: t.Set[t.Hashable] = set()
        self._thread_broken_transactions: t.Set[t.Hashable] = set()
        self._thread_attributes: t.Dict[t.Hashable, t.Dict[str, t.Any]] = defaultdict(dict)
        self._thread_cursors_lock = Lock()
        self._thread_transactions_lock = Lock()
        self._cursor_init = cursor_init

    def get_cursor(self) -> t.Any:
        thread_id = get_ident()
        self._raise_if_transaction_broken(thread_id)
        with self._thread_cursors_lock:
            if thread_id not in self._thread_cursors:
                self._thread_cursors[thread_id] = self.get().cursor()
                if self._cursor_init:
                    self._cursor_init(self._thread_cursors[thread_id])
            return self._thread_cursors[thread_id]

    def get_attribute(self, key: str) -> t.Optional[t.Any]:
        thread_id = get_ident()
        return self._thread_attributes[thread_id].get(key)

    def set_attribute(self, key: str, value: t.Any) -> None:
        thread_id = get_ident()
        self._thread_attributes[thread_id][key] = value

    def get_all_attributes(self, key: str) -> t.List[t.Any]:
        """Returns all attributes with the given key across all threads."""
        return [
            thread_attrs[key]
            for thread_attrs in self._thread_attributes.values()
            if key in thread_attrs
        ]

    def begin(self) -> None:
        self._do_begin()
        with self._thread_transactions_lock:
            self._thread_transactions.add(get_ident())

    def commit(self) -> None:
        thread_id = get_ident()
        if self._is_transaction_broken(thread_id):
            self._discard_transaction(thread_id)
            raise SQLMeshError(_BROKEN_TRANSACTION_MESSAGE)
        self._do_commit()
        self._discard_transaction(thread_id)

    def rollback(self) -> None:
        thread_id = get_ident()
        try:
            if not self._is_transaction_broken(thread_id):
                self._do_rollback()
        finally:
            self._discard_transaction(thread_id)

    @property
    def is_transaction_active(self) -> bool:
        with self._thread_transactions_lock:
            return get_ident() in self._thread_transactions

    def close_cursor(self) -> None:
        thread_id = get_ident()
        with self._thread_cursors_lock:
            if thread_id in self._thread_cursors:
                _try_close(self._thread_cursors[thread_id], "cursor")
                self._thread_cursors.pop(thread_id)

    def _discard_transaction(self, thread_id: t.Hashable) -> None:
        with self._thread_transactions_lock:
            self._thread_transactions.discard(thread_id)
            self._thread_broken_transactions.discard(thread_id)

    def _close_transaction(self, thread_id: t.Hashable) -> None:
        # The statements another thread has run in its transaction are rolled back when its
        # connection is closed, so it must not continue the transaction on a new connection, where
        # its remaining statements would be committed without them. The transaction is kept active
        # but broken until the thread rolls it back. A finished thread can't continue it, and its
        # ident may be reused by a new thread, so its transaction is discarded instead.
        with self._thread_transactions_lock:
            if (
                thread_id != get_ident()
                and thread_id in self._thread_transactions
                and any(thread.ident == thread_id for thread in enumerate_threads())
            ):
                self._thread_broken_transactions.add(thread_id)
                return
        self._discard_transaction(thread_id)

    def _is_transaction_broken(self, thread_id: t.Hashable) -> bool:
        with self._thread_transactions_lock:
            return thread_id in self._thread_broken_transactions

    def _raise_if_transaction_broken(self, thread_id: t.Hashable) -> None:
        if self._is_transaction_broken(thread_id):
            raise SQLMeshError(_BROKEN_TRANSACTION_MESSAGE)


class ThreadLocalConnectionPool(_ThreadLocalBase):
    def __init__(
        self,
        connection_factory: t.Callable[[], t.Any],
        cursor_init: t.Optional[t.Callable[[t.Any], None]] = None,
    ):
        super().__init__(connection_factory, cursor_init)
        self._thread_connections: t.Dict[t.Hashable, t.Any] = {}
        self._thread_connections_lock = Lock()

    def get(self) -> t.Any:
        thread_id = get_ident()
        self._raise_if_transaction_broken(thread_id)
        with self._thread_connections_lock:
            if thread_id not in self._thread_connections:
                self._thread_connections[thread_id] = self._connection_factory()
            return self._thread_connections[thread_id]

    def close(self) -> None:
        thread_id = get_ident()
        with self._thread_cursors_lock, self._thread_connections_lock:
            if thread_id in self._thread_connections:
                _try_close(self._thread_connections[thread_id], "connection")
                self._thread_connections.pop(thread_id)
                self._thread_cursors.pop(thread_id, None)
                self._discard_transaction(thread_id)
            self._thread_attributes.pop(thread_id, None)

    def close_all(self, exclude_calling_thread: bool = False) -> None:
        calling_thread_id = get_ident()
        with self._thread_cursors_lock, self._thread_connections_lock:
            for thread_id, connection in self._thread_connections.copy().items():
                if not exclude_calling_thread or thread_id != calling_thread_id:
                    _try_close(connection, "connection")
                    self._thread_connections.pop(thread_id)
                    self._thread_cursors.pop(thread_id, None)
                    self._close_transaction(thread_id)

            self._thread_attributes.clear()


class ThreadLocalSharedConnectionPool(_ThreadLocalBase):
    def __init__(
        self,
        connection_factory: t.Callable[[], t.Any],
        cursor_init: t.Optional[t.Callable[[t.Any], None]] = None,
    ):
        super().__init__(connection_factory, cursor_init)
        self._connection: t.Optional[t.Any] = None
        self._connection_lock = Lock()

    def get(self) -> t.Any:
        self._raise_if_transaction_broken(get_ident())
        with self._connection_lock:
            if self._connection is None:
                self._connection = self._connection_factory()
            return self._connection

    def close(self) -> None:
        thread_id = get_ident()
        with self._thread_cursors_lock, self._connection_lock:
            if thread_id in self._thread_cursors:
                _try_close(self._thread_cursors[thread_id], "cursor")
                self._thread_cursors.pop(thread_id)
            self._discard_transaction(thread_id)
            self._thread_attributes.pop(thread_id, None)

    def close_all(self, exclude_calling_thread: bool = False) -> None:
        calling_thread_id = get_ident()
        with self._thread_cursors_lock, self._connection_lock:
            for thread_id, cursor in self._thread_cursors.copy().items():
                if not exclude_calling_thread or thread_id != calling_thread_id:
                    _try_close(cursor, "cursor")
                    self._thread_cursors.pop(thread_id)
                    self._close_transaction(thread_id)
                self._thread_attributes.pop(thread_id, None)

            if not exclude_calling_thread:
                _try_close(self._connection, "connection")
                self._connection = None


class SingletonConnectionPool(_TransactionManagementMixin):
    def __init__(
        self,
        connection_factory: t.Callable[[], t.Any],
        cursor_init: t.Optional[t.Callable[[t.Any], None]] = None,
    ):
        self._connection_factory = connection_factory
        self._connection: t.Optional[t.Any] = None
        self._cursor: t.Optional[t.Any] = None
        self._attributes: t.Dict[str, t.Any] = {}
        self._is_transaction_active: bool = False
        self._cursor_init = cursor_init

    def get_cursor(self) -> t.Any:
        if not self._cursor:
            self._cursor = self.get().cursor()
            if self._cursor_init:
                self._cursor_init(self._cursor)
        return self._cursor

    def get(self) -> t.Any:
        if not self._connection:
            self._connection = self._connection_factory()
        return self._connection

    def get_attribute(self, key: str) -> t.Optional[t.Any]:
        return self._attributes.get(key)

    def set_attribute(self, key: str, value: t.Any) -> None:
        self._attributes[key] = value

    def get_all_attributes(self, key: str) -> t.List[t.Any]:
        """Returns all attributes with the given key (single-threaded pool has at most one)."""
        value = self._attributes.get(key)
        return [value] if value is not None else []

    def begin(self) -> None:
        self._do_begin()
        self._is_transaction_active = True

    def commit(self) -> None:
        self._do_commit()
        self._is_transaction_active = False

    def rollback(self) -> None:
        self._do_rollback()
        self._is_transaction_active = False

    @property
    def is_transaction_active(self) -> bool:
        return self._is_transaction_active

    def close_cursor(self) -> None:
        _try_close(self._cursor, "cursor")
        self._cursor = None

    def close(self) -> None:
        _try_close(self._connection, "connection")
        self._connection = None
        self._cursor = None
        self._is_transaction_active = False
        self._attributes.clear()

    def close_all(self, exclude_calling_thread: bool = False) -> None:
        if not exclude_calling_thread:
            self.close()


def create_connection_pool(
    connection_factory: t.Callable[[], t.Any],
    multithreaded: bool,
    shared_connection: bool = False,
    cursor_init: t.Optional[t.Callable[[t.Any], None]] = None,
) -> ConnectionPool:
    pool_class = (
        ThreadLocalSharedConnectionPool
        if multithreaded and shared_connection
        else ThreadLocalConnectionPool
        if multithreaded
        else SingletonConnectionPool
    )
    return pool_class(connection_factory, cursor_init=cursor_init)


def _try_close(closeable: t.Any, kind: str) -> None:
    if closeable is None:
        return
    try:
        closeable.close()
    except Exception:
        logger.exception("Failed to close %s", kind)
