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

## Rolling back a migration

Before `sqlmesh migrate` changes the project metadata, it copies each state table to a backup table with a `_backup` suffix. The `sqlmesh rollback` command restores the metadata from those backups, returning it to the format used by the SQLMesh version that was installed before the migration. If a migration fails partway through, SQLMesh rolls it back automatically.

To undo a migration:

1. Run `sqlmesh rollback` with the SQLMesh version that performed the migration still installed.
2. Reinstall the SQLMesh version the project used before the upgrade, for example by reverting the version change in your requirements file.

The second step is required. Rolling back does not change the installed version of SQLMesh, and the newer version will refuse to run against the restored metadata until it is migrated again:

```bash
> sqlmesh plan
Error: SQLMesh (local) is using version '2' which is ahead of '1' (remote). Please run a migration ('sqlmesh migrate' command).
```

Keep the following in mind before rolling back:

- Only the most recent migration can be rolled back. Restoring consumes the backup tables, so running `sqlmesh rollback` a second time fails with `There are no prior migrations to roll back to.`
- The backups are taken at the moment of migration. Any changes made to the project metadata after the migration, such as plans applied with the newer version, are discarded.
- Like `sqlmesh migrate`, rolling back affects all users of the project and should be issued manually by a single user.
