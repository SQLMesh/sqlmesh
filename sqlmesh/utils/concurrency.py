import typing as t
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from threading import Lock
from contextvars import Context

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
        context_factory: Optional factory called at each run admission. Each node runs
            in a separate copy of that context, including serial execution. By default
            no context is captured or copied.
    """

    def __init__(
        self,
        dag: DAG[H],
        fn: t.Callable[[H], None],
        tasks_num: int,
        raise_on_error: bool,
        context_factory: t.Optional[t.Callable[[], Context]] = None,
    ):
        self.dag = dag
        self.fn = fn
        self.tasks_num = tasks_num
        self.raise_on_error = raise_on_error
        self.context_factory = context_factory
        self._context: t.Optional[Context] = None

        self._init_state()

    def run(self) -> t.Tuple[t.List[NodeExecutionFailedError[H]], t.List[H]]:
        """Runs the executor.

        Raises:
            NodeExecutionFailedError if `raise_on_error` was set to True and execution fails for any snapshot.

        Returns:
            A pair which contains a list of node errors and a list of skipped nodes.
        """
        self._context = self.context_factory() if self.context_factory else None
        if self._finished_future.done():
            self._init_state()

        try:
            with ThreadPoolExecutor(max_workers=self.tasks_num) as pool:
                with self._unprocessed_nodes_lock:
                    self._submit_next_nodes(pool)
                self._finished_future.result()
            return self._node_errors, self._skipped_nodes
        finally:
            self._context = None

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
            if self._context is None:
                executor.submit(self._process_node, submitted_node, executor)
            else:
                executor.submit(
                    self._context.copy().run, self._process_node, submitted_node, executor
                )

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
    context_factory: t.Optional[t.Callable[[], Context]] = None,
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
        context_factory: Optional factory called at each run admission. Each node runs
            in a separate copy of that context, including serial execution. By default
            no context is captured or copied.

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
        context_factory=context_factory,
    )


def concurrent_apply_to_dag(
    dag: DAG[H],
    fn: t.Callable[[H], None],
    tasks_num: int,
    raise_on_error: bool = True,
    context_factory: t.Optional[t.Callable[[], Context]] = None,
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
        context_factory: Optional factory called at each run admission. Each node runs
            in a separate copy of that context, including serial execution. By default
            no context is captured or copied.

    Raises:
        NodeExecutionFailedError if `raise_on_error` is set to True and execution fails for any snapshot.

    Returns:
        A pair which contains a list of node errors and a list of skipped nodes.
    """
    if tasks_num <= 0:
        raise ConfigError(f"Invalid number of concurrent tasks {tasks_num}")

    if tasks_num == 1:
        return sequential_apply_to_dag(dag, fn, raise_on_error, context_factory)

    return ConcurrentDAGExecutor(
        dag,
        fn,
        tasks_num,
        raise_on_error,
        context_factory,
    ).run()


def sequential_apply_to_dag(
    dag: DAG[H],
    fn: t.Callable[[H], None],
    raise_on_error: bool = True,
    context_factory: t.Optional[t.Callable[[], Context]] = None,
) -> t.Tuple[t.List[NodeExecutionFailedError[H]], t.List[H]]:
    context = context_factory() if context_factory else None
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
            if context is None:
                fn(node)
            else:
                context.copy().run(fn, node)
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
) -> t.List[R]:
    """Applies a function to the given collection of values concurrently.

    Args:
        values: Target values.
        fn: The function that will be applied concurrently to each value.
        tasks_num: The number of concurrent tasks.

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

    with ThreadPoolExecutor(max_workers=tasks_num) as pool:
        for index, value in enumerate(values):
            pool.submit(_process_value, value, index)

    return [f.result() for f in futures]
