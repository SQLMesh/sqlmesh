import { test as base } from '@playwright/test'
import path from 'path'
import fs from 'fs-extra'
import os from 'os'
import {
  startCodeServer,
  stopCodeServer,
  CodeServerContext,
} from './utils_code_server'

// Worker-scoped fixture to start/stop VS Code server once per worker
export const test = base.extend<
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  {},
  { sharedCodeServer: CodeServerContext; tempDir: string }
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
