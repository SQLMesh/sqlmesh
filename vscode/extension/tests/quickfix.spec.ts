import fs from 'fs-extra'
import path from 'path'
import {
  openProblemsView,
  saveFile,
  SUSHI_SOURCE_PATH,
  waitForLoadedSQLMesh,
} from './utils'
import { test, expect } from './fixtures'
import { createPythonInterpreterSettingsSpecifier } from './utils_code_server'

test('noselectstar quickfix', async ({ page, sharedCodeServer, tempDir }) => {
  await fs.copy(SUSHI_SOURCE_PATH, tempDir)
  await createPythonInterpreterSettingsSpecifier(tempDir)

  // Override the settings for the linter
  const configPath = path.join(tempDir, 'config.py')
  const read = await fs.readFile(configPath, 'utf8')
  // Replace linter to be on
  const target = 'enabled=True'
  const replaced = read.replace('enabled=False', 'enabled=True')
  // Assert replaced correctly
  expect(replaced).toContain(target)

  // Replace the rules to only have noselectstar
  const targetRules = `rules=[
            "noselectstar",
        ],`
  const replacedTheOtherRules = replaced.replace(
    `rules=[
            "ambiguousorinvalidcolumn",
            "invalidselectstarexpansion",
            "noselectstar",
            "nomissingaudits",
            "nomissingowner",
            "nomissingexternalmodels",
        ],`,
    targetRules,
  )
  expect(replacedTheOtherRules).toContain(targetRules)

  await fs.writeFile(configPath, replacedTheOtherRules)
  // Replace the file to cause the error
  const modelPath = path.join(tempDir, 'models', 'latest_order.sql')
  const readModel = await fs.readFile(modelPath, 'utf8')
  // Replace the specific select with the select star
  const modelReplaced = readModel.replace(
    'SELECT id, customer_id, start_ts, end_ts, event_date',
    'SELECT *',
  )
  await fs.writeFile(modelPath, modelReplaced)

  // Open the code server with the specified directory
  await page.goto(
    `http://127.0.0.1:${sharedCodeServer.codeServerPort}/?folder=${tempDir}`,
  )
  await page.waitForLoadState('networkidle')

  // Open the file with the linter issue
  await page
    .getByRole('treeitem', { name: 'models', exact: true })
    .locator('a')
    .click()
  await page
    .getByRole('treeitem', { name: 'latest_order.sql', exact: true })
    .locator('a')
    .click()

  await waitForLoadedSQLMesh(page)

  await openProblemsView(page)

  await page.getByRole('button', { name: 'Show fixes' }).click()
  // Wait for the quick fix menu to appear and click the specific action within it.
  const quickFixMenu = page.getByRole('menu')
  await quickFixMenu.waitFor({ state: 'visible' })
  const replaceSelectStar = quickFixMenu.getByRole('menuitem', {
    name: /Replace SELECT \* with/i,
  })
  await replaceSelectStar.first().waitFor({ state: 'visible' })
  await replaceSelectStar.first().click()
  // The code action edits the open document. It stays unsaved until the
  // editor writes it, so the on-disk assertion below would keep seeing
  // SELECT * on a busy CI runner.
  await quickFixMenu.waitFor({ state: 'hidden' })
  await saveFile(page)

  // Wait for the quick fix to be applied by polling the file content
  await expect
    .poll(
      async () => {
        const content = (await fs.readFile(modelPath)).toString('utf8')
        return content.includes('SELECT *')
      },
      { timeout: 15_000 },
    )
    .toBeFalsy()

  // Assert that the model no longer contains SELECT * but SELECT id, customer_id, waiter_id, start_ts, end_ts, event_date
  const readUpdatedFile = (await fs.readFile(modelPath)).toString('utf8')
  expect(readUpdatedFile).not.toContain('SELECT *')
  expect(readUpdatedFile).toContain(
    'SELECT id, customer_id, waiter_id, start_ts, end_ts, event_date',
  )
})
