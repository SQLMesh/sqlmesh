import { describe, expect, it } from 'vitest'
import { requiresLspRestart } from './configurationChange'

/**
 * Build a stand-in for `vscode.ConfigurationChangeEvent` from the settings that
 * changed. VS Code reports a section as affected when the changed key is the
 * section itself or sits underneath it.
 */
const changed = (...keys: string[]) => ({
  affectsConfiguration: (section: string) =>
    keys.some(key => key === section || key.startsWith(`${section}.`)),
})

describe('requiresLspRestart', () => {
  it('restarts when a sqlmesh setting changes', () => {
    expect(requiresLspRestart(changed('sqlmesh.projectPaths'))).toBe(true)
    expect(requiresLspRestart(changed('sqlmesh.lspEntrypoint'))).toBe(true)
  })

  it('restarts when the python interpreter changes', () => {
    expect(requiresLspRestart(changed('python.defaultInterpreterPath'))).toBe(
      true,
    )
  })

  // The LSP used to restart on every configuration change in the editor, so
  // anything that wrote a setting took the extension down with it. See #5920.
  it('ignores settings the language server does not read', () => {
    expect(requiresLspRestart(changed('editor.fontSize'))).toBe(false)
    expect(requiresLspRestart(changed('workbench.colorTheme'))).toBe(false)
    expect(requiresLspRestart(changed('files.autoSave'))).toBe(false)
  })

  // Running any python command in a VS Code terminal makes the Python
  // extension touch its own terminal settings, which is what made the
  // extension crash whenever sqlmesh was run in the terminal. See #5642.
  it('ignores python settings unrelated to the interpreter', () => {
    expect(
      requiresLspRestart(changed('python.terminal.activateEnvironment')),
    ).toBe(false)
    expect(
      requiresLspRestart(changed('python.analysis.typeCheckingMode')),
    ).toBe(false)
    expect(requiresLspRestart(changed('terminal.integrated.env.linux'))).toBe(
      false,
    )
  })

  it('does not restart when nothing relevant changed', () => {
    expect(requiresLspRestart(changed())).toBe(false)
  })

  it('restarts when a relevant setting changes alongside irrelevant ones', () => {
    expect(
      requiresLspRestart(changed('editor.fontSize', 'sqlmesh.projectPaths')),
    ).toBe(true)
  })
})
