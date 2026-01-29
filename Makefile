.PHONY: docs

ifdef UV
    PIP := uv pip
else
    PIP := pip3
endif

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
    SED_INPLACE = sed -i ''
else
    SED_INPLACE = sed -i
endif

install-dev:
	$(PIP) install -e ".[dev,web,slack,dlt,lsp]" ./examples/custom_materializations

install-doc:
	$(PIP) install -r ./docs/requirements.txt

install-pre-commit:
	pre-commit install

install-dev-dbt-%:
	@version="$*"; \
	period_count=$$(echo "$$version" | tr -cd '.' | wc -c); \
	if [ "$$period_count" -eq 0 ]; then \
		version="$${version:0:1}.$${version:1}"; \
	elif [ "$$period_count" -eq 1 ]; then \
		version="$$version.0"; \
	fi; \
	echo "Installing dbt version: $$version"; \
	cp pyproject.toml pyproject.toml.backup; \
	$(SED_INPLACE) 's/"pydantic>=2.0.0"/"pydantic"/g' pyproject.toml; \
	if [ "$$version" = "1.10.0" ]; then \
		echo "Applying special handling for dbt 1.10.0"; \
		$(SED_INPLACE) -E 's/"(dbt-core)[^"]*"/"\1~='"$$version"'"/g' pyproject.toml; \
		$(SED_INPLACE) -E 's/"(dbt-(bigquery|duckdb|snowflake|athena-community|clickhouse|redshift|trino))[^"]*"/"\1"/g' pyproject.toml; \
		$(SED_INPLACE) -E 's/"(dbt-databricks)[^"]*"/"\1~='"$$version"'"/g' pyproject.toml; \
	else \
		echo "Applying version $$version to all dbt packages"; \
		$(SED_INPLACE) -E 's/"(dbt-[^"><=~!]+)[^"]*"/"\1~='"$$version"'"/g' pyproject.toml; \
	fi; \
	if printf '%s\n' "$$version" | awk -F. '{ if ($$1 == 1 && (($$2 >= 3 && $$2 <= 5) || $$2 == 10)) exit 0; exit 1 }'; then \
		echo "Applying numpy<2 constraint for dbt $$version"; \
		$(SED_INPLACE) 's/"numpy"/"numpy<2"/g' pyproject.toml; \
	fi; \
	$(MAKE) install-dev; \
	if [ "$$version" = "1.6.0" ]; then \
		echo "Applying overrides for dbt 1.6.0"; \
		$(PIP) install 'pydantic>=2.0.0' 'google-cloud-bigquery==3.30.0' 'databricks-sdk==0.28.0' --reinstall; \
	fi; \
	if [ "$$version" = "1.7.0" ]; then \
		echo "Applying overrides for dbt 1.7.0"; \
		$(PIP) install 'databricks-sdk==0.28.0' --reinstall; \
	fi; \
	if [ "$$version" = "1.5.0" ]; then \
		echo "Applying overrides for dbt 1.5.0"; \
		$(PIP) install 'dbt-databricks==1.5.6' 'numpy<2' --reinstall; \
	fi; \
	mv pyproject.toml.backup pyproject.toml; \
	echo "Restored original pyproject.toml"

style:
	pre-commit run --all-files

py-style:
	SKIP=prettier,eslint pre-commit run --all-files

ui-style:
	pnpm run lint

doc-test:
	python -m pytest --doctest-modules sqlmesh/core sqlmesh/utils

package:
	$(PIP) install build && python3 -m build

publish: package
	$(PIP) install twine && python3 -m twine upload dist/*

package-tests:
	$(PIP) install build && cp pyproject.toml tests/sqlmesh_pyproject.toml && python3 -m build tests/

publish-tests: package-tests
	$(PIP) install twine && python3 -m twine upload -r tobiko-private tests/dist/*

docs-serve:
	mkdocs serve

api-docs:
	python pdoc/cli.py -o docs/_readthedocs/html/

api-docs-serve:
	python pdoc/cli.py

ui-up:
	docker compose -f ./web/docker-compose.yml up --build -d && $(if $(shell which open), open http://localhost:8001, echo "Open http://localhost:8001 in your browser.")

ui-down:
	docker compose -f ./web/docker-compose.yml down

ui-build:
	docker compose -f ./web/docker-compose.yml -f ./web/docker-compose.build.yml run app

clean-build:
	rm -rf build/ && rm -rf dist/ && rm -rf *.egg-info

clear-caches:
	find . -type d -name ".cache" -exec rm -rf {} + 2>/dev/null && echo "Successfully removed all .cache directories"

dev-publish: ui-build clean-build publish

jupyter-example:
	jupyter lab tests/slows/jupyter/example_outputs.ipynb

engine-up: engine-clickhouse-up engine-mssql-up engine-mysql-up engine-postgres-up engine-spark-up engine-trino-up

engine-down: engine-clickhouse-down engine-mssql-down engine-mysql-down engine-postgres-down engine-spark-down engine-trino-down

fast-test:
	pytest -n auto -m "fast and not cicdonly" --junitxml=test-results/junit-fast-test.xml && pytest -m "isolated" && pytest -m "registry_isolation" && pytest -m "dialect_isolated"

slow-test:
	pytest -n auto -m "(fast or slow) and not cicdonly" && pytest -m "isolated" && pytest -m "registry_isolation" && pytest -m "dialect_isolated"

cicd-test:
	pytest -n auto -m "fast or slow" --junitxml=test-results/junit-cicd.xml && pytest -m "isolated" && pytest -m "registry_isolation" && pytest -m "dialect_isolated"

core-fast-test:
	pytest -n auto -m "fast and not web and not github and not dbt and not jupyter"

core-slow-test:
	pytest -n auto -m "(fast or slow) and not web and not github and not dbt and not jupyter"

engine-slow-test:
	pytest -n auto -m "(fast or slow) and engine"

engine-docker-test:
	pytest -n auto -m "docker and engine"

engine-remote-test:
	pytest -n auto -m "remote and engine"

engine-test:
	pytest -n auto -m "engine"

dbt-test:
	pytest -n auto -m "dbt and not cicdonly"

dbt-fast-test:
	pytest -n auto -m "dbt and fast" --reruns 3

github-test:
	pytest -n auto -m "github"

jupyter-test:
	pytest -n auto -m "jupyter"

web-test:
	pytest -n auto -m "web"

guard-%:
	@ if [ "${${*}}" = "" ]; then \
		echo "Environment variable $* not set"; \
		exit 1; \
	fi

engine-%-install:
	$(PIP) install -e ".[dev,web,slack,lsp,${*}]" ./examples/custom_materializations

engine-docker-%-up:
	docker compose -f ./tests/core/engine_adapter/integration/docker/compose.${*}.yaml up -d
	./.circleci/wait-for-db.sh ${*}

engine-%-up: engine-%-install engine-docker-%-up
	@echo "Engine '${*}' is up and running"

engine-%-down:
	docker compose -f ./tests/core/engine_adapter/integration/docker/compose.${*}.yaml down -v

##################
# Docker Engines #
##################

clickhouse-test: engine-clickhouse-up
	pytest -n auto -m "clickhouse" --reruns 3 --junitxml=test-results/junit-clickhouse.xml

duckdb-test: engine-duckdb-install
	pytest -n auto -m "duckdb" --reruns 3 --junitxml=test-results/junit-duckdb.xml

mssql-test: engine-mssql-up
	pytest -n auto -m "mssql" --reruns 3 --junitxml=test-results/junit-mssql.xml

mysql-test: engine-mysql-up
	pytest -n auto -m "mysql" --reruns 3 --junitxml=test-results/junit-mysql.xml

postgres-test: engine-postgres-up
	pytest -n auto -m "postgres" --reruns 3 --junitxml=test-results/junit-postgres.xml

spark-test: engine-spark-up
	pytest -n auto -m "spark" --reruns 3 --junitxml=test-results/junit-spark.xml && pytest -n auto -m "pyspark" --reruns 3 --junitxml=test-results/junit-pyspark.xml

trino-test: engine-trino-up
	pytest -n auto -m "trino" --reruns 3 --junitxml=test-results/junit-trino.xml

risingwave-test: engine-risingwave-up
	pytest -n auto -m "risingwave" --reruns 3 --junitxml=test-results/junit-risingwave.xml

#################
# Cloud Engines #
#################

snowflake-test: guard-SNOWFLAKE_ACCOUNT guard-SNOWFLAKE_WAREHOUSE guard-SNOWFLAKE_DATABASE guard-SNOWFLAKE_USER engine-snowflake-install
	pytest -n auto -m "snowflake" --reruns 3 --junitxml=test-results/junit-snowflake.xml

bigquery-test: guard-BIGQUERY_KEYFILE engine-bigquery-install
	$(PIP) install -e ".[bigframes]"
	pytest -n auto -m "bigquery" --reruns 3 --junitxml=test-results/junit-bigquery.xml

databricks-test: guard-DATABRICKS_CATALOG guard-DATABRICKS_SERVER_HOSTNAME guard-DATABRICKS_HTTP_PATH guard-DATABRICKS_CONNECT_VERSION engine-databricks-install
	$(PIP) install 'databricks-connect==${DATABRICKS_CONNECT_VERSION}'
	pytest -n auto -m "databricks" --reruns 3 --junitxml=test-results/junit-databricks.xml

redshift-test: guard-REDSHIFT_HOST guard-REDSHIFT_USER guard-REDSHIFT_PASSWORD guard-REDSHIFT_DATABASE engine-redshift-install
	pytest -n auto -m "redshift" --reruns 3 --junitxml=test-results/junit-redshift.xml

clickhouse-cloud-test: guard-CLICKHOUSE_CLOUD_HOST guard-CLICKHOUSE_CLOUD_USERNAME guard-CLICKHOUSE_CLOUD_PASSWORD engine-clickhouse-install
	pytest -n 1 -m "clickhouse_cloud" --reruns 3 --junitxml=test-results/junit-clickhouse-cloud.xml

athena-test: guard-AWS_ACCESS_KEY_ID guard-AWS_SECRET_ACCESS_KEY guard-ATHENA_S3_WAREHOUSE_LOCATION engine-athena-install
	pytest -n auto -m "athena" --reruns 3 --junitxml=test-results/junit-athena.xml

fabric-test: guard-FABRIC_HOST guard-FABRIC_CLIENT_ID guard-FABRIC_CLIENT_SECRET guard-FABRIC_DATABASE engine-fabric-install
	pytest -n auto -m "fabric" --reruns 3 --junitxml=test-results/junit-fabric.xml

gcp-postgres-test: guard-GCP_POSTGRES_INSTANCE_CONNECTION_STRING guard-GCP_POSTGRES_USER guard-GCP_POSTGRES_PASSWORD guard-GCP_POSTGRES_KEYFILE_JSON engine-gcppostgres-install
	pytest -n auto -m "gcp_postgres" --reruns 3 --junitxml=test-results/junit-gcp-postgres.xml

vscode_settings:
	mkdir -p .vscode
	cp -r ./tooling/vscode/*.json .vscode/

vscode-generate-openapi:
	python3 web/server/openapi.py --output vscode/openapi.json
	pnpm run fmt
	cd vscode/react && pnpm run generate:api

benchmark-ci:
	python benchmarks/lsp_render_model_bench.py --debug-single-value
