from __future__ import annotations

import typing as t
import logging
import requests
import time
from functools import cached_property
from sqlglot import exp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_result
from sqlmesh.core.engine_adapter.mssql import MSSQLEngineAdapter
from sqlmesh.core.engine_adapter.shared import (
    CommentCreationTable,
    CommentCreationView,
    InsertOverwriteStrategy,
)
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.connection_pool import ConnectionPool
from sqlmesh.core.schema_diff import TableAlterOperation
from sqlmesh.utils import random_id


logger = logging.getLogger(__name__)


class FabricEngineAdapter(MSSQLEngineAdapter):
    """
    Adapter for Microsoft Fabric.
    """

    DIALECT = "fabric"
    SUPPORTS_INDEXES = False
    SUPPORTS_TRANSACTIONS = False
    SUPPORTS_CREATE_DROP_CATALOG = True
    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.DELETE_INSERT
    # There is no standard method to handle comments in Fabric for now, so we disable it.
    # Otherwise, it would be inherited from MSSQL and would not work.
    COMMENT_CREATION_TABLE = CommentCreationTable.UNSUPPORTED
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED

    def __init__(
        self, connection_factory_or_pool: t.Union[t.Callable, t.Any], *args: t.Any, **kwargs: t.Any
    ) -> None:
        # Wrap connection factory to support changing the catalog dynamically at runtime
        if not isinstance(connection_factory_or_pool, ConnectionPool):
            original_connection_factory = connection_factory_or_pool

            connection_factory_or_pool = lambda *args, **kwargs: original_connection_factory(
                target_catalog=self._target_catalog, *args, **kwargs
            )

        super().__init__(connection_factory_or_pool, *args, **kwargs)

    @property
    def _target_catalog(self) -> t.Optional[str]:
        return self._connection_pool.get_attribute("target_catalog")

    @_target_catalog.setter
    def _target_catalog(self, value: t.Optional[str]) -> None:
        self._connection_pool.set_attribute("target_catalog", value)

    @property
    def api_client(self) -> FabricHttpClient:
        # the requests Session is not guaranteed to be threadsafe
        # so we create a http client per thread on demand
        if existing_client := self._connection_pool.get_attribute("api_client"):
            return existing_client

        tenant_id: t.Optional[str] = self._extra_config.get("tenant_id")
        workspace_id: t.Optional[str] = self._extra_config.get("workspace_id")
        client_id: t.Optional[str] = self._extra_config.get("user")
        client_secret: t.Optional[str] = self._extra_config.get("password")

        if not tenant_id or not client_id or not client_secret:
            raise SQLMeshError(
                "Service Principal authentication requires tenant_id, client_id, and client_secret "
                "in the Fabric connection configuration"
            )

        if not workspace_id:
            raise SQLMeshError(
                "Fabric requires the workspace_id to be configured in the connection configuration to create / drop catalogs"
            )

        client = FabricHttpClient(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            client_id=client_id,
            client_secret=client_secret,
        )

        self._connection_pool.set_attribute("api_client", client)
        return client

    def _create_catalog(self, catalog_name: exp.Identifier) -> None:
        """Create a catalog (warehouse) in Microsoft Fabric via REST API."""
        warehouse_name = catalog_name.sql(dialect=self.dialect, identify=False)
        logger.info(f"Creating Fabric warehouse: {warehouse_name}")

        self.api_client.create_warehouse(warehouse_name)

    def _drop_catalog(self, catalog_name: exp.Identifier) -> None:
        """Drop a catalog (warehouse) in Microsoft Fabric via REST API."""
        warehouse_name = catalog_name.sql(dialect=self.dialect, identify=False)
        current_catalog = self.get_current_catalog()

        logger.info(f"Deleting Fabric warehouse: {warehouse_name}")
        self.api_client.delete_warehouse(warehouse_name)

        if warehouse_name == current_catalog:
            # Somewhere around 2025-09-08, Fabric started validating the "Database=" connection argument and throwing 'Authentication failed' if the database doesnt exist
            # In addition, set_current_catalog() is implemented using a threadlocal variable "target_catalog"
            # So, when we drop a warehouse, and there are still threads with "target_catalog" set to reference it, any operations on those threads
            # that use an either use an existing connection pointing to this warehouse or trigger a new connection
            # will fail with an 'Authentication Failed' error unless we close all connections here, which also clears all the threadlocal data
            self.close()

    def set_current_catalog(self, catalog_name: str) -> None:
        """
        Set the current catalog for Microsoft Fabric connections.

        Override to handle Fabric's stateless session limitation where USE statements
        don't persist across queries. Instead, we close existing connections and
        recreate them with the new catalog in the connection configuration.

        Args:
            catalog_name: The name of the catalog (warehouse) to switch to

        Note:
            Fabric doesn't support catalog switching via USE statements because each
            statement runs as an independent session. This method works around this
            limitation by updating the connection pool with new catalog configuration.

        See:
            https://learn.microsoft.com/en-us/fabric/data-warehouse/sql-query-editor#limitations
        """
        current_catalog = self.get_current_catalog()

        # If already using the requested catalog, do nothing
        if current_catalog and current_catalog == catalog_name:
            logger.debug(f"Already using catalog '{catalog_name}', no action needed")
            return

        logger.info(f"Switching from catalog '{current_catalog}' to '{catalog_name}'")

        # commit the transaction before closing the connection to help prevent errors like:
        # > Snapshot isolation transaction failed in database because the object accessed by the statement has been modified by a
        # > DDL statement in another concurrent transaction since the start of this transaction
        # on subsequent queries in the new connection
        self._connection_pool.commit()

        # note: we call close() on the connection pool instead of self.close() because self.close() calls close_all()
        # on the connection pool but we just want to close the connection for this thread
        self._connection_pool.close()
        self._target_catalog = catalog_name  # new connections will use this catalog

        catalog_after_switch = self.get_current_catalog()

        if catalog_after_switch != catalog_name:
            # We need to raise an error if the catalog switch failed to prevent the operation that needed the catalog switch from being run against the wrong catalog
            raise SQLMeshError(
                f"Unable to switch catalog to {catalog_name}, catalog ended up as {catalog_after_switch}"
            )

    def alter_table(
        self, alter_expressions: t.Union[t.List[exp.Alter], t.List[TableAlterOperation]]
    ) -> None:
        """
        Applies alter expressions to a table. Fabric has limited support for ALTER TABLE,
        so this method implements a workaround for column type changes.
        This method is self-contained and sets its own catalog context.
        """
        if not alter_expressions:
            return

        # Get the target table from the first expression to determine the correct catalog.
        first_op = alter_expressions[0]
        expression = first_op.expression if isinstance(first_op, TableAlterOperation) else first_op
        if not isinstance(expression, exp.Alter) or not expression.this.catalog:
            # Fallback for unexpected scenarios
            logger.warning(
                "Could not determine catalog from alter expression, executing with current context."
            )
            super().alter_table(alter_expressions)
            return

        target_catalog = expression.this.catalog
        self.set_current_catalog(target_catalog)

        with self.transaction():
            for op in alter_expressions:
                expression = op.expression if isinstance(op, TableAlterOperation) else op

                if not isinstance(expression, exp.Alter):
                    self.execute(expression)
                    continue

                for action in expression.actions:
                    table_name = expression.this

                    table_name_without_catalog = table_name.copy()
                    table_name_without_catalog.set("catalog", None)

                    is_type_change = isinstance(action, exp.AlterColumn) and action.args.get(
                        "dtype"
                    )

                    if is_type_change:
                        column_to_alter = action.this
                        new_type = action.args["dtype"]
                        temp_column_name_str = f"{column_to_alter.name}__{random_id(short=True)}"
                        temp_column_name = exp.to_identifier(temp_column_name_str)

                        logger.info(
                            "Applying workaround for column '%s' on table '%s' to change type to '%s'.",
                            column_to_alter.sql(),
                            table_name.sql(),
                            new_type.sql(),
                        )

                        # Step 1: Add a temporary column.
                        add_column_expr = exp.Alter(
                            this=table_name_without_catalog.copy(),
                            kind="TABLE",
                            actions=[
                                exp.ColumnDef(this=temp_column_name.copy(), kind=new_type.copy())
                            ],
                        )
                        add_sql = self._to_sql(add_column_expr)
                        self.execute(add_sql)

                        # Step 2: Copy and cast data.
                        update_sql = self._to_sql(
                            exp.Update(
                                this=table_name_without_catalog.copy(),
                                expressions=[
                                    exp.EQ(
                                        this=temp_column_name.copy(),
                                        expression=exp.Cast(
                                            this=column_to_alter.copy(), to=new_type.copy()
                                        ),
                                    )
                                ],
                            )
                        )
                        self.execute(update_sql)

                        # Step 3: Drop the original column.
                        drop_sql = self._to_sql(
                            exp.Alter(
                                this=table_name_without_catalog.copy(),
                                kind="TABLE",
                                actions=[exp.Drop(this=column_to_alter.copy(), kind="COLUMN")],
                            )
                        )
                        self.execute(drop_sql)

                        # Step 4: Rename the temporary column.
                        old_name_qualified = f"{table_name_without_catalog.sql(dialect=self.dialect)}.{temp_column_name.sql(dialect=self.dialect)}"
                        new_name_unquoted = column_to_alter.sql(
                            dialect=self.dialect, identify=False
                        )
                        rename_sql = f"EXEC sp_rename '{old_name_qualified}', '{new_name_unquoted}', 'COLUMN'"
                        self.execute(rename_sql)
                    else:
                        # For other alterations, execute directly.
                        direct_alter_expr = exp.Alter(
                            this=table_name_without_catalog.copy(), kind="TABLE", actions=[action]
                        )
                        self.execute(direct_alter_expr)


class FabricHttpClient:
    def __init__(self, tenant_id: str, workspace_id: str, client_id: str, client_secret: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.workspace_id = workspace_id

    def create_warehouse(
        self, warehouse_name: str, if_not_exists: bool = True, attempt: int = 0
    ) -> None:
        """Create a catalog (warehouse) in Microsoft Fabric via REST API."""

        # attempt count is arbitrary, it essentially equates to 5 minutes of 30 second waits
        if attempt > 10:
            raise SQLMeshError(
                f"Gave up waiting for Fabric warehouse {warehouse_name} to become available"
            )

        logger.info(f"Creating Fabric warehouse: {warehouse_name}")

        request_data = {
            "displayName": warehouse_name,
            "description": f"Warehouse created by SQLMesh: {warehouse_name}",
        }

        response = self.session.post(self._endpoint_url("warehouses"), json=request_data)

        if (
            if_not_exists
            and response.status_code == 400
            and (errorCode := response.json().get("errorCode", None))
        ):
            if errorCode == "ItemDisplayNameAlreadyInUse":
                logger.warning(f"Fabric warehouse {warehouse_name} already exists")
                return
            if errorCode == "ItemDisplayNameNotAvailableYet":
                logger.warning(f"Fabric warehouse {warehouse_name} is still spinning up; waiting")
                # Fabric error message is something like:
                #  - "Requested 'circleci_51d7087e__dev' is not available yet and is expected to become available in the upcoming minutes."
                # This seems to happen if a catalog is dropped and then a new one with the same name is immediately created.
                # There appears to be some delayed async process on the Fabric side that actually drops the warehouses and frees up the names to be used again
                time.sleep(30)
                return self.create_warehouse(
                    warehouse_name=warehouse_name, if_not_exists=if_not_exists, attempt=attempt + 1
                )

        try:
            response.raise_for_status()
        except:
            # the important information to actually debug anything is in the response body which Requests never prints
            logger.exception(
                f"Failed to create warehouse {warehouse_name}. status: {response.status_code}, body: {response.text}"
            )
            raise

        # Handle direct success (201) or async creation (202)
        if response.status_code == 201:
            logger.info(f"Successfully created Fabric warehouse: {warehouse_name}")
            return

        if response.status_code == 202 and (location_header := response.headers.get("location")):
            logger.info(f"Warehouse creation initiated for: {warehouse_name}")
            self._wait_for_completion(location_header, warehouse_name)
            logger.info(f"Successfully created Fabric warehouse: {warehouse_name}")
        else:
            logger.error(f"Unexpected response from Fabric API: {response}\n{response.text}")
            raise SQLMeshError(f"Unable to create warehouse: {response}")

    def delete_warehouse(self, warehouse_name: str, if_exists: bool = True) -> None:
        """Drop a catalog (warehouse) in Microsoft Fabric via REST API."""
        logger.info(f"Deleting Fabric warehouse: {warehouse_name}")

        # Get the warehouse ID by listing warehouses
        # TODO: handle continuationUri for pagination, ref: https://learn.microsoft.com/en-us/rest/api/fabric/warehouse/items/list-warehouses?tabs=HTTP#warehouses
        response = self.session.get(self._endpoint_url("warehouses"))
        response.raise_for_status()

        warehouse_name_to_id = {
            warehouse.get("displayName"): warehouse.get("id")
            for warehouse in response.json().get("value", [])
        }

        warehouse_id = warehouse_name_to_id.get(warehouse_name, None)

        if not warehouse_id:
            logger.warning(
                f"Fabric warehouse does not exist: {warehouse_name}\n(available warehouses: {', '.join(warehouse_name_to_id)})"
            )
            if if_exists:
                return

            raise SQLMeshError(
                f"Unable to delete Fabric warehouse {warehouse_name} as it doesnt exist"
            )

        # Delete the warehouse by ID
        response = self.session.delete(self._endpoint_url(f"warehouses/{warehouse_id}"))
        response.raise_for_status()

        logger.info(f"Successfully deleted Fabric warehouse: {warehouse_name}")

    @cached_property
    def session(self) -> requests.Session:
        s = requests.Session()

        access_token = self._get_access_token()
        s.headers.update({"Authorization": f"Bearer {access_token}"})

        return s

    def _endpoint_url(self, endpoint: str) -> str:
        if endpoint.startswith("/"):
            endpoint = endpoint[1:]

        return f"https://api.fabric.microsoft.com/v1/workspaces/{self.workspace_id}/{endpoint}"

    def _get_access_token(self) -> str:
        """Get access token using Service Principal authentication."""

        # Use Azure AD OAuth2 token endpoint
        token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"

        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://api.fabric.microsoft.com/.default",
        }

        response = requests.post(token_url, data=data)
        response.raise_for_status()
        token_data = response.json()
        return token_data["access_token"]

    def _wait_for_completion(self, location_url: str, operation_name: str) -> None:
        """Poll the operation status until completion."""

        @retry(
            wait=wait_exponential(multiplier=1, min=1, max=30),
            stop=stop_after_attempt(20),
            retry=retry_if_result(lambda result: result not in ["Succeeded", "Failed"]),
        )
        def _poll() -> str:
            response = self.session.get(location_url)
            response.raise_for_status()

            result = response.json()
            status = result.get("status", "Unknown")

            logger.debug(f"Operation {operation_name} status: {status}")

            if status == "Failed":
                error_msg = result.get("error", {}).get("message", "Unknown error")
                raise SQLMeshError(f"Operation {operation_name} failed: {error_msg}")
            elif status in ["InProgress", "Running"]:
                logger.debug(f"Operation {operation_name} still in progress...")
            elif status not in ["Succeeded"]:
                logger.warning(f"Unknown status '{status}' for operation {operation_name}")

            return status

        final_status = _poll()
        if final_status != "Succeeded":
            raise SQLMeshError(f"Operation {operation_name} completed with status: {final_status}")
