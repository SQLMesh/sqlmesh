#!/usr/bin/env bash
set -ex

if [[ -z $(git tag --points-at HEAD) ]]; then
    # If the current commit is not tagged, we need to find the last tag
    LAST_TAG=$(git describe --tags --abbrev=0)
else
    # If the current commit is tagged, we need to find the previous tag
    LAST_TAG=$(git tag --sort=-creatordate | head -n 2 | tail -n 1)
fi

if [ "$1" == "" ]; then
    echo "Usage: $0 <example name> <sqlmesh opts>"
    echo "eg $0 sushi '--gateway duckdb_persistent'"
    exit 1
fi


TMP_DIR=$(mktemp -d)
EXAMPLE_NAME="$1"
SQLMESH_OPTS="$2"
EXAMPLE_DIR="./examples/$EXAMPLE_NAME"
TEST_DIR="$TMP_DIR/$EXAMPLE_NAME"

echo "Running migration test for '$EXAMPLE_NAME' in '$TEST_DIR' for example project '$EXAMPLE_DIR' using options '$SQLMESH_OPTS'"

# Copy the example project from the *current* checkout so it's stable across old/new SQLMesh versions
cp -r "$EXAMPLE_DIR" "$TEST_DIR"

git checkout $LAST_TAG

# Install dependencies from the previous release.
uv venv .venv --clear
source .venv/bin/activate
make install-dev

# this is only needed temporarily until the released tag for $LAST_TAG includes this config
if [ "$EXAMPLE_NAME" == "sushi_dbt" ]; then
    echo 'migration_test_config = sqlmesh_config(Path(__file__).parent, dbt_target_name="duckdb")' >> $TEST_DIR/config.py
fi

# Run initial plan
pushd $TEST_DIR
rm -rf ./data/*
sqlmesh $SQLMESH_OPTS plan --no-prompts --auto-apply
rm -rf .cache
popd

# Switch back to the starting state of the repository
git checkout -

# Install updated dependencies.
uv venv .venv --clear
source .venv/bin/activate
make install-dev

# Migrate and make sure the diff is empty
pushd $TEST_DIR
sqlmesh $SQLMESH_OPTS migrate
sqlmesh $SQLMESH_OPTS diff prod
popd
