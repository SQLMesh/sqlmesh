# Releasing SQLMesh

This guide is for maintainers with write access to `SQLMesh/sqlmesh`. It covers the three things the project publishes:

| Package | Registry | How a release starts |
|---|---|---|
| `sqlmesh` (Python) | PyPI | Push a `v*.*.*` tag |
| SQLMesh VS Code extension | VS Code Marketplace and Open VSX | Run the **Release VSCode Extension** workflow |
| `@tobikodata/sqlmesh-common` | npm | Run the **Release web common code** workflow |

## Release the Python Package

The [`Release`](.github/workflows/release.yaml) workflow runs when a tag that matches `v*.*.*` is pushed. The package version comes from the tag through `setuptools_scm`, so you don't edit a version number in the code.

The workflow does four things:

1. Builds the web UI and bundles it into the package.
2. Publishes `sqlmesh` to PyPI with `make publish`.
3. Publishes the test package to the private `tobiko-private` registry with `make publish-tests`.
4. Creates a GitHub release with notes generated from the pull requests merged since the previous tag.

Each step publishes immediately and there's no approval gate. Check `main` before you tag.

### Steps

1. Pick the commit to release. It must be on `main`, and every required check must pass on it. Check the latest **pr.yaml** run for that commit on the [Actions page](https://github.com/SQLMesh/sqlmesh/actions/workflows/pr.yaml).
2. Pick the version. Fetch the tags, then find the highest released version and increment it:

    ```bash
    git fetch <sqlmesh-remote> main --tags
    git tag --list 'v[0-9]*' --sort=-v:refname | head -1
    ```

    Replace `<sqlmesh-remote>` with your remote for `SQLMesh/sqlmesh`.

3. Tag the commit and push the tag. Set `VERSION` to the version from step 2, for example `v0.236.5`:

    ```bash
    VERSION=v0.236.5
    git tag "$VERSION" <commit-sha>
    git push <sqlmesh-remote> "$VERSION"
    ```

    Push only the tag, not a branch.

4. Watch the [Release](https://github.com/SQLMesh/sqlmesh/actions/workflows/release.yaml) run until it finishes.
5. Check that the new version is on [PyPI](https://pypi.org/project/sqlmesh/#history).
6. Edit the generated release on the [Releases page](https://github.com/SQLMesh/sqlmesh/releases). Under **Breaking changes**, add a short upgrade note for each entry. Say what users will see after upgrading, such as a migration or a metadata-only change in `sqlmesh plan`.

If the workflow fails after the PyPI step, don't delete or reuse the tag. PyPI doesn't accept the same version twice. Fix the cause and release the next version.

### Release Notes

GitHub builds the notes from merged pull request titles and groups them by label, as configured in [`.github/release.yml`](.github/release.yml). The [PR Labels](.github/workflows/pr-labels.yaml) workflow applies the labels from each pull request's title (`type(scope): description`), and adds `migration` to pull requests that add a state migration. See [Pull Request Titles](CONTRIBUTING.md#pull-request-titles) in `CONTRIBUTING.md`.

- A `!` in the title, such as `fix!:` or `feat!:`, puts the pull request under **Breaking changes**.
- A pull request that adds a state migration goes under **State migrations**.
- CI, test and Dependabot pull requests are left out. Add the `skip-changelog` label to leave out any other pull request.

Each pull request appears in only one section: the first one in this list that matches its labels. A breaking change that also adds a state migration is listed under **Breaking changes**, so its upgrade note in step 6 should say to run `sqlmesh migrate`.

To move a pull request to another section, change its title. The workflow resets the type labels from the title.

## Release the VS Code Extension

The [Release VSCode Extension](.github/workflows/release_extension.yaml) workflow publishes to the VS Code Marketplace and to Open VSX.

1. On the workflow's page, choose **Run workflow**.
2. Select the `main` branch. The workflow stops if you run it from any other branch.
3. Enter the version without a `v`, for example `0.0.8`.

The workflow sets the version only in its own build. It doesn't commit it, so `vscode/extension/package.json` keeps its old version. Check the [Marketplace listing](https://marketplace.visualstudio.com/items?itemName=tobikodata.sqlmesh) for the latest published version and enter a higher one.

## Release the Shared Web Package

The [Release web common code](.github/workflows/release_shared_js.yaml) workflow publishes `@tobikodata/sqlmesh-common` from `web/common` to npm. Run it the same way as the extension workflow: from `main`, with a version that has no `v`. It also doesn't commit the version, so check [npm](https://www.npmjs.com/package/@tobikodata/sqlmesh-common) for the latest published version first.
