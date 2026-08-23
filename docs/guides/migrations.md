# Migrations guide

New versions of SQLMesh may be incompatible with the project's stored metadata format. Migrations provide a way to upgrade the project metadata format to operate with the new SQLMesh version.

## Detecting incompatibility
When issuing a SQLMesh command, SQLMesh will automatically check for incompatibilities between the installed version of SQLMesh and the project's metadata format, prompting what action is required. SQLMesh commands will not execute until the action is complete.

### Installed version is newer than metadata format
In this scenario, the project's metadata format needs to be migrated.

```bash
> sqlmesh plan my_dev
Error: SQLMesh (local) is using version '2' which is ahead of '1' (remote). Please run a migration ('sqlmesh migrate' command).
```

### Installed version is older than metadata format
Here, the installed version of SQLMesh needs to be upgraded.

```bash
> sqlmesh plan my_dev
SQLMeshError: SQLMesh (local) is using version '1' which is behind '2' (remote). Please upgrade SQLMesh.
```

## How to migrate

### Built-in Scheduler Migrations

The project metadata can be migrated to the latest metadata format using SQLMesh's migrate command.

```bash
sqlmesh migrate
```

Migration should be issued manually by a single user and the migration will affect all users of the project. 
Migrations should ideally run when no one will be running plan/apply. 
Migrations should not be run in parallel. 
Due to these constraints, it is better for a person responsible for managing SQLMesh to manually issue migrations. 
Therefore, it is not recommended to issue migrations from CI/CD pipelines.
