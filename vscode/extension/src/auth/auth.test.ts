// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ok } from '@bus/result'

const mocks = vi.hoisted(() => ({
  execAsync: vi.fn(),
  getProjectRoot: vi.fn(),
  getTcloudBin: vi.fn(),
  showInformationMessage: vi.fn(),
}))

vi.mock('vscode', () => ({
  env: { openExternal: vi.fn() },
  Uri: { parse: vi.fn((value: string) => value) },
  EventEmitter: class {
    event = vi.fn()
    fire = vi.fn()
  },
  window: { showInformationMessage: mocks.showInformationMessage },
}))

vi.mock('../utilities/exec', () => ({ execAsync: mocks.execAsync }))
vi.mock('../utilities/common/utilities', () => ({
  getProjectRoot: mocks.getProjectRoot,
}))
vi.mock('../utilities/sqlmesh/sqlmesh', () => ({
  getTcloudBin: mocks.getTcloudBin,
}))
vi.mock('../utilities/common/log', () => ({ traceError: vi.fn() }))

import { AuthenticationProviderTobikoCloud } from './auth'

describe('AuthenticationProviderTobikoCloud telemetry environment', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getProjectRoot.mockResolvedValue({ uri: { fsPath: '/workspace' } })
    mocks.getTcloudBin.mockResolvedValue(
      ok({
        bin: '/venv/bin/tcloud',
        workspacePath: '/workspace',
        env: { SQLMESH__DISABLE_ANONYMIZED_ANALYTICS: 'true' },
        args: [],
      }),
    )
    mocks.execAsync
      .mockResolvedValueOnce({
        exitCode: 0,
        stdout: JSON.stringify({
          url: 'https://example.com/login',
          verifier_code: 'verifier',
        }),
        stderr: '',
      })
      .mockResolvedValueOnce({ exitCode: 0, stdout: '', stderr: '' })
    // Dismissing the prompt exits without waiting for the mocked login server.
    mocks.showInformationMessage.mockResolvedValue(undefined)
  })

  it('passes the telemetry-aware environment to every OAuth tcloud subprocess', async () => {
    const provider = new AuthenticationProviderTobikoCloud()

    await provider.sign_in_oauth_flow()

    const expectedOptions = expect.objectContaining({
      cwd: '/workspace',
      env: { SQLMESH__DISABLE_ANONYMIZED_ANALYTICS: 'true' },
    })
    expect(mocks.execAsync).toHaveBeenNthCalledWith(
      1,
      '/venv/bin/tcloud',
      ['auth', 'vscode', 'login-url'],
      expectedOptions,
    )
    expect(mocks.execAsync).toHaveBeenNthCalledWith(
      2,
      '/venv/bin/tcloud',
      ['auth', 'vscode', 'start-server', 'verifier'],
      expectedOptions,
    )
  })
})
