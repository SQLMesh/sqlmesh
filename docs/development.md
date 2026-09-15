# Contribute to development

SQLMesh is licensed under [Apache 2.0](https://github.com/SQLMesh/sqlmesh/blob/main/LICENSE). We encourage community contribution and would love for you to get involved. The following document outlines the process to contribute to SQLMesh.

## Prerequisites

Before you begin, ensure you have the following installed on your machine. Exactly how to install these is dependent on your operating system.

* Docker
* Docker Compose V2
* OpenJDK >= 11
* Python >= 3.9 < 3.13

### Windows Prerequisites

The development environment of SQLMesh depends both on:

* Symbolic links in the repository which, whilst available on Windows, typically require additional permissions for the process running git, and;
* Some Python functionality (e.g. `SIGUSR1`) that is only available on UNIX systems. Whilst this functionality is gated so shouldn't error on Windows, the development container enables its use.

For the Python functionality, a development container is provided to develop against Ubuntu 24 with Python 3.12.

For symbolic links, you must ensure that when checking out the repository:

* The git configuration `core.symlinks` is set to `true` (this also needs to be done before bind mount, i.e. when the development container is started)
* The process that git runs as is permitted to create symbolic links. This can typically be done by running git as an administrator, or enabling [developer mode on Windows](https://learn.microsoft.com/en-us/windows/advanced-settings/developer-mode).

Development containers are supported by [a number of IDEs](https://containers.dev/supporting.html). For developers using VSCode,  [Microsoft has a tutorial on how to use development containers](https://code.visualstudio.com/docs/devcontainers/tutorial).

## Virtual environment setup

We do recommend using a virtual environment to develop SQLMesh.

```bash
python -m venv .venv
source .venv/bin/activate
```

Once you have activated your virtual environment, you can install the dependencies by running the following command.

```bash
make install-dev
```

Optionally, you can use pre-commit to automatically run linters/formatters:

```bash
make install-pre-commit
```

## Python development

Run linters and formatters:

```bash
make style
```

Run faster tests for quicker local feedback:

```bash
make fast-test
```

Run more comprehensive tests that run on each commit:

```bash
make slow-test
```

## Documentation

In order to run the documentation server, you will need to install the dependencies by running the following command.

```bash
make install-doc
```

Once you have installed the dependencies, you can run the documentation server by running the following command.

```bash
make docs-serve
```

Run docs tests:

```bash
make doc-test
```

## UI development

In addition to the Python development, you can also develop the UI.

The UI is built using React and Typescript. To run the UI, you will need to install the dependencies by running the following command.

```bash
pnpm install
```

Run ide:

```bash
make ui-up
```

## Developing the VSCode extension

Similar to UI development, you can also develop the VSCode extension. To do so, make sure you have the dependencies installed by running the following command inside the `vscode/extension` directory.

```bash
pnpm install
```

Once that is done, developing the VSCode extension is most easily done by launching the `Run Extensions` debug task from a Visual Studio Code workspace opened at the root of the SQLMesh repository. By default, the VSCode extension will run the SQLMesh server locally and open a new Visual Studio Code window that allows you to try out the SQLMesh IDE. It opens the `examples/sushi` project by default. To set up Visual Studio Code to run the `Run Extensions` debug task, you can run the following command which will copy the `launch.json` and `tasks.json` files to the `.vscode` directory.

```bash 
make vscode_settings
```
