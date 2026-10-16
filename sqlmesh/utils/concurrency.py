import typing as t
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from threading import Condition, Lock
from types import TracebackType

from sqlmesh.core.snapshot import SnapshotId, SnapshotInfoLike
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.errors import ConfigError, SQLMeshError

H = t.TypeVar("H", bound=t.Hashable)
S = t.TypeVar("S", bound=SnapshotInfoLike)
A = t.TypeVar("A")
R = t.TypeVar("R")


class NodeExecutionFailedError(t.Generic[H], SQLMeshError):
    def __init__(self, node: H):
        self.node = node
        super().__init__(f"Execution failed for node {node}")


class ConcurrentDAGExecutor(t.Generic[H]):
    """Concurrently traverses the given DAG in topological order while applying a function to each node.

    If `raise_on_error` is set to False maintains a state of execution errors as well as of skipped nodes.

    Args:
        dag: The target DAG.
        fn: The function that will be applied concurrently to each snapshot.
        tasks_num: The number of concurrent tasks.
        raise_on_error: If set to True raises an exception on a first encountered error,
            otherwises returns a tuple which contains a list of failed nodes and a list of
            skipped nodes.
        on_interrupt: Called when the calling thread is interrupted, to stop the running tasks
            before they're waited for.
    """

    def __init__(
        self,
        dag: DAG[H],
        fn: t.Callable[[H], None],
        tasks_num: int,
        raise_on_error: bool,
        on_interrupt: t.Optional[t.Callable[[], None]] = None,
    ):
        self.dag = dag
        self.fn = fn
        self.tasks_num = tasks_num
        self.raise_on_error = raise_on_error
        self.on_interrupt = on_interrupt

        self._init_state()

    def run(self) -> t.Tuple[t.List[NodeExecutionFailedError[H]], t.List[H]]:
        """Runs the executor.

        Raises:
            NodeExecutionFailedError if `raise_on_error` was set to True and execution fails for any snapshot.

        Returns:
            A pair which contains a list of node errors and a list of skipped nodes.
        """
        if self._finished_future.done():
            self._init_state()

        with _StoppableThreadPool(self.tasks_num, self.on_interrupt) as pool:
            with self._unprocessed_nodes_lock:
                self._submit_next_nodes(pool)
            self._finished_future.result()
        return self._node_errors, self._skipped_nodes

    def _process_node(self, node: H, executor: Executor) -> None:
        try:
            self.fn(node)

            with self._unprocessed_nodes_lock:
                self._unprocessed_nodes_num -= 1
                self._submit_next_nodes(executor, node)
        except Exception as ex:
            error = NodeExecutionFailedError(node)
            error.__cause__ = ex

            if self.raise_on_error:
                self._finished_future.set_exception(error)
                return

            with self._unprocessed_nodes_lock:
                self._unprocessed_nodes_num -= 1
                self._node_errors.append(error)
                self._skip_next_nodes(node)

    def _submit_next_nodes(self, executor: Executor, processed_node: t.Optional[H] = None) -> None:
        if not self._unprocessed_nodes_num:
            self._finished_future.set_result(None)
            return

        submitted_nodes = []
        for next_node, deps in self._unprocessed_nodes.items():
            if processed_node:
                deps.discard(processed_node)
            if not deps:
                submitted_nodes.append(next_node)

        for submitted_node in submitted_nodes:
            self._unprocessed_nodes.pop(submitted_node)
            executor.submit(self._process_node, submitted_node, executor)

    def _skip_next_nodes(self, parent: H) -> None:
        if not self._unprocessed_nodes_num:
            self._finished_future.set_result(None)
            return

        skipped_nodes = {node for node, deps in self._unprocessed_nodes.items() if parent in deps}

        while skipped_nodes:
            self._skipped_nodes.extend(skipped_nodes)

            for skipped_node in skipped_nodes:
                self._unprocessed_nodes_num -= 1
                self._unprocessed_nodes.pop(skipped_node)

            skipped_nodes = {
                node
                for node, deps in self._unprocessed_nodes.items()
                if skipped_nodes.intersection(deps)
            }

        if not self._unprocessed_nodes_num:
            self._finished_future.set_result(None)

    def _init_state(self) -> None:
        self._unprocessed_nodes = self.dag.graph
        self._unprocessed_nodes_num = len(self._unprocessed_nodes)
        self._unprocessed_nodes_lock = Lock()
        self._finished_future = Future()  # type: ignore

        self._node_errors: t.List[NodeExecutionFailedError[H]] = []
        self._skipped_nodes: t.List[H] = []


def concurrent_apply_to_snapshots(
    snapshots: t.Iterable[S],
    fn: t.Callable[[S], None],
    tasks_num: int,
    reverse_order: bool = False,
    raise_on_error: bool = True,
    on_interrupt: t.Optional[t.Callable[[], None]] = None,
) -> t.Tuple[t.List[NodeExecutionFailedError[SnapshotId]], t.List[SnapshotId]]:
    """Applies a function to the given collection of snapshots concurrently while
    preserving the topological order between snapshots.

    Args:
        snapshots: Target snapshots.
        fn: The function that will be applied concurrently to each snapshot.
        tasks_num: The number of concurrent tasks.
        reverse_order: Whether the order should be reversed. Default: False.
        raise_on_error: If set to True raises an exception on a first encountered error,
            otherwises returns a tuple which contains a list of failed nodes and a list of
            skipped nodes.
        on_interrupt: Called when the calling thread is interrupted, to stop the running tasks
            before they're waited for.

    Raises:
        NodeExecutionFailedError if `raise_on_error` is set to True and execution fails for any snapshot.

    Returns:
        A pair which contains a list of errors and a list of skipped snapshot IDs.
    """
    snapshots_by_id = {s.snapshot_id: s for s in snapshots}

    dag: DAG[SnapshotId] = DAG[SnapshotId]()
    for snapshot in snapshots:
        dag.add(
            snapshot.snapshot_id,
            [p_sid for p_sid in snapshot.parents if p_sid in snapshots_by_id],
        )

    return concurrent_apply_to_dag(
        dag if not reverse_order else dag.reversed,
        lambda s_id: fn(snapshots_by_id[s_id]),
        tasks_num,
        raise_on_error=raise_on_error,
        on_interrupt=on_interrupt,
    )


def concurrent_apply_to_dag(
    dag: DAG[H],
    fn: t.Callable[[H], None],
    tasks_num: int,
    raise_on_error: bool = True,
    on_interrupt: t.Optional[t.Callable[[], None]] = None,
) -> t.Tuple[t.List[NodeExecutionFailedError[H]], t.List[H]]:
    """Applies a function to the given DAG concurrently while preserving the topological
    order between snapshots.

    Args:
        dag: The target DAG.
        fn: The function that will be applied concurrently to each snapshot.
        tasks_num: The number of concurrent tasks.
        raise_on_error: If set to True raises an exception on a first encountered error,
            otherwises returns a tuple which contains a list of failed nodes and a list of
            skipped nodes.
        on_interrupt: Called when the calling thread is interrupted, to stop the running tasks
            before they're waited for.

    Raises:
        NodeExecutionFailedError if `raise_on_error` is set to True and execution fails for any snapshot.

    Returns:
        A pair which contains a list of node errors and a list of skipped nodes.
    """
    if tasks_num <= 0:
        raise ConfigError(f"Invalid number of concurrent tasks {tasks_num}")

    if tasks_num == 1:
        return sequential_apply_to_dag(dag, fn, raise_on_error)

    return ConcurrentDAGExecutor(
        dag,
        fn,
        tasks_num,
        raise_on_error,
        on_interrupt,
    ).run()


def sequential_apply_to_dag(
    dag: DAG[H],
    fn: t.Callable[[H], None],
    raise_on_error: bool = True,
) -> t.Tuple[t.List[NodeExecutionFailedError[H]], t.List[H]]:
    dependencies = dag.graph

    node_errors: t.List[NodeExecutionFailedError[H]] = []
    skipped_nodes: t.List[H] = []

    failed_or_skipped_nodes: t.Set[H] = set()

    for node in dag.sorted:
        if not failed_or_skipped_nodes.isdisjoint(dependencies[node]):
            skipped_nodes.append(node)
            failed_or_skipped_nodes.add(node)
            continue

        try:
            fn(node)
        except Exception as ex:
            error = NodeExecutionFailedError(node)
            error.__cause__ = ex

            if raise_on_error:
                raise error

            node_errors.append(error)
            failed_or_skipped_nodes.add(node)

    return node_errors, skipped_nodes


def concurrent_apply_to_values(
    values: t.Sequence[A],
    fn: t.Callable[[A], R],
    tasks_num: int,
    on_interrupt: t.Optional[t.Callable[[], None]] = None,
) -> t.List[R]:
    """Applies a function to the given collection of values concurrently.

    Args:
        values: Target values.
        fn: The function that will be applied concurrently to each value.
        tasks_num: The number of concurrent tasks.
        on_interrupt: Called when the calling thread is interrupted, to stop the running tasks
            before they're waited for.

    Returns:
        A list of results.
    """
    if tasks_num == 1:
        return [fn(value) for value in values]

    futures: t.List[Future] = [Future() for _ in values]

    def _process_value(value: A, index: int) -> None:
        try:
            futures[index].set_result(fn(value))
        except Exception as ex:
            futures[index].set_exception(ex)

    with _StoppableThreadPool(tasks_num, on_interrupt) as pool:
        for index, value in enumerate(values):
            pool.submit(_process_value, value, index)

    return [f.result() for f in futures]


class _StoppableThreadPool(ThreadPoolExecutor):
    """A `ThreadPoolExecutor` that, like its parent, waits for its tasks on exit, but handles an
    interrupt of the calling thread (e.g. `KeyboardInterrupt`) differently.

    When interrupted, pending tasks are cancelled and `on_interrupt`, if set, is called to stop the
    running tasks (e.g. by cancelling the queries they're blocked on). The running tasks are then
    waited for before the interrupt propagates.

    The running tasks are counted rather than waited for by joining the pool's threads, because a
    join can't be relied on after an interrupt:
    - An interrupted `Thread.join` marks the thread as stopped even if it's still running, in
      CPython < 3.13 (https://github.com/python/cpython/issues/90882).
    - A thread whose start was interrupted isn't tracked by the pool. `ThreadPoolExecutor` starts
      a thread and only then adds it to the threads that `shutdown(wait=True)` joins:

          t.start()  # `start` waits for the thread to run, so an interrupt can land here
          self._threads.add(t)  # skipped, though the thread may already be running a task
    """

    def __init__(self, max_workers: int, on_interrupt: t.Optional[t.Callable[[], None]] = None):
        super().__init__(max_workers=max_workers)
        self._on_interrupt = on_interrupt
        self._tasks_condition = Condition()
        self._running_tasks = 0
        self._stopped = False

    def submit(self, fn: t.Callable[..., R], /, *args: t.Any, **kwargs: t.Any) -> Future:
        def run() -> t.Optional[R]:
            with self._tasks_condition:
                if self._stopped:
                    return None
                self._running_tasks += 1
            try:
                return fn(*args, **kwargs)
            finally:
                with self._tasks_condition:
                    self._running_tasks -= 1
                    self._tasks_condition.notify_all()

        return super().submit(run)

    def __exit__(
        self,
        exc_type: t.Optional[t.Type[BaseException]],
        exc_val: t.Optional[BaseException],
        exc_tb: t.Optional[TracebackType],
    ) -> None:
        if exc_type is None or issubclass(exc_type, Exception):
            try:
                self.shutdown(wait=True)
            except BaseException:
                self._stop()
                raise
        else:
            self._stop()

    def _stop(self) -> None:
        # Make sure no new task starts: the tasks still in the queue are cancelled, and a task that a
        # worker has already taken off the queue sees `_stopped` and skips its work.
        with self._tasks_condition:
            self._stopped = True
        self.shutdown(wait=False, cancel_futures=True)
        # Ask the running tasks to stop early (e.g. by cancelling their queries).
        if self._on_interrupt is not None:
            self._on_interrupt()
        # Wait for the running tasks to finish.
        with self._tasks_condition:
            self._tasks_condition.wait_for(lambda: self._running_tasks == 0)
