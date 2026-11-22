import { test, Page, SharedPythonEnvironment } from './fixtures'
import fs from 'fs-extra'
import {
  createVirtualEnvironment,
  openLineageView,
  openServerPage,
  pipInstall,
  PythonEnvironment,
  REPO_ROOT,
  SUSHI_SOURCE_PATH,
  waitForLoadedSQLMesh,
} from './utils'
import path from 'path'
import { setTcloudVersion, setupAuthenticatedState } from './tcloud_utils'
import { CodeServerContext } from './utils_code_server'

function writeEnvironmentConfig(sushiPath: string) {
  const configPath = path.join(sushiPath, 'config.py')
  const originalConfig = fs.readFileSync(configPath, 'utf8')

  const newConfig =
    `
import os

test_var = os.getenv("TEST_VAR")
if test_var is None or test_var == "":
    raise Exception("TEST_VAR is not set")
` + originalConfig

  fs.writeFileSync(configPath, newConfig)
}

async function runTest(
  page: Page,
  context: CodeServerContext,
  tempDir: string,
): Promise<void> {
  await openServerPage(page, tempDir, context)
  await page.waitForSelector('text=models')
  await openLineageView(page)
}

async function writeSettings(
  tempDir: string,
  pythonDetails: PythonEnvironment,
  pythonEnvDir: string,
): Promise<void> {
  const settings = {
    'python.defaultInterpreterPath': pythonDetails.pythonPath,
    'sqlmesh.environmentPath': pythonEnvDir,
  }
  await fs.ensureDir(path.join(tempDir, '.vscode'))
  await fs.writeJson(path.join(tempDir, '.vscode', 'settings.json'), settings, {
    spaces: 2,
  })
}

/**
 * Copy the sushi project into the test directory and point it at the shared
 * virtual environment.
 */
async function setupSharedEnvironment(
  tempDir: string,
  sharedPythonEnvironment: SharedPythonEnvironment,
): Promise<void> {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)
  await writeSettings(
    tempDir,
    sharedPythonEnvironment,
    sharedPythonEnvironment.venvDir,
  )
}

/**
 * Copy the sushi project into the test directory and give it a virtual
 * environment of its own. Used by the tcloud tests, which install a mock
 * tcloud package that must not leak into other tests.
 */
async function setupEnvironment(tempDir: string): Promise<{
  pythonDetails: PythonEnvironment
}> {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)
  const pythonEnvDir = path.join(tempDir, '.venv')
  const pythonDetails = await createVirtualEnvironment(pythonEnvDir)
  const custom_materializations = path.join(
    REPO_ROOT,
    'examples',
    'custom_materializations',
  )
  const sqlmeshWithExtras = `${REPO_ROOT}[bigquery,lsp]`
  await pipInstall(pythonDetails, [sqlmeshWithExtras, custom_materializations])

  await writeSettings(tempDir, pythonDetails, pythonEnvDir)
  return { pythonDetails }
}

test.describe('python environment variable injection on sqlmesh_lsp', () => {
  test('normal setup - error ', async ({
    page,
    sharedCodeServer,
    sharedPythonEnvironment,
    tempDir,
  }) => {
    await setupSharedEnvironment(tempDir, sharedPythonEnvironment)
    writeEnvironmentConfig(tempDir)
    await runTest(page, sharedCodeServer, tempDir)
    await page.waitForSelector('text=Error creating context')
  })

  test('normal setup - set', async ({
    page,
    sharedCodeServer,
    sharedPythonEnvironment,
    tempDir,
  }) => {
    await setupSharedEnvironment(tempDir, sharedPythonEnvironment)
    writeEnvironmentConfig(tempDir)
    const env_file = path.join(tempDir, '.env')
    fs.writeFileSync(env_file, 'TEST_VAR=test_value')
    await runTest(page, sharedCodeServer, tempDir)
    await waitForLoadedSQLMesh(page)
  })
})

async function setupTcloudProject(
  tempDir: string,
  pythonDetails: PythonEnvironment,
) {
  // Install the mock tcloud package
  const mockTcloudPath = path.join(__dirname, 'tcloud')
  await pipInstall(pythonDetails, [mockTcloudPath])

  // Create a tcloud.yaml to mark this as a tcloud project
  const tcloudConfig = {
    url: 'https://mock.tobikodata.com',
    org: 'test-org',
    project: 'test-project',
  }
  await fs.writeFile(
    path.join(tempDir, 'tcloud.yaml'),
    `url: ${tcloudConfig.url}\norg: ${tcloudConfig.org}\nproject: ${tcloudConfig.project}\n`,
  )
  // Write mock ".tcloud_auth_state.json" file
  await setupAuthenticatedState(tempDir)
  // Set tcloud version to 2.10.1
  await setTcloudVersion(tempDir, '2.10.1')
}

test.describe('tcloud version', () => {
  test('normal setup - error ', async ({ page, sharedCodeServer, tempDir }) => {
    const { pythonDetails } = await setupEnvironment(tempDir)
    await setupTcloudProject(tempDir, pythonDetails)
    writeEnvironmentConfig(tempDir)
    await runTest(page, sharedCodeServer, tempDir)
    await page.waitForSelector('text=Error creating context')
  })

  test('normal setup - set', async ({ page, sharedCodeServer, tempDir }) => {
    const { pythonDetails } = await setupEnvironment(tempDir)
    await setupTcloudProject(tempDir, pythonDetails)
    writeEnvironmentConfig(tempDir)
    const env_file = path.join(tempDir, '.env')
    fs.writeFileSync(env_file, 'TEST_VAR=test_value')
    await runTest(page, sharedCodeServer, tempDir)
    await waitForLoadedSQLMesh(page)
  })
})
