import { test, expect, SharedPythonEnvironment } from './fixtures'
import {
  openServerPage,
  SUSHI_SOURCE_PATH,
  waitForLoadedSQLMesh,
} from './utils'
import path from 'path'
import fs from 'fs-extra'

/**
 * Creates an entrypoint file used to test the LSP configuration.
 *
 * The entrypoint file is a bash script that simply calls out to the
 */
const createEntrypointFile = (
  tempDir: string,
  entrypointFileName: string,
  pythonEnvironment: SharedPythonEnvironment,
  bitToStripFromArgs = '',
): {
  entrypointFile: string
  fileWhereStoredInputs: string
} => {
  const entrypointFile = path.join(tempDir, entrypointFileName)
  const fileWhereStoredInputs = path.join(tempDir, 'inputs.txt')
  const sqlmeshLSPFile = path.join(
    path.dirname(pythonEnvironment.pythonPath),
    'sqlmesh_lsp',
  )

  // Create the entrypoint file
  fs.writeFileSync(
    entrypointFile,
    `#!/bin/bash
echo "$@" > ${fileWhereStoredInputs}
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
  }
}

test.describe('Test LSP Entrypoint configuration', () => {
  test('specify single entrypoint relative path', async ({
    page,
    sharedCodeServer,
    sharedPythonEnvironment,
    tempDir,
  }) => {
    await fs.copy(SUSHI_SOURCE_PATH, tempDir)

    const { fileWhereStoredInputs } = createEntrypointFile(
      tempDir,
      'entrypoint.sh',
      sharedPythonEnvironment,
    )

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
  })

  test('specify one entrypoint absolute path', async ({
    page,
    sharedCodeServer,
    sharedPythonEnvironment,
    tempDir,
  }) => {
    await fs.copy(SUSHI_SOURCE_PATH, tempDir)

    const { entrypointFile, fileWhereStoredInputs } = createEntrypointFile(
      tempDir,
      'entrypoint.sh',
      sharedPythonEnvironment,
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
    sharedPythonEnvironment,
    tempDir,
  }) => {
    await fs.copy(SUSHI_SOURCE_PATH, tempDir)

    const { fileWhereStoredInputs } = createEntrypointFile(
      tempDir,
      'entrypoint.sh',
      sharedPythonEnvironment,
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
