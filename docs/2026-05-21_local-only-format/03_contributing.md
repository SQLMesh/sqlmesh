# Contributing to SQLMesh

This reference guide details the standards, local environments, testing frameworks, and submission workflows required when contributing to SQLMesh. Since you have already forked the repository and started a development branch (`fix/local-only-format`), this document focuses on what happens from this point forward.

---

## 1. Core Contribution Principles

SQLMesh is a Linux Foundation project and is licensed under the **Apache License 2.0**. Understanding community expectations and legal baselines helps avoid friction during your pull request (PR).

### Community Policies
*   **Code of Conduct (`CODE_OF_CONDUCT.md`)**: SQLMesh adheres to the [LF Projects Code of Conduct](https://lfprojects.org/policies/code-of-conduct/). All community participants are expected to uphold these standards.
*   **General Expectations (`CONTRIBUTING.md`)**: Outlines project roles (Contributors, Maintainers, and the Technical Steering Committee), file licensing expectations, and general contribution workflow steps.

### Legal Safeguards
*   **Developer Certificate of Origin (DCO)**: All commits must be signed off to certify that you have the right to submit the code under the project's license. Your commit messages must include a `Signed-off-by` line.
    *   *How to commit with sign-off:* Use the `-s` flag when committing:
        ```bash
        git commit -s -m "Your commit message"
        ```
    *   *How to sign off existing commits:*
        *   Amending the latest commit: `git commit --amend -s`
        *   Rebasing multiple commits: `git rebase HEAD~N --signoff`
*   **License Headers**: Any new file introduced to the codebase must begin with the SPDX license header:
    ```python
    # SPDX-License-Identifier: Apache-2.0
    ```

---

## 2. Developer Environment Setup

The primary configuration for SQLMesh is defined in the root `pyproject.toml` and orchestrated using the project's `Makefile`.

### Prerequisites
*   **Python**: `>= 3.9` and `< 3.13` (Python 3.13+ is not yet supported).
*   **Java**: OpenJDK `>= 11` (required for certain engine tests such as Spark).
*   **Docker & Docker Compose V2**: Required for running containerized database engines.

### Environment Setup Commands
Always work within a virtual environment. Use these commands (detailed in `docs/development.md` and `Makefile`):

1.  **Create and Activate Virtual Environment**:
    ```bash
    python -m venv .venv
    source .venv/bin/activate  # On Windows: .venv\Scripts\activate
    ```
2.  **Install Development Dependencies**:
    ```bash
    make install-dev
    ```
    This targets the `dev` extra in `pyproject.toml` (`[project.optional-dependencies]`) along with web, slack, dlt, and lsp extras:
    ```bash
    pip install -e ".[dev,web,slack,dlt,lsp]" ./examples/custom_materializations
    ```
3.  **Setup Pre-Commit Hooks**:
    ```bash
    make install-pre-commit
    ```
    This registers the pre-commit configuration in `.pre-commit-config.yaml` with git.

---

## 3. Local Verification (Run Before You Push)

To maintain a clean main branch, contributors must verify code style, static typing, and basic test suites locally before pushing their branch.

### Linting and Formatting
SQLMesh uses **Ruff** for linting/formatting and **MyPy** for static type checks, automated via `pre-commit` hooks.

*   **Style Verification**: Run the style command before every push:
    ```bash
    make style
    ```
    This executes `pre-commit run --all-files`, which runs:
    *   `ruff check --force-exclude --fix --ignore E721 --ignore E741` on python files.
    *   `ruff format --force-exclude --line-length 100` for PEP 8 compliance.
    *   `mypy` for static typing (configured in `pyproject.toml` to enforce `disallow_untyped_defs = true` on the core library, with relaxed rules for tests and migrations).
    *   `valid migrations` using `tooling/validating_migration_numbers.sh` to ensure migration sequences are consecutive and have no gaps or overlaps.
*   **Python-Only Style**: If you only touched python files and want to skip node/frontend linters, run:
    ```bash
    make py-style
    ```

---

## 4. Testing Framework and Conventions

The SQLMesh test suite lives in the `tests/` directory. Tests are categorised and run using `pytest` markers (defined in `pyproject.toml`).

### Organization of Tests
*   **Unit & Integration Tests**: Organized within subdirectories under `tests/` matching core modules (e.g., `tests/core/`, `tests/dbt/`, `tests/web/`, etc.).
*   **Engine-Specific Tests**: Integration tests that verify SQLMesh's behavior against physical databases.

### Pytest Markers
Pytest markers control execution speed and environment requirements. Key markers include:
*   `fast`: Tests that execute rapidly without database interactions (default when no markers are supplied).
*   `slow`: Tests involving local DB file/memory interactions (such as DuckDB).
*   `docker`: Tests that require running localized Docker containers.
*   `remote`: Tests that interact with cloud database endpoints.
*   `isolated` / `dialect_isolated` / `registry_isolation`: Tests that must be run sequentially or separately to avoid shared state contamination.

### Running Tests Locally
Depending on the scope of your changes, use the corresponding Makefile target:

*   **Fast Suite**: Run unit and quick tests:
    ```bash
    make fast-test
    ```
    *Maps to: `pytest -n auto -m "fast and not cicdonly" ...` followed by isolated test suites.*
*   **Slow Suite**: Run all local unit and integration tests (including slow ones):
    ```bash
    make slow-test
    ```
    *Maps to: `pytest -n auto -m "(fast or slow) and not cicdonly" ...` followed by isolated test suites.*
*   **Doctests**: Verify examples embedded in docstrings:
    ```bash
    make doc-test
    ```
*   **Engine-Specific Unit Tests**:
    *   **DuckDB**: Run unit tests targeting DuckDB adapter:
        ```bash
        make duckdb-test
        ```
    *   **Dockerized Engines** (Postgres, MySQL, MSSQL, ClickHouse, Trino, Spark, RisingWave):
        To run these, you must bring the engine up in Docker first, run the tests, and optionally tear down:
        ```bash
        # Bring up Postgres in Docker and run tests
        make engine-postgres-up
        make postgres-test
        make engine-postgres-down
        ```
        *Note: Individual engine targets automatically execute standard Docker Compose files located under `tests/core/engine_adapter/integration/docker/`.*
    *   **Cloud Engines** (Snowflake, BigQuery, Databricks, Redshift, Athena, Fabric, GCP-Postgres):
        These require passing corresponding credentials via environment variables (e.g., `SNOWFLAKE_ACCOUNT`, `BIGQUERY_KEYFILE`, etc.) and executing the respective targets (e.g., `make snowflake-test`).

---

## 5. Agent & AI-Assisted Workflow

SQLMesh contains explicit expectations for contributors using AI assistants or agents (detailed in `CLAUDE.md`). The project relies on a structured, Test-Driven Development (TDD) cycle.

### The Agent Loop
When implementing features or bug fixes, you should navigate the code in a systematic loop:
1.  **Understand**: Explore the codebase, review existing design patterns, and read relevant GitHub issues.
2.  **TDD (Failing Test First)**: *Always begin by writing a failing test (or tests)* that reproduces the bug or asserts the expected behavior of the new feature before editing the source files.
3.  **Implement**: Make the smallest surgical change that solves the issue. Avoid speculative coding.
4.  **Code Review**: Hand off your implementation to an automated review or use a code-reviewer agent to identify edge cases, test coverage gaps, and style compliance.
5.  **Iterate**: Fix any review items, verify the tests pass, and proceed.
6.  **Document**: Write or update user-facing documentation for any user-visible behaviors.

---

## 6. Commit and PR Workflow

### Commit Conventions
Based on the repository's git history, commits follow a semantic naming convention. Maintainers merge PRs using squash-commits, so clean commit messages are highly valued.
*   **Prefixes**: Use semantic prefixes to describe the nature of your change:
    *   `fix:` or `Fix:` for bug fixes.
    *   `feat:` or `Feat:` for new features.
    *   `chore:` or `Chore:` for dependency bumps, configuration, or structural updates.
    *   `docs:` or `(docs):` for documentation edits.
    *   Add a `!` prefix (e.g., `Chore!:`) for breaking changes.
*   **Engine Scopes**: If the change is engine-specific, denote the engine in parentheses:
    *   `Fix (databricks): use shared connection pool...`
*   **PR Reference**: Keep commit titles clean. When merging, the PR number is appended (e.g., `(#5801)`).
*   *Mandatory Requirement:* Every commit must contain the DCO sign-off (`Signed-off-by`).

### Submission Process
1.  **Branch Target**: All pull requests must be opened against the `main` branch of `sqlmesh/sqlmesh`.
2.  **PR Template**: Fill out `.github/pull_request_template.md` completely. It requires:
    *   **Description**: A clear summary of the changes.
    *   **Test Plan**: Specific steps on how you tested the changes.
    *   **Checklist**: Check boxes certifying you ran `make style`, added tests, verified `make fast-test`, and signed off all commits.
3.  **Reviewers & CODEOWNERS**: There is no explicit `CODEOWNERS` file in the repository. Instead, community maintainers (such as Alexander Butler `z3z1ma`, Toby Mao `tobymao`, or Yuki Kakegawa `StuffbyYuki`) manually review and assign reviewers.

### Required CI Checks (GitHub Actions)
The PR workflow (`.github/workflows/pr.yaml`) runs an extensive validation suite:
*   **`doc-tests`**: Runs `make doc-test` to verify docstring examples.
*   **`style-and-cicd-tests`**: Runs `make py-style`, `make benchmark-ci`, and `make cicd-test` across a Python version matrix (`3.9`, `3.10`, `3.11`, `3.12`, `3.13`) on Ubuntu.
*   **`cicd-tests-windows`**: Runs `make fast-test` on Windows.
*   **`migration-test`**: Validates migrations by running integration tests under database schema upgrades using `./.github/scripts/test_migration.sh`.
*   **`ui-test`**: Runs frontend unit and Playwright e2e tests for the web UI.
*   **`engine-tests-docker`**: Executes test suites for containerized engines (`duckdb`, `postgres`, `mysql`, `mssql`, `trino`, `spark`, `clickhouse`, `risingwave`).
*   **`test-dbt-versions`**: Validates compatibility across multiple dbt versions (`1.3` through `1.10`) using `make dbt-fast-test`.

---

## 7. Documentation Contributions

If your changes alter user-visible APIs, configurations, or behaviors, you must update the documentation (written in connected prose following `docs/HOWTO.md`).

### Structure and Location
*   Documentation markdown files live in the `docs/` folder.
*   Configuration guides are located under `docs/guides/configuration.md`.
*   Integration-specific documents are located under `docs/integrations/`.

### Previewing Documentation
1.  **Install Docs Dependencies**:
    ```bash
    make install-doc
    ```
    *If version conflicts occur, run the fallback command:*
    ```bash
    pip install mkdocs mkdocs-include-markdown-plugin mkdocs-material mkdocs-material-extensions mkdocs-glightbox pdoc
    ```
2.  **Serve Locally**:
    ```bash
    make docs-serve
    ```
    This launches a local MkDocs development server at `http://127.0.0.1:8000/` that hot-reloads as you edit markdown files.

### Style and Tone Guidelines
*   **Cognitive Load**: SQLMesh is powerful and complex; focus on minimizing cognitive load for readers.
*   **Instructional Voice**: Use the second-person voice for instructions ("Add an audit...", "You can partition...").
*   **Narrative Voice**: Use the first-person plural for walk-throughs or narratives ("First, we create a new environment...").
*   **Tone**: Prefer active voice over passive voice, and keep sentences short.
*   **Review Flow**: For larger doc additions, the Technical Writer/Editor (Trey) may perform a full review and editing pass by opening a PR directly *against your branch* before team approval.

---

## 8. Summary Checklist for Contributors

Before pushing and submitting your PR, ensure you can tick off all items on this list:
- [ ] No new files are missing the Apache 2.0 license header.
- [ ] All commits are signed off (`git commit -s`).
- [ ] Code is formatted and typed (`make style`).
- [ ] No gaps or overlaps exist in any newly added migration numbers.
- [ ] The local fast test suite passes (`make fast-test`).
- [ ] The `.github/pull_request_template.md` is fully completed.
