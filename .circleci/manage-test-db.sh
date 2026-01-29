#!/bin/bash

# The purpose of this script is to create and destroy temporary test databases on the cloud engines
# The idea is that a database is created, the integration tests are run on that database and then the database is dropped
# This allows builds for multiple PR's to run concurrently without the tests clobbering each other and also gives each set of tests a fresh environment

# Note: It is expected that the environment variables defined in 'tests/core/engine_adapter/config.yaml' for each cloud engine are set

set -e

if [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ]; then
    echo "USAGE: $0 <engine> <db_name> <up/down>"
    exit 1
fi

ENGINE="$1"
DB_NAME="$2"
DIRECTION="$3"

function_exists() {
    declare -f -F $1 > /dev/null
    return $?
}

# Snowflake
snowflake_init() {
    echo "Installing Snowflake CLI"
    pip install "snowflake-cli"
}

snowflake_up() {
    snow sql -q "create database if not exists $1" --temporary-connection
}

snowflake_down() {
    snow sql -q "drop database if exists $1" --temporary-connection
}

# Databricks
databricks_init() {
    echo "Installing Databricks CLI"
    curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sudo sh || true
}

databricks_up() {
    databricks catalogs create $1 || true
}

databricks_down() {
    databricks catalogs delete $1 --force || true
}

# Redshift
redshift_init() {
    psql --version
}

redshift_exec() {
    PGPASSWORD=$REDSHIFT_PASSWORD psql -h $REDSHIFT_HOST -p $REDSHIFT_PORT -U $REDSHIFT_USER -c "$1" dev
}

redshift_up() {
    redshift_exec "create database $1"
}

redshift_down() {
    # try to prevent a "database is being accessed by other users" error when running DROP DATABASE
    EXIT_CODE=1
    ATTEMPTS=0
    while [ $EXIT_CODE -ne 0 ] && [ $ATTEMPTS -lt 5 ]; do
        # note: sometimes this pg_terminate_backend() call can randomly fail with: ERROR:  Insufficient privileges 
        # if it does, let's proceed with the drop anyway rather than aborting and never attempting the drop
        redshift_exec "select pg_terminate_backend(procpid) from pg_stat_activity where datname = '$1'" || true
        
        # perform drop
        redshift_exec "drop database $1;" && EXIT_CODE=$? || EXIT_CODE=$?
        if [ $EXIT_CODE -ne 0 ]; then
            echo "Unable to drop database; retrying..."
            ATTEMPTS=$((ATTEMPTS + 1))
            sleep 5
        fi
    done
}

# BigQuery
bigquery_init() {
    # Write out the keyfile for the integration tests to pick up
    echo "Writing out keyfile to $BIGQUERY_KEYFILE"
    echo "$BIGQUERY_KEYFILE_CONTENTS" > $BIGQUERY_KEYFILE
}


# Clickhouse cloud
clickhouse-cloud_init() {
    # note: the ping endpoint doesnt seem to need any API keys
    until curl https://$CLICKHOUSE_CLOUD_HOST:8443/ping
    do
        echo "Pinging Clickhouse Cloud service to ensure it's not in idle mode..."
        sleep 5
    done
    echo "Clickhouse Cloud instance $CLICKHOUSE_CLOUD_HOST is up and running"
}

# GCP Postgres
gcp-postgres_init() {
    # Download and start Cloud SQL Proxy
    curl -fsSL -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.18.0/cloud-sql-proxy.linux.amd64
    chmod +x cloud-sql-proxy
    echo "$GCP_POSTGRES_KEYFILE_JSON" > /tmp/keyfile.json
    ./cloud-sql-proxy --credentials-file /tmp/keyfile.json $GCP_POSTGRES_INSTANCE_CONNECTION_STRING &

    # Wait for proxy to start
    sleep 5
}

gcp-postgres_exec() {
    PGPASSWORD=$GCP_POSTGRES_PASSWORD psql -h 127.0.0.1 -U $GCP_POSTGRES_USER -c "$1" postgres
}

gcp-postgres_up() {
    gcp-postgres_exec "create database $1"
}

gcp-postgres_down() {
    gcp-postgres_exec "drop database $1"
}

# Fabric
fabric_init() {    
    python --version #note: as at 2025-08-20, ms-fabric-cli is pinned to Python >= 3.10, <3.13
    pip install ms-fabric-cli
    
    # to prevent the '[EncryptionFailed] An error occurred with the encrypted cache.' error
    # ref: https://microsoft.github.io/fabric-cli/#switch-to-interactive-mode-optional
    fab config set encryption_fallback_enabled true 

    echo "Logging in to Fabric"
    fab auth login -u $FABRIC_CLIENT_ID -p $FABRIC_CLIENT_SECRET --tenant $FABRIC_TENANT_ID
}

fabric_up() {
    fab create "SQLMesh CircleCI.Workspace/$1.Warehouse"
}

fabric_down() {
    fab rm -f "SQLMesh CircleCI.Workspace/$1.Warehouse" || true
}

INIT_FUNC="${ENGINE}_init"
UP_FUNC="${ENGINE}_up"
DOWN_FUNC="${ENGINE}_down"

# If called with an unimplemented / unsupported engine, just exit
if ! function_exists $INIT_FUNC ; then
    echo "WARN: $INIT_FUNC not implemeted; exiting"
    exit 0
fi

echo "Initializing $ENGINE"
$INIT_FUNC

if [ "$DIRECTION" == "up" ] && function_exists $UP_FUNC; then
    echo "Creating database $DB_NAME"
    $UP_FUNC $DB_NAME
elif [ "$DIRECTION" == "down" ] && function_exists $DOWN_FUNC; then
    echo "Dropping database $DB_NAME"
    $DOWN_FUNC $DB_NAME
fi

echo "All done"
