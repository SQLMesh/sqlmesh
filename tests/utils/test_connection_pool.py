import typing as t
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from pytest_mock.plugin import MockerFixture

from sqlmesh.utils.connection_pool import (
    SingletonConnectionPool,
    ThreadLocalConnectionPool,
    ThreadLocalSharedConnectionPool,
)
from sqlmesh.utils.errors import SQLMeshError


def test_singleton_connection_pool_get(mocker: MockerFixture):
    cursor_mock = mocker.Mock()
    connection_mock = mocker.Mock()
    connection_mock.cursor.return_value = cursor_mock
    connection_factory_mock = mocker.Mock(return_value=connection_mock)

    pool = SingletonConnectionPool(connection_factory_mock)

    assert pool.get_cursor() == cursor_mock
    assert pool.get_cursor() == cursor_mock
    assert pool.get() == connection_mock
    assert pool.get() == connection_mock

    connection_factory_mock.assert_called_once()
    connection_mock.cursor.assert_called_once()


def test_singleton_connection_pool_close(mocker: MockerFixture):
    cursor_mock = mocker.Mock()
    connection_mock = mocker.Mock()
    connection_mock.cursor.return_value = cursor_mock
    connection_factory_mock = mocker.Mock(return_value=connection_mock)

    pool = SingletonConnectionPool(connection_factory_mock)

    pool.close()
    pool.close_cursor()
    pool.close_all()
    pool.close_all(exclude_calling_thread=True)

    assert pool.get_cursor() == cursor_mock
    pool.close_cursor()

    assert pool.get_cursor() == cursor_mock
    pool.close()

    assert pool.get_cursor() == cursor_mock
    pool.close_all()

    assert pool.get_cursor() == cursor_mock
    pool.close_all(exclude_calling_thread=True)

    assert connection_mock.close.call_count == 2
    assert connection_mock.cursor.call_count == 4
    assert cursor_mock.close.call_count == 1
    assert connection_factory_mock.call_count == 3


def test_singleton_connection_pool_transaction(mocker: MockerFixture):
    cursor_mock = mocker.Mock()
    connection_mock = mocker.Mock()
    connection_mock.cursor.return_value = cursor_mock
    connection_factory_mock = mocker.Mock(return_value=connection_mock)

    pool = SingletonConnectionPool(connection_factory_mock)

    assert not pool.is_transaction_active

    pool.begin()
    assert pool.is_transaction_active

    pool.commit()
    assert not pool.is_transaction_active

    pool.begin()
    pool.rollback()
    assert not pool.is_transaction_active

    pool.begin()
    pool.close()
    assert not pool.is_transaction_active

    pool.begin()
    pool.close_all()
    assert not pool.is_transaction_active

    assert cursor_mock.begin.call_count == 4
    assert cursor_mock.commit.call_count == 1
    assert cursor_mock.rollback.call_count == 1


def test_thread_local_connection_pool(mocker: MockerFixture):
    cursor_mock_thread_one = mocker.Mock()
    connection_mock_thread_one = mocker.Mock()
    connection_mock_thread_one.cursor.return_value = cursor_mock_thread_one

    cursor_mock_thread_two = mocker.Mock()
    connection_mock_thread_two = mocker.Mock()
    connection_mock_thread_two.cursor.return_value = cursor_mock_thread_two

    test_thread_id = get_ident()

    def connection_factory():
        return (
            connection_mock_thread_one
            if get_ident() == test_thread_id
            else connection_mock_thread_two
        )

    connection_factory_mock = mocker.Mock(side_effect=connection_factory)
    pool = ThreadLocalConnectionPool(connection_factory_mock)

    def thread():
        assert pool.get_cursor() == cursor_mock_thread_two
        assert pool.get_cursor() == cursor_mock_thread_two
        assert pool.get() == connection_mock_thread_two
        assert pool.get() == connection_mock_thread_two

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(thread).result()

    assert pool.get_cursor() == cursor_mock_thread_one
    assert pool.get_cursor() == cursor_mock_thread_one
    assert pool.get() == connection_mock_thread_one
    assert pool.get() == connection_mock_thread_one

    assert len(pool._thread_connections) == 2
    assert len(pool._thread_cursors) == 2

    pool.close_all(exclude_calling_thread=True)

    assert len(pool._thread_connections) == 1
    assert len(pool._thread_cursors) == 1
    assert test_thread_id in pool._thread_connections
    assert test_thread_id in pool._thread_cursors

    pool.close_cursor()
    pool.close()

    assert pool.get_cursor() == cursor_mock_thread_one

    pool.close_all()

    assert connection_factory_mock.call_count == 3

    assert cursor_mock_thread_one.close.call_count == 1
    assert connection_mock_thread_one.cursor.call_count == 2
    assert connection_mock_thread_one.close.call_count == 2

    assert connection_mock_thread_two.cursor.call_count == 1
    assert connection_mock_thread_two.close.call_count == 1


def test_thread_local_connection_pool_transaction(mocker: MockerFixture):
    cursor_mock_thread_one = mocker.Mock()
    connection_mock_thread_one = mocker.Mock()
    connection_mock_thread_one.cursor.return_value = cursor_mock_thread_one

    cursor_mock_thread_two = mocker.Mock()
    connection_mock_thread_two = mocker.Mock()
    connection_mock_thread_two.cursor.return_value = cursor_mock_thread_two

    test_thread_id = get_ident()

    def connection_factory():
        return (
            connection_mock_thread_one
            if get_ident() == test_thread_id
            else connection_mock_thread_two
        )

    connection_factory_mock = mocker.Mock(side_effect=connection_factory)
    pool = ThreadLocalConnectionPool(connection_factory_mock)

    def thread():
        pool.begin()
        assert pool.is_transaction_active

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(thread).result()

    pool.begin()
    assert pool.is_transaction_active

    assert len(pool._thread_transactions) == 2

    pool.commit()
    assert not pool.is_transaction_active
    assert len(pool._thread_transactions) == 1

    pool.begin()
    pool.rollback()
    assert not pool.is_transaction_active
    assert len(pool._thread_transactions) == 1

    pool.begin()
    pool.close()
    assert not pool.is_transaction_active
    assert len(pool._thread_transactions) == 1

    pool.close_all()
    assert not pool._thread_transactions

    assert cursor_mock_thread_one.begin.call_count == 3
    assert cursor_mock_thread_one.commit.call_count == 1
    assert cursor_mock_thread_one.rollback.call_count == 1

    assert cursor_mock_thread_two.begin.call_count == 1


def test_thread_local_connection_pool_attributes(mocker: MockerFixture):
    pool = ThreadLocalConnectionPool(connection_factory=lambda: mocker.Mock())

    pool.set_attribute("foo", "bar")
    current_threadid = get_ident()

    def _in_thread(pool: ThreadLocalConnectionPool):
        assert get_ident() != current_threadid
        pool.set_attribute("foo", "baz")

    with ThreadPoolExecutor() as executor:
        future = executor.submit(_in_thread, pool)
        assert not future.exception()

    assert pool.get_all_attributes("foo") == ["bar", "baz"]
    assert pool.get_attribute("foo") == "bar"

    pool.close_all()

    assert pool.get_all_attributes("foo") == []
    assert pool.get_attribute("foo") is None


def test_thread_local_shared_connection_pool(mocker: MockerFixture):
    cursor_mock_thread_one = mocker.Mock()
    cursor_mock_thread_two = mocker.Mock()
    connection_mock = mocker.Mock()
    connection_mock.cursor.side_effect = [
        cursor_mock_thread_one,
        cursor_mock_thread_two,
        cursor_mock_thread_one,
    ]

    test_thread_id = get_ident()

    connection_factory_mock = mocker.Mock(return_value=connection_mock)
    pool = ThreadLocalSharedConnectionPool(connection_factory_mock)

    assert pool.get_cursor() == cursor_mock_thread_one
    assert pool.get_cursor() == cursor_mock_thread_one
    assert pool.get() == connection_mock
    assert pool.get() == connection_mock

    def thread():
        assert pool.get_cursor() == cursor_mock_thread_two
        assert pool.get_cursor() == cursor_mock_thread_two
        assert pool.get() == connection_mock
        assert pool.get() == connection_mock

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(thread).result()

    assert pool._connection is not None
    assert len(pool._thread_cursors) == 2

    pool.close_all(exclude_calling_thread=True)

    assert pool._connection is not None
    assert len(pool._thread_cursors) == 1
    assert test_thread_id in pool._thread_cursors

    pool.close_cursor()
    pool.close()

    assert pool.get_cursor() == cursor_mock_thread_one

    pool.close_all()

    assert connection_factory_mock.call_count == 1

    assert cursor_mock_thread_one.close.call_count == 2
    assert connection_mock.cursor.call_count == 3
    assert connection_mock.close.call_count == 1


def test_thread_local_shared_connection_pool_close(mocker: MockerFixture):
    connection_mock = mocker.Mock()
    cursor_mock = mocker.Mock()
    connection_mock.cursor.return_value = cursor_mock

    connection_factory_mock = mocker.Mock(return_value=connection_mock)
    pool = ThreadLocalSharedConnectionPool(connection_factory_mock)

    # First time we get a connection
    pool.get()
    pool.get()
    pool.get_cursor()
    pool.get_cursor()

    # This shouldn't close the connection, only the cursor
    pool.close()
    pool.get()
    pool.get()
    pool.get_cursor()

    pool.get_cursor()
    # This shouldn't close the connection either
    pool.close_all(exclude_calling_thread=True)

    pool.get()
    pool.get()
    # Now this should close the connection
    pool.close_all()

    # Re-open the connection
    pool.get()
    pool.get()
    # Close it again
    pool.close_all()

    assert cursor_mock.close.call_count == 2
    assert connection_factory_mock.call_count == 2
    assert connection_mock.close.call_count == 2


@pytest.mark.parametrize("pool_class", [ThreadLocalConnectionPool, ThreadLocalSharedConnectionPool])
@pytest.mark.parametrize("exclude_calling_thread", [True, False])
def test_thread_local_pool_close_all_breaks_transactions_of_other_running_threads(
    mocker: MockerFixture, pool_class: t.Type, exclude_calling_thread: bool
):
    connection_factory_mock = mocker.Mock(side_effect=lambda: mocker.Mock())
    pool = pool_class(connection_factory_mock)
    in_transaction = Event()
    closed = Event()

    def thread() -> None:
        pool.begin()
        cursor = pool.get_cursor()
        in_transaction.set()
        assert closed.wait(timeout=10)

        # The rest of the transaction must not run on a new connection, where it would be committed
        # without the statements that ran on the closed one.
        assert pool.is_transaction_active
        with pytest.raises(SQLMeshError, match="closed while a transaction was in progress"):
            pool.get_cursor()
        with pytest.raises(SQLMeshError, match="closed while a transaction was in progress"):
            pool.get()

        connections_opened = connection_factory_mock.call_count
        pool.rollback()
        assert connection_factory_mock.call_count == connections_opened
        cursor.rollback.assert_not_called()
        assert not pool.is_transaction_active

        pool.get_cursor()

    pool.begin()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(thread)
        assert in_transaction.wait(timeout=10)
        pool.close_all(exclude_calling_thread=exclude_calling_thread)
        closed.set()
        future.result()

    assert pool.is_transaction_active == exclude_calling_thread
    pool.get_cursor()


@pytest.mark.parametrize("pool_class", [ThreadLocalConnectionPool, ThreadLocalSharedConnectionPool])
def test_thread_local_pool_failed_rollback_ends_transaction(
    mocker: MockerFixture, pool_class: t.Type
):
    pool = pool_class(lambda: mocker.Mock())
    pool.begin()
    pool.get_cursor().rollback.side_effect = RuntimeError("connection closed")

    with pytest.raises(RuntimeError, match="connection closed"):
        pool.rollback()
    assert not pool.is_transaction_active


@pytest.mark.parametrize("pool_class", [ThreadLocalConnectionPool, ThreadLocalSharedConnectionPool])
def test_thread_local_pool_commit_of_broken_transaction_fails(
    mocker: MockerFixture, pool_class: t.Type
):
    pool = pool_class(lambda: mocker.Mock())
    in_transaction = Event()
    closed = Event()

    def thread() -> None:
        pool.begin()
        cursor = pool.get_cursor()
        in_transaction.set()
        assert closed.wait(timeout=10)

        with pytest.raises(SQLMeshError, match="closed while a transaction was in progress"):
            pool.commit()
        cursor.commit.assert_not_called()
        assert not pool.is_transaction_active

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(thread)
        assert in_transaction.wait(timeout=10)
        pool.close_all(exclude_calling_thread=True)
        closed.set()
        future.result()
