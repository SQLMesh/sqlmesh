// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it, vi } from 'vitest'
import {
  requiresLspRestart,
  restartLspOnTelemetryChange,
} from './configurationChange'

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

describe('restartLspOnTelemetryChange', () => {
  const setup = () => {
    let listener: ((enabled: boolean) => void) | undefined
    const disposable = { dispose: vi.fn() }
    const onDidChangeTelemetryEnabled = vi.fn(
      (registeredListener: (enabled: boolean) => void) => {
        listener = registeredListener
        return disposable
      },
    )
    const restartLsp = vi.fn(() => Promise.resolve())

    const subscription = restartLspOnTelemetryChange(
      onDidChangeTelemetryEnabled,
      restartLsp,
    )

    return { disposable, listener: () => listener, restartLsp, subscription }
  }

  it('defers and collapses telemetry changes during initial LSP startup', async () => {
    const { listener, restartLsp, subscription } = setup()

    await subscription.runDuringInitialStart(() => {
      listener()?.(false)
      listener()?.(true)
      expect(restartLsp).not.toHaveBeenCalled()
      return Promise.resolve()
    })

    expect(restartLsp).toHaveBeenCalledOnce()
  })

  it('restarts for every telemetry preference change after initial startup', async () => {
    const { listener, restartLsp, subscription } = setup()
    await subscription.runDuringInitialStart(() => Promise.resolve())

    expect(restartLsp).not.toHaveBeenCalled()

    listener()?.(false)
    await vi.waitFor(() => expect(restartLsp).toHaveBeenCalledTimes(1))

    listener()?.(true)
    await vi.waitFor(() => expect(restartLsp).toHaveBeenCalledTimes(2))
  })

  it('releases deferred restarts when initial startup fails', async () => {
    const { listener, restartLsp, subscription } = setup()

    await expect(
      subscription.runDuringInitialStart(() => {
        listener()?.(false)
        return Promise.reject(new Error('initial start failed'))
      }),
    ).rejects.toThrow('initial start failed')

    expect(restartLsp).toHaveBeenCalledOnce()
    listener()?.(true)
    await vi.waitFor(() => expect(restartLsp).toHaveBeenCalledTimes(2))
  })

  it('disposes the underlying VS Code event listener', () => {
    const { disposable, subscription } = setup()

    subscription.dispose()

    expect(disposable.dispose).toHaveBeenCalledOnce()
  })
})
