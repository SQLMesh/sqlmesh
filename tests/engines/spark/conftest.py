import typing as t

import pytest
from pyspark.sql import SparkSession

from sqlmesh.utils.java import is_spark_java_supported, spark_java_options

pytestmark = [pytest.mark.slow, pytest.mark.pyspark]


@pytest.fixture(scope="session")
def spark_session() -> t.Generator[SparkSession, None, None]:
    if not is_spark_java_supported():
        pytest.skip("Spark is not supported on Java 24+ with bundled Hadoop dependencies.")

    builder = (
        SparkSession.builder.master("local")
        .appName("SQLMesh Test")
        .config("spark.driver.memory", "512m")
    )
    java_options = spark_java_options()
    if java_options:
        builder = builder.config("spark.driver.extraJavaOptions", java_options)

    session = builder.enableHiveSupport().getOrCreate()
    yield session
    session.stop()
