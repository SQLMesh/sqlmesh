#!/bin/bash

# The purpose of this script is to be called after `docker compose up -d` has been run for a given database
# The idea is to block until the database is available to serve requests. Once the database can serve requests,
# the integration tests can be run.
# Therefore, the ports etc are tightly coupled with the compose.yml files under tests/core/engine_adapter/docker/
#
# Note that if the docker daemon is not running `localhost`, you can set the DOCKER_HOSTNAME environment variable to the
# correct host Docker is running on

set -e

if [ -z "$1" ]; then
    echo "USAGE: $0 <engine>"
    exit 1
fi

ENGINE="$1"

function_exists() {
    declare -f -F $1 > /dev/null
    return $?
}

probe_port() {
    HOSTNAME=${DOCKER_HOSTNAME:-localhost}
    echo "Probing '$HOSTNAME' on port $1"
    while ! nc -z $HOSTNAME $1; do
        sleep 1
    done
}

clickhouse_ready() {
    probe_port 8123
}

postgres_ready() {
    probe_port 5432
}

mssql_ready() {
    probe_port 1433
}

mysql_ready() {
    probe_port 3306
}

spark_ready() {
    probe_port 15002
}

starrocks_ready() {
    probe_port 9030

    echo "Checking for 1 alive StarRocks backends..."
    sleep 5

    while true; do
        echo "Checking StarRocks backends..."
        ALIVE_BACKENDS=$(docker exec -i starrocks-fe mysql -h127.0.0.1 -P9030 -uroot -e "show backends \G" | grep -c "^ *Alive: true *$")

        # fallback value if failed to get number
        if ! [[ "$ALIVE_BACKENDS" =~ ^[0-9]+$ ]]; then
            echo "WARN: Unable to parse number of alive backends, got: '$ALIVE_BACKENDS'"
            ALIVE_BACKENDS=0
        fi

        echo "Found $ALIVE_BACKENDS alive backends"

        if [ "$ALIVE_BACKENDS" -ge 1 ]; then
            echo "StarRocks has 1 or more alive backends"
            break
        fi

        echo "Waiting for more backends to become alive..."
        sleep 5
    done

    # set default replication num to 1 (there is only one be in the docker compose file)
    docker exec -i starrocks-fe mysql -h127.0.0.1 -P9030 -uroot -e "ADMIN SET frontend config ('default_replication_num' = '1');"
}

trino_ready() {
    # Trino has a built-in healthcheck script, just call that
    docker compose -f tests/core/engine_adapter/integration/docker/compose.trino.yaml exec trino /bin/bash -c '/usr/lib/trino/bin/health-check'
}

risingwave_ready() {
    probe_port 4566
}

echo "Waiting for $ENGINE to be ready..."

READINESS_FUNC="${ENGINE}_ready"

# If called with an unimplemented / unsupported engine, just exit
if ! function_exists $READINESS_FUNC ; then
    echo "WARN: $READINESS_FUNC not implemeted; exiting"
    exit 0
fi

EXIT_CODE=1

while [ $EXIT_CODE -ne 0 ]; do
    echo "Checking $ENGINE"
    $READINESS_FUNC && EXIT_CODE=$? || EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        echo "$ENGINE not ready; sleeping"
        sleep 5
    fi
done

echo "$ENGINE is ready!"
