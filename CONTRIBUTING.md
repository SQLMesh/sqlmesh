# Contributing to SQLMesh

## Welcome

SQLMesh is a project of the Linux Foundation. We welcome contributions from anyone — whether you're fixing a bug, improving documentation, or proposing a new feature.

## Technical Steering Committee (TSC)

The TSC is responsible for technical oversight of the SQLMesh project, including coordinating technical direction, approving contribution policies, and maintaining community norms.

Initial TSC voting members are the project's Maintainers:

| Name                | GitHub Handle | Affiliation    | Role       |
|---------------------|---------------|----------------|------------| 
| Alexander Butler    | z3z1ma        | Harness        | TSC Member |
| Alexander Filipchik | afilipchik    | Cloud Kitchens | TSC Member |
| Reid Hooper         | rhooper9711   | Benzinga       | TSC Member |
| Yuki Kakegawa       | StuffbyYuki   | Jump.ai        | TSC Member |
| Toby Mao            | tobymao       | Fivetran       | TSC Chair  |
| Alex Wilde          | alexminerv    | Minerva        | TSC Member |


## Roles

**Contributors**: Anyone who contributes code, documentation, or other technical artifacts to the project.

**Maintainers**: Contributors who have earned the ability to modify source code, documentation, or other technical artifacts. A Contributor may become a Maintainer by majority approval of the TSC. A Maintainer may be removed by majority approval of the TSC.

## How to Contribute

1. Fork the repository on GitHub
2. Create a branch for your changes
3. Make your changes and commit them with a sign-off (see DCO section below)
4. Submit a pull request against the `main` branch

File issues at [github.com/sqlmesh/sqlmesh/issues](https://github.com/sqlmesh/sqlmesh/issues).

## Developer Certificate of Origin (DCO)

All contributions must include a `Signed-off-by` line in the commit message per the [Developer Certificate of Origin](DCO). This certifies that you wrote the contribution or have the right to submit it under the project's open source license.

Use `git commit -s` to add the sign-off automatically:

```bash
git commit -s -m "Your commit message"
```

To fix a commit that is missing the sign-off:

```bash
git commit --amend -s
```

To add a sign-off to multiple commits:

```bash
git rebase HEAD~N --signoff
```

## Development Setup

See [docs/development.md](docs/development.md) for full setup instructions. Key commands:

```bash
python -m venv .venv
source .venv/bin/activate
make install-dev
make style    # Run before submitting
make fast-test # Quick test suite
```

## Coding Standards

- Run `make style` before submitting a pull request
- Follow existing code patterns and conventions in the codebase
- New files should include an SPDX license header:
  ```python
  # SPDX-License-Identifier: Apache-2.0
  ```

## Pull Request Process

- Describe your changes clearly in the pull request description
- Ensure all CI checks pass
- Include a DCO sign-off on all commits (`git commit -s`)
- Be responsive to review feedback from maintainers

## Licensing

Code contributions are licensed under the [Apache License 2.0](LICENSE). Documentation contributions are licensed under [Creative Commons Attribution 4.0 International (CC-BY-4.0)](https://creativecommons.org/licenses/by/4.0/). See the LICENSE file and the [technical charter](sqlmesh-technical-charter.pdf) for details.
