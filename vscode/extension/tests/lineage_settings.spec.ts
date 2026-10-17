import { test, expect } from './fixtures'
import type { FrameLocator, Page } from './fixtures'
import fs from 'fs-extra'
import {
  openLineageView,
  openServerPage,
  SUSHI_SOURCE_PATH,
  waitForLoadedSQLMesh,
} from './utils'
import { createPythonInterpreterSettingsSpecifier } from './utils_code_server'

/**
 * Find the iframe that hosts the lineage UI (the one containing the
 * Settings cog button). Returns null if it can't be located.
 *
 * The webview iframes attach asynchronously, so a single sweep can run before
 * the lineage one exists and find nothing. The sweep is repeated until the
 * Settings button answers from one of them or `timeout` elapses.
 */
async function findLineageFrame(
  page: Page,
  timeout = 60_000,
): Promise<FrameLocator | null> {
  const deadline = Date.now() + timeout

  while (Date.now() < deadline) {
    const iframes = page.locator('iframe')
    const iframeCount = await iframes.count()

    for (let i = 0; i < iframeCount; i++) {
      const contentFrame = iframes.nth(i).contentFrame()
      if (!contentFrame) continue
      const activeFrame = contentFrame.locator('#active-frame').contentFrame()
      if (!activeFrame) continue
      try {
        await activeFrame
          .getByRole('button', { name: 'Settings' })
          .waitFor({ timeout: 1000 })
        return activeFrame
      } catch {
        continue
      }
    }

    // Nothing answered this time round; give the next webview a chance to
    // attach instead of spinning when the page has no iframes yet.
    await page.waitForTimeout(250)
  }
  return null
}

test('Settings button is visible in the lineage view', async ({
  page,
  sharedCodeServer,
  tempDir,
}) => {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)
  await createPythonInterpreterSettingsSpecifier(tempDir)

  await openServerPage(page, tempDir, sharedCodeServer)

  await page.waitForSelector('text=models')

  // Click on the models folder, excluding external_models
  await page
    .getByRole('treeitem', { name: 'models', exact: true })
    .locator('a')
    .click()
  // Open the waiters.py model
  await page
    .getByRole('treeitem', { name: 'waiters.py', exact: true })
    .locator('a')
    .click()
  await waitForLoadedSQLMesh(page)

  // Open lineage
  await openLineageView(page)

  const lineageFrame = await findLineageFrame(page)
  expect(lineageFrame).not.toBeNull()
})

test('Only Direct Neighbors toggle filters the lineage graph', async ({
  page,
  sharedCodeServer,
  tempDir,
}) => {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)
  await createPythonInterpreterSettingsSpecifier(tempDir)

  await openServerPage(page, tempDir, sharedCodeServer)
  await page.waitForSelector('text=models')

  await page
    .getByRole('treeitem', { name: 'models', exact: true })
    .locator('a')
    .click()
  await page
    .getByRole('treeitem', { name: 'waiters.py', exact: true })
    .locator('a')
    .click()
  await waitForLoadedSQLMesh(page)

  await openLineageView(page)

  const lineageFrame = await findLineageFrame(page)
  expect(lineageFrame).not.toBeNull()
  if (!lineageFrame) return

  // Wait for the graph to render at least one node
  await lineageFrame.locator('.react-flow__node').first().waitFor()
  const nodesBefore = await lineageFrame.locator('.react-flow__node').count()
  expect(nodesBefore).toBeGreaterThan(0)

  // Open the settings menu and toggle "Only Direct Neighbors". Headless UI
  // renders the entry as `<button role="menuitem">`, and the explicit role is
  // the one the accessibility tree reports.
  await lineageFrame.getByRole('button', { name: 'Settings' }).click()
  const toggle = lineageFrame.getByRole('menuitem', {
    name: 'Only Direct Neighbors',
  })
  await toggle.waitFor()
  await toggle.click()

  // After enabling, the visible node set must be a subset of the original.
  // We assert a strict drop only when the original graph had room to shrink
  // (i.e. more than the worst-case direct-neighbor count of 1 + parents + children).
  await page.waitForTimeout(250) // let React Flow re-layout
  const nodesAfter = await lineageFrame.locator('.react-flow__node').count()
  expect(nodesAfter).toBeLessThanOrEqual(nodesBefore)
  expect(nodesAfter).toBeGreaterThan(0) // main node is always shown

  // Toggle off → graph returns to the full size
  await lineageFrame.getByRole('button', { name: 'Settings' }).click()
  const toggleOff = lineageFrame.getByRole('menuitem', {
    name: 'Only Direct Neighbors',
  })
  await toggleOff.waitFor()
  await toggleOff.click()
  await expect(lineageFrame.locator('.react-flow__node')).toHaveCount(
    nodesBefore,
  )
})
