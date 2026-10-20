import path from 'path'
import { Page } from '@playwright/test'
import { execAsync } from '../src/utilities/exec'
import { CodeServerContext } from './utils_code_server'

// Where your extension lives on disk
export const EXT_PATH = path.resolve(__dirname, '..')
// Where the sushi project lives which we copy from
export const SUSHI_SOURCE_PATH = path.join(
  __dirname,
  '..',
  '..',
  '..',
  'examples',
  'sushi',
)
export const MULTI_SOURCE_PATH = path.join(
  __dirname,
  '..',
  '..',
  '..',
  'examples',
  'multi',
)
export const REPO_ROOT = path.join(__dirname, '..', '..', '..')

/**
 * Click on the Explorer tab in the VS Code activity bar if the Explorer tab is not already active.
 * This is necessary because the Explorer tab may not be visible if the user has not opened it yet.
 */
export const clickExplorerTab = async (page: Page): Promise<void> => {
  const isExplorerActive = await page.locator("text='Explorer'").isVisible()
  if (!isExplorerActive) {
    // Wait for the activity bar to be loaded
    await page.waitForSelector('.actions-container[role="tablist"]')

    // Click on the Explorer tab using the codicon class
    await page.click('.codicon-explorer-view-icon')

    // Wait a bit for the explorer view to activate
    await page.locator("text='Explorer'").waitFor({ state: 'visible' })
  }
}

export interface PythonEnvironment {
  pythonPath: string
  pipPath: string
}

/**
 * Create a virtual environment in the given directory using uv.
 * @param venvDir The directory to create the virtual environment in.
 */
export const createVirtualEnvironment = async (
  venvDir: string,
): Promise<PythonEnvironment> => {
  // Try to use uv first, fallback to python -m venv
  const { exitCode, stderr } = await execAsync(`uv venv "${venvDir}"`)
  if (exitCode !== 0) {
    throw new Error(`Failed to create venv with uv: ${stderr}`)
  }

  // Get paths
  const isWindows = process.platform === 'win32'
  const binDir = path.join(venvDir, isWindows ? 'Scripts' : 'bin')
  const pythonPath = path.join(binDir, isWindows ? 'python.exe' : 'python')
  const pipPath = path.join(binDir, isWindows ? 'pip.exe' : 'pip')

  return {
    pythonPath,
    pipPath,
  }
}

/**
 * Install packages in the given virtual environment using uv.
 * @param pythonDetails The Python environment to use.
 * @param packagePaths The paths to the packages to install (string[]).
 */
export const pipInstall = async (
  pythonDetails: PythonEnvironment,
  packagePaths: string[],
): Promise<void> => {
  const packages = packagePaths.map(pkg => `-e "${pkg}"`).join(' ')
  const execString = `uv pip install --python "${pythonDetails.pythonPath}" ${packages}`
  const { stderr, exitCode } = await execAsync(execString)
  if (exitCode !== 0) {
    throw new Error(`Failed to install package with uv: ${stderr}`)
  }
}

/**
 * Import the language server entrypoint once so that Python writes its
 * bytecode cache.
 *
 * The first import of sqlmesh in a fresh environment is several times slower
 * than every import after it, because the whole dependency tree has to be
 * compiled. Paying that here rather than inside a test keeps it out of the
 * per-test timeout.
 */
export const warmUpVirtualEnvironment = async (
  pythonDetails: PythonEnvironment,
): Promise<void> => {
  const { stderr, exitCode } = await execAsync(
    `"${pythonDetails.pythonPath}" -c "import sqlmesh.lsp.main"`,
  )
  if (exitCode !== 0) {
    throw new Error(`Failed to warm up the virtual environment: ${stderr}`)
  }
}

/**
 * Open the lineage view in the given window.
 */
export const openLineageView = async (page: Page) =>
  await runCommand(page, 'Lineage: Focus On View')

/**
 * Open the problems/diagnostics view in the given window.
 */
export const openProblemsView = async (page: Page) =>
  await runCommand(page, 'View: Focus Problems')

/**
 * Restart the SQLMesh servers
 */
export const restartSqlmeshServers = async (page: Page) =>
  runCommand(page, 'SQLMesh: Restart Servers')

/**
 * Open the vscode command palette and run the given command.
 * @param page The window to run the command in.
 * @param command The command to run.
 */
export const runCommand = async (
  page: Page,
  command: string,
): Promise<void> => {
  const maxRetries = 3
  const retryDelay = 3000

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      await page.keyboard.press(
        process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P',
      )
      await page.waitForSelector(
        'input[aria-label="Type the name of a command to run."]',
        { timeout: 5000 },
      )
      await page.keyboard.type(command)
      const commandElement = await page.waitForSelector(
        `a:has-text("${command}")`,
        { timeout: 5000 },
      )
      await commandElement.click()
      return // Success, exit the retry loop
    } catch (error) {
      if (attempt === maxRetries - 1) {
        throw error // Last attempt failed, throw the error
      }

      // Close any open command palette before retrying
      await page.keyboard.press('Escape')
      await page.waitForTimeout(retryDelay)
    }
  }
}

/**
 * Go to definition. Assumes the location is clicked.
 */
export const goToDefinition = async (page: Page) =>
  runCommand(page, 'Go to Definition')

/**
 * Save file
 */
export const saveFile = async (page: Page) => runCommand(page, 'File: Save')

/**
 * Rename Symbol opens the rename symbol dialog in VS Code.
 */
export const renameSymbol = async (page: Page) =>
  runCommand(page, 'Rename Symbol')

/**
 * Find all references to the symbol under the cursor.
 */
export const findAllReferences = async (page: Page): Promise<void> =>
  runCommand(page, 'References: Find All References')

/**
 * Go to references. Assumes the location is clicked.
 */
export const goToReferences = async (page: Page): Promise<void> =>
  runCommand(page, 'Go to References')

/**
 * Open the vscode code file picker and select the given file.
 */
export const openFile = async (page: Page, file: string): Promise<void> => {
  const maxRetries = 3
  const retryDelay = 3000

  const fileName = path.basename(file)

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      await page.keyboard.press(
        process.platform === 'darwin' ? 'Meta+P' : 'Control+P',
      )
      await page
        .getByRole('textbox', { name: 'Search files by name' })
        .waitFor({ state: 'visible', timeout: 5000 })
      await page.keyboard.type(file)
      const commandElement = await page.waitForSelector(
        `a:has-text("${fileName}")`,
        { timeout: 5000 },
      )
      await commandElement.click()
      return // Success, exit the retry loop
    } catch (error) {
      if (attempt === maxRetries - 1) {
        throw error // Last attempt failed, throw the error
      }

      // Close any open command palette before retrying
      await page.keyboard.press('Escape')
      await page.waitForTimeout(retryDelay)
    }
  }
}

/**
 * Wait for SQLMesh context to be loaded.
 */
export const waitForLoadedSQLMesh = (page: Page) =>
  page.waitForSelector('text=Loaded SQLMesh Context')

/**
 * Go to VSCode page
 */
export const openServerPage = async (
  page: Page,
  targetPath: string,
  context: CodeServerContext,
) => {
  const isWorkspace = targetPath.endsWith('.code-workspace')
  const param = isWorkspace ? 'workspace' : 'folder'
  await page.goto(
    `http://127.0.0.1:${context.codeServerPort}/?${param}=${targetPath}`,
  )
  await page.waitForLoadState('networkidle')
  await page.waitForSelector('[role="application"]', { timeout: 10000 })
}
