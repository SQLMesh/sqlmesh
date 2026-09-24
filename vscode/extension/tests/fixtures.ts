import { test as base } from '@playwright/test'
import path from 'path'
import fs from 'fs-extra'
import os from 'os'
import {
  startCodeServer,
  stopCodeServer,
  CodeServerContext,
} from './utils_code_server'
import {
  createVirtualEnvironment,
  pipInstall,
  PythonEnvironment,
  REPO_ROOT,
  warmUpVirtualEnvironment,
} from './utils'

/**
 * A virtual environment with sqlmesh installed, shared by every test in a
 * worker that does not need an environment of its own.
 */
export interface SharedPythonEnvironment extends PythonEnvironment {
  venvDir: string
}

// Creating the environment installs sqlmesh from source, which is far slower
// than a test. Give it a budget of its own so it is not charged to the first
// test that happens to ask for it.
const SHARED_PYTHON_ENVIRONMENT_TIMEOUT_MS = 300_000

// Worker-scoped fixture to start/stop VS Code server once per worker
export const test = base.extend<
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  {},
  {
    sharedCodeServer: CodeServerContext
    sharedPythonEnvironment: SharedPythonEnvironment
    tempDir: string
  }
>({
  sharedCodeServer: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      // Create a temporary directory for the shared server
      const tempDir = await fs.mkdtemp(
        path.join(os.tmpdir(), 'vscode-test-shared-server-'),
      )

      // Start the code server once per worker
      const context = await startCodeServer({
        tempDir,
      })

      console.log(
        `Started shared VS Code server for worker ${test.info().workerIndex} on port ${context.codeServerPort}`,
      )

      // Provide the context to all tests in this worker
      await use(context)

      // Clean up after all tests in this worker are done
      console.log(`Stopping shared VS Code server`)
      await stopCodeServer(context)
    },
    { scope: 'worker', auto: true },
  ],
  sharedPythonEnvironment: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      // The environment lives outside the per-test temporary directory, which
      // is removed after every test.
      const envDir = await fs.mkdtemp(
        path.join(os.tmpdir(), 'vscode-test-shared-python-env-'),
      )
      const venvDir = path.join(envDir, '.venv')
      const pythonDetails = await createVirtualEnvironment(venvDir)
      await pipInstall(pythonDetails, [
        `${REPO_ROOT}[lsp,bigquery]`,
        path.join(REPO_ROOT, 'examples', 'custom_materializations'),
      ])
      await warmUpVirtualEnvironment(pythonDetails)

      console.log(
        `Created shared Python environment for worker ${test.info().workerIndex} at ${venvDir}`,
      )

      await use({ ...pythonDetails, venvDir })

      console.log(`Removing shared Python environment: ${envDir}`)
      await fs.remove(envDir)
    },
    { scope: 'worker', timeout: SHARED_PYTHON_ENVIRONMENT_TIMEOUT_MS },
  ],
  tempDir: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      // Create a temporary directory for each test
      const tempDir = await fs.mkdtemp(
        path.join(os.tmpdir(), 'vscode-test-temp-'),
      )
      console.log(`Created temporary directory: ${tempDir}`)
      await use(tempDir)

      // Clean up after each test
      console.log(`Cleaning up temporary directory: ${tempDir}`)
      await fs.remove(tempDir)
    },
    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-expect-error
    { auto: true },
  ],
})

// Export expect and commonly used Playwright types for convenience
export { expect, FrameLocator, Page } from '@playwright/test'
