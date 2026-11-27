# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import time
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
from minio import Minio  # type: ignore[import-not-found]
from pyiceberg.catalog import load_catalog  # type: ignore[import-not-found]
from pyiceberg.schema import Schema  # type: ignore[import-not-found]
from pyiceberg.types import LongType, NestedField, StringType  # type: ignore[import-not-found]


CATALOG_NAME = "demo"
NAMESPACE = "analytics"
TABLE_NAME = "events"
TABLE_IDENTIFIER = f"{NAMESPACE}.{TABLE_NAME}"
BUCKET = "warehouse-rest"
OBJECT_PREFIX = "data/sqlmesh/events/"
TABLE_LOCATION = "s3://warehouse-rest/data/sqlmesh/events"
ROWS = [
    {"id": 1, "value": "alpha"},
    {"id": 2, "value": "beta"},
    {"id": 3, "value": "gamma"},
]

SCHEMA = Schema(
    NestedField(field_id=1, name="id", field_type=LongType(), required=True),
    NestedField(field_id=2, name="value", field_type=StringType(), required=True),
)
ARROW_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("value", pa.string(), nullable=False),
    ]
)


def _load_catalog() -> Any:
    last_error: Exception | None = None
    for attempt in range(30):
        try:
            catalog = load_catalog(
                CATALOG_NAME,
                **{
                    "uri": os.environ["ICEBERG_REST_URL"],
                    "type": "rest",
                    "s3.endpoint": os.environ["S3_ENDPOINT"],
                    "s3.access-key-id": os.environ["S3_ACCESS_KEY_ID"],
                    "s3.secret-access-key": os.environ["S3_SECRET_ACCESS_KEY"],
                    "s3.region": "us-east-1",
                    # PyArrow's pinned S3FileSystem defaults to path-style addressing.
                },
            )
            catalog.list_namespaces()
            return catalog
        except Exception as ex:
            last_error = ex
            if attempt == 29:
                break
            time.sleep(1)

    raise RuntimeError(
        "Iceberg REST catalog did not become ready after 30 attempts"
    ) from last_error


def _clear_table_location() -> None:
    client = Minio(
        "minio:9000",
        access_key=os.environ["S3_ACCESS_KEY_ID"],
        secret_key=os.environ["S3_SECRET_ACCESS_KEY"],
        secure=False,
    )
    for attempt in range(30):
        try:
            if client.bucket_exists(BUCKET):
                break
        except Exception:
            if attempt == 29:
                raise
        time.sleep(1)
    else:
        raise RuntimeError(f"MinIO bucket {BUCKET!r} was not created")

    for item in client.list_objects(BUCKET, prefix=OBJECT_PREFIX, recursive=True):
        if item.object_name:
            client.remove_object(BUCKET, item.object_name)


def main() -> None:
    catalog = _load_catalog()
    namespaces = catalog.list_namespaces()
    if (NAMESPACE,) not in namespaces:
        catalog.create_namespace(NAMESPACE)

    if (NAMESPACE, TABLE_NAME) in catalog.list_tables(NAMESPACE):
        catalog.drop_table(TABLE_IDENTIFIER)
    _clear_table_location()

    table = catalog.create_table(
        identifier=TABLE_IDENTIFIER,
        schema=SCHEMA,
        location=TABLE_LOCATION,
    )
    table.append(pa.Table.from_pylist(ROWS[:2], schema=ARROW_SCHEMA))
    table.append(pa.Table.from_pylist(ROWS[2:], schema=ARROW_SCHEMA))

    actual_rows = sorted(table.scan().to_arrow().to_pylist(), key=lambda row: row["id"])
    snapshot_count = len(table.metadata.snapshots)
    if actual_rows != ROWS or snapshot_count != 2:
        raise RuntimeError(
            f"Expected 3 rows in 2 snapshots; got rows={actual_rows!r}, snapshots={snapshot_count}"
        )

    print(f"Seeded {TABLE_IDENTIFIER} at {TABLE_LOCATION}: 3 rows, 2 snapshots")


if __name__ == "__main__":
    main()
