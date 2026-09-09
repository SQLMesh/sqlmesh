from __future__ import annotations

import time
import threading
import typing as t
import unittest
from io import StringIO

import concurrent
from concurrent.futures import ThreadPoolExecutor

from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.model import Model
from sqlmesh.core.test.definition import ModelTest as ModelTest, generate_test as generate_test
from sqlmesh.core.test.discovery import (
    ModelTestMetadata as ModelTestMetadata,
)
from sqlmesh.core.config.connection import BaseDuckDBConnectionConfig
from sqlmesh.core.test.result import ModelTextTestResult as ModelTextTestResult
from sqlmesh.utils import UniqueKeyDict, Verbosity


if t.TYPE_CHECKING:
    from sqlmesh.core.config.loader import C


class ModelTextTestRunner(unittest.TextTestRunner):
    def __init__(
        self,
        **kwargs: t.Any,
    ) -> None:
        # StringIO is used to capture the output of the tests since we'll
        # run them in parallel and we don't want to mix the output streams
        from io import StringIO

        super().__init__(
            stream=StringIO(),
            resultclass=ModelTextTestResult,
            **kwargs,
        )


def create_testing_engine_adapters(
    model_test_metadata: list[ModelTestMetadata],
    config: C,
    selected_gateway: str,
    default_catalog: str | None = None,
    default_catalog_dialect: str = "",
) -> t.Dict[ModelTestMetadata, EngineAdapter]:
    testing_adapter_by_gateway: t.Dict[str, EngineAdapter] = {}
    metadata_to_adapter = {}

    for metadata in model_test_metadata:
        gateway = metadata.body.get("gateway") or selected_gateway
        test_connection = config.get_test_connection(
            gateway, default_catalog, default_catalog_dialect
        )

        concurrent_tasks = test_connection.concurrent_tasks

        is_duckdb_connection = isinstance(test_connection, BaseDuckDBConnectionConfig)
        adapter = None
        if is_duckdb_connection:
            # Ensure DuckDB connections are fully isolated from each other
            # by forcing the creation of a new adapter with SingletonConnectionPool
            test_connection.concurrent_tasks = 1
            adapter = test_connection.create_engine_adapter(register_comments_override=False)
            test_connection.concurrent_tasks = concurrent_tasks
        elif gateway not in testing_adapter_by_gateway:
            # All other engines can share connections between threads
            testing_adapter_by_gateway[gateway] = test_connection.create_engine_adapter(
                register_comments_override=False
            )

        metadata_to_adapter[metadata] = adapter or testing_adapter_by_gateway[gateway]

    return metadata_to_adapter


def run_tests(
    model_test_metadata: list[ModelTestMetadata],
    models: UniqueKeyDict[str, Model],
    config: C,
    selected_gateway: str,
    dialect: str | None = None,
    verbosity: Verbosity = Verbosity.DEFAULT,
    preserve_fixtures: bool = False,
    stream: t.TextIO | None = None,
    default_catalog: str | None = None,
    default_catalog_dialect: str = "",
) -> ModelTextTestResult:
    """Create a test suite of ModelTest objects and run it.

    Args:
        model_test_metadata: A list of ModelTestMetadata named tuples.
        models: All models to use for expansion and mapping of physical locations.
        verbosity: The verbosity level.
        preserve_fixtures: Preserve the fixture tables in the testing database, useful for debugging.
    """
    default_test_connection = config.get_test_connection(
        gateway_name=selected_gateway,
        default_catalog=default_catalog,
        default_catalog_dialect=default_catalog_dialect,
    )

    lock = threading.Lock()

    from sqlmesh.core.console import get_console

    combined_results = ModelTextTestResult(
        stream=unittest.runner._WritelnDecorator(stream or StringIO()),  # type: ignore
        verbosity=2 if verbosity >= Verbosity.VERBOSE else 1,
        descriptions=True,
        console=get_console(),
    )

    metadata_to_adapter = create_testing_engine_adapters(
        model_test_metadata=model_test_metadata,
        config=config,
        selected_gateway=selected_gateway,
        default_catalog=default_catalog,
        default_catalog_dialect=default_catalog_dialect,
    )

    # Ensure workers are not greater than the number of tests
    num_workers = min(len(model_test_metadata) or 1, default_test_connection.concurrent_tasks)

    # Build ModelTest instances on the calling thread before workers start. create_test()
    # can call to_datetime() / ttl_cache (time.time()), which races with another worker's
    # time_machine freeze when execution_time is set under concurrent_tasks > 1.
    tests: list[ModelTest] = []
    for metadata, engine_adapter in metadata_to_adapter.items():
        test = ModelTest.create_test(
            body=metadata.body,
            test_name=metadata.test_name,
            models=models,
            engine_adapter=engine_adapter,
            dialect=dialect,
            path=metadata.path,
            default_catalog=default_catalog,
            preserve_fixtures=preserve_fixtures,
            concurrency=num_workers > 1,
            verbosity=verbosity,
        )
        if test:
            tests.append(test)

    def _run_single_test(test: ModelTest) -> ModelTextTestResult:
        result = t.cast(
            ModelTextTestResult,
            ModelTextTestRunner().run(t.cast(unittest.TestCase, test)),
        )

        with lock:
            combined_results.merge(result)

        return result

    test_results = []

    start_time = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = [pool.submit(_run_single_test, test) for test in tests]

            for future in concurrent.futures.as_completed(futures):
                test_results.append(future.result())
    finally:
        for engine_adapter in set(metadata_to_adapter.values()):
            # The engine adapters list might have duplicates, so we ensure that we close each adapter once
            if engine_adapter:
                engine_adapter.close()

    end_time = time.perf_counter()

    combined_results.duration = round(end_time - start_time, 2)

    return combined_results
