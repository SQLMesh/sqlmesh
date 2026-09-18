import { test, expect } from './fixtures'
import fs from 'fs-extra'
import {
  openLineageView,
  openServerPage,
  runCommand,
  SUSHI_SOURCE_PATH,
  waitForLoadedSQLMesh,
} from './utils'
import { createPythonInterpreterSettingsSpecifier } from './utils_code_server'

test('Render works correctly', async ({ page, sharedCodeServer, tempDir }) => {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)
  await createPythonInterpreterSettingsSpecifier(tempDir)

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

  await page.waitForSelector('text=grain')
  await waitForLoadedSQLMesh(page)

  // Render the model
  await runCommand(page, 'Render Model')

  // Check if the model is rendered by check if "`oi`.`order_id` AS `order_id`," is in the window
  await expect(page.locator('text="marketing"."customer_id" AS')).toBeVisible()
  await expect(page.getByRole('tab', { name: 'sushi.customers (rendered)' })).toBeVisible()
})

test('Render works correctly with model without a description', async ({
  page,
  sharedCodeServer,
  tempDir,
}) => {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)

  await createPythonInterpreterSettingsSpecifier(tempDir)
  await openServerPage(page, tempDir, sharedCodeServer)

  //   Wait for the models folder to be visible
  await page.waitForSelector('text=models')

  // Click on the models folder, excluding external_models
  await page
    .getByRole('treeitem', { name: 'models', exact: true })
    .locator('a')
    .click()

  // Open the latest_order model
  await page
    .getByRole('treeitem', { name: 'latest_order.sql', exact: true })
    .locator('a')
    .click()

  await page.waitForSelector('text=custom_full_with_custom_kind')
  await waitForLoadedSQLMesh(page)

  // Render the model
  await runCommand(page, 'Render Model')

  // Check if the model is rendered correctly
  await expect(page.locator('text="orders"."id" AS "id",')).toBeVisible()
  await expect(page.getByRole('tab', { name: 'sushi.latest_order (rendered)' })).toBeVisible()
})

test('Render works correctly with every rendered model opening a new tab', async ({
  page,
  sharedCodeServer,
  tempDir,
}) => {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)

  await createPythonInterpreterSettingsSpecifier(tempDir)
  await openServerPage(page, tempDir, sharedCodeServer)

  // Wait for the models folder to be visible
  await page.waitForSelector('text=models')
  await page
    .getByRole('treeitem', { name: 'models', exact: true })
    .locator('a')
    .click()
  await page
    .getByRole('treeitem', { name: 'latest_order.sql', exact: true })
    .locator('a')
    .click()
  await page.waitForSelector('text=custom_full_with_custom_kind')
  await waitForLoadedSQLMesh(page)

  // Render the model
  await runCommand(page, 'Render Model')

  // Check if the model is rendered correctly
  await expect(page.getByRole('tab', { name: 'sushi.latest_order (rendered)' })).toBeVisible()

  // Open the customers model
  await page
    .getByRole('treeitem', { name: 'customers.sql', exact: true })
    .locator('a')
    .click()
  await page.waitForSelector('text=grain')

  // Render the customers model
  await runCommand(page, 'Render Model')

  // Assert both tabs exist
  await expect(page.getByRole('tab', { name: 'sushi.latest_order (rendered)' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'sushi.customers (rendered)' })).toBeVisible()
})

test('Render shows model picker when no active editor is open', async ({
  page,
  sharedCodeServer,
  tempDir,
}) => {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)
  await createPythonInterpreterSettingsSpecifier(tempDir)

  // Navigate to code-server instance
  await openServerPage(page, tempDir, sharedCodeServer)

  // Load the lineage view to initialize SQLMesh context (like lineage.spec.ts does)
  await openLineageView(page)

  await waitForLoadedSQLMesh(page)

  // Run the render command without any active editor
  await runCommand(page, 'Render Model')

  // Type to filter for customers model and select it
  await page.keyboard.type('customers')
  await page.waitForSelector('text=sushi.customers', { timeout: 2_000 })
  await page.locator('text=sushi.customers').click()

  // Verify the rendered model is shown
  await expect(page.getByRole('tab', { name: 'sushi.customers (rendered)' })).toBeVisible({
    timeout: 2_000,
  })
})
