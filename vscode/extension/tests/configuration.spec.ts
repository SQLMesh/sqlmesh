import { test, expect } from './fixtures'
import {
  createVirtualEnvironment,
  openServerPage,
  pipInstall,
  REPO_ROOT,
  SUSHI_SOURCE_PATH,
  waitForLoadedSQLMesh,
} from './utils'
import path from 'path'
import fs from 'fs-extra'

async function setupPythonEnvironment(tempDir: string): Promise<void> {
  // Create a temporary directory for the virtual environment
  const venvDir = path.join(tempDir, '.venv')
  fs.mkdirSync(venvDir, { recursive: true })

  // Create virtual environment
  const pythonDetails = await createVirtualEnvironment(venvDir)

  // Install sqlmesh from the local repository with LSP support
  const customMaterializations = path.join(
    REPO_ROOT,
    'examples',
    'custom_materializations',
  )
  const sqlmeshWithExtras = `${REPO_ROOT}[lsp,bigquery]`
  await pipInstall(pythonDetails, [sqlmeshWithExtras, customMaterializations])
}

/**
 * Creates an entrypoint file used to test the LSP configuration.
 *
 * The entrypoint file is a bash script that simply calls out to the
 */
const createEntrypointFile = (
  tempDir: string,
  entrypointFileName: string,
  bitToStripFromArgs = '',
): {
  entrypointFile: string
  fileWhereStoredInputs: string
  fileWhereStoredTelemetryPreference: string
} => {
  const entrypointFile = path.join(tempDir, entrypointFileName)
  const fileWhereStoredInputs = path.join(tempDir, 'inputs.txt')
  const fileWhereStoredTelemetryPreference = path.join(
    tempDir,
    'telemetry-preference.txt',
  )
  const sqlmeshLSPFile = path.join(tempDir, '.venv/bin/sqlmesh_lsp')

  // Create the entrypoint file
  fs.writeFileSync(
    entrypointFile,
    `#!/bin/bash
echo "$@" > ${fileWhereStoredInputs}
echo "\${SQLMESH__DISABLE_ANONYMIZED_ANALYTICS:-}" > ${fileWhereStoredTelemetryPreference}
# Strip bitToStripFromArgs from the beginning of the args if it matches
if [[ "$1" == "${bitToStripFromArgs}" ]]; then
  shift
fi
# Call the sqlmesh_lsp with the remaining arguments
${sqlmeshLSPFile} "$@"`,
    { mode: 0o755 }, // Make it executable
  )

  return {
    entrypointFile,
    fileWhereStoredInputs,
    fileWhereStoredTelemetryPreference,
  }
}

test.describe('Test LSP Entrypoint configuration', () => {
  test('specify single entrypoint relative path', async ({
    page,
    sharedCodeServer,
    tempDir,
  }) => {
    await fs.copy(SUSHI_SOURCE_PATH, tempDir)

    await setupPythonEnvironment(tempDir)

    const { fileWhereStoredInputs, fileWhereStoredTelemetryPreference } =
      createEntrypointFile(tempDir, 'entrypoint.sh')

    const settings = {
      'sqlmesh.lspEntrypoint': './entrypoint.sh',
    }
    // Write the settings to the settings.json file
    const settingsPath = path.join(tempDir, '.vscode', 'settings.json')
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true })
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2))

    await openServerPage(page, tempDir, sharedCodeServer)

    //   Wait for the models folder to be visible
    await page.waitForSelector('text=models')

    // Click on the models folder, excluding external_models
    await page
      .getByRole('treeitem', { name: 'models', exact: true })
      .locator('a')
      .click()

    // Open the customer_revenue_lifetime model
    await page
      .getByRole('treeitem', { name: 'customers.sql', exact: true })
      .locator('a')
      .click()

    await waitForLoadedSQLMesh(page)

    // Check that the output file exists and contains the entrypoint script arguments
    expect(fs.existsSync(fileWhereStoredInputs)).toBe(true)
    expect(fs.readFileSync(fileWhereStoredInputs, 'utf8')).toBe(`--stdio
`)
    // The e2e code-server is launched with --disable-telemetry. Verify the
    // packaged extension propagates that global preference to the real LSP
    // child process, not only to a mocked environment builder.
    expect(fs.readFileSync(fileWhereStoredTelemetryPreference, 'utf8')).toBe(
      'true\n',
    )
  })

  test('specify one entrypoint absolute path', async ({
    page,
    sharedCodeServer,
    tempDir,
  }) => {
    await fs.copy(SUSHI_SOURCE_PATH, tempDir)

    await setupPythonEnvironment(tempDir)

    const { entrypointFile, fileWhereStoredInputs } = createEntrypointFile(
      tempDir,
      'entrypoint.sh',
    )
    // Assert that the entrypoint file is an absolute path
    expect(path.isAbsolute(entrypointFile)).toBe(true)

    const settings = {
      'sqlmesh.lspEntrypoint': `${entrypointFile}`,
    }
    // Write the settings to the settings.json file
    const settingsPath = path.join(tempDir, '.vscode', 'settings.json')
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true })
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2))

    await openServerPage(page, tempDir, sharedCodeServer)

    //   Wait for the models folder to be visible
    await page.waitForSelector('text=models')

    // Click on the models folder, excluding external_models
    await page
      .getByRole('treeitem', { name: 'models', exact: true })
      .locator('a')
      .click()

    // Open the customer_revenue_lifetime model
    await page
      .getByRole('treeitem', { name: 'customers.sql', exact: true })
      .locator('a')
      .click()

    await waitForLoadedSQLMesh(page)

    // Check that the output file exists and contains the entrypoint script arguments
    expect(fs.existsSync(fileWhereStoredInputs)).toBe(true)
    expect(fs.readFileSync(fileWhereStoredInputs, 'utf8')).toBe(`--stdio
`)
  })

  test('specify entrypoint with arguments', async ({
    page,
    sharedCodeServer,
    tempDir,
  }) => {
    await fs.copy(SUSHI_SOURCE_PATH, tempDir)

    await setupPythonEnvironment(tempDir)

    const { fileWhereStoredInputs } = createEntrypointFile(
      tempDir,
      'entrypoint.sh',
      '--argToIgnore',
    )

    const settings = {
      'sqlmesh.lspEntrypoint': './entrypoint.sh --argToIgnore',
    }
    // Write the settings to the settings.json file
    const settingsPath = path.join(tempDir, '.vscode', 'settings.json')
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true })
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2))

    await openServerPage(page, tempDir, sharedCodeServer)

    //   Wait for the models folder to be visible
    await page.waitForSelector('text=models')

    // Click on the models folder, excluding external_models
    await page
      .getByRole('treeitem', { name: 'models', exact: true })
      .locator('a')
      .click()

    // Open the customer_revenue_lifetime model
    await page
      .getByRole('treeitem', { name: 'customers.sql', exact: true })
      .locator('a')
      .click()

    await waitForLoadedSQLMesh(page)

    // Check that the output file exists and contains the entrypoint script arguments
    expect(fs.existsSync(fileWhereStoredInputs)).toBe(true)
    expect(fs.readFileSync(fileWhereStoredInputs, 'utf8'))
      .toBe(`--argToIgnore --stdio
`)
  })
})
