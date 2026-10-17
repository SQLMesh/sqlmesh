// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Mutable telemetry state — flip per test.
let _isTelemetryEnabled = true

vi.mock('vscode', () => ({
  default: {},
  env: {
    get isTelemetryEnabled(): boolean {
      return _isTelemetryEnabled
    },
  },
  ProgressLocation: { Notification: 15 },
  window: {
    withProgress: vi.fn(),
    showInformationMessage: vi.fn(),
  },
}))

vi.mock('../common/python', () => ({
  getInterpreterDetails: vi.fn(),
  getPythonEnvVariables: vi.fn(),
}))

vi.mock('../common/log', () => ({
  traceInfo: vi.fn(),
  traceLog: vi.fn(),
  traceVerbose: vi.fn(),
  traceError: vi.fn(),
  traceWarn: vi.fn(),
}))

vi.mock('../common/utilities', () => ({
  getProjectRoot: vi.fn(),
}))

vi.mock('../python', () => ({
  isPythonModuleInstalled: vi.fn(),
}))

vi.mock('../../auth/auth', () => ({
  isSignedIntoTobikoCloud: vi.fn(),
}))

vi.mock('../exec', () => ({
  execAsync: vi.fn(),
}))

vi.mock('../config', () => ({
  getSqlmeshLspEntryPoint: vi.fn().mockReturnValue(undefined),
  resolveProjectPath: vi.fn(),
}))

vi.mock('../isWindows', () => ({
  IS_WINDOWS: false,
}))

import { getSqlmeshEnvironment, sqlmeshLspExec } from './sqlmesh'
import { getInterpreterDetails, getPythonEnvVariables } from '../common/python'
import { getProjectRoot } from '../common/utilities'
import { getSqlmeshLspEntryPoint, resolveProjectPath } from '../config'
import { ok } from '@bus/result'

const ANALYTICS_KEY = 'SQLMESH__DISABLE_ANONYMIZED_ANALYTICS'
let originalAnalyticsValue: string | undefined
let originallyHadAnalyticsKey = false

beforeEach(() => {
  originallyHadAnalyticsKey = Object.prototype.hasOwnProperty.call(
    process.env,
    ANALYTICS_KEY,
  )
  originalAnalyticsValue = process.env[ANALYTICS_KEY]
  Reflect.deleteProperty(process.env, ANALYTICS_KEY)
  _isTelemetryEnabled = true
})

afterEach(() => {
  if (originallyHadAnalyticsKey) {
    process.env[ANALYTICS_KEY] = originalAnalyticsValue
  } else {
    Reflect.deleteProperty(process.env, ANALYTICS_KEY)
  }
  vi.clearAllMocks()
})

describe('getSqlmeshEnvironment telemetry', () => {
  const baseInterpreterDetails = {
    path: ['/usr/bin/python3'],
    binPath: undefined as string | undefined,
    isVirtualEnvironment: false,
    resource: undefined,
  }

  beforeEach(() => {
    vi.mocked(getInterpreterDetails).mockResolvedValue(baseInterpreterDetails)
    vi.mocked(getPythonEnvVariables).mockResolvedValue(ok({}))
  })

  it('sets SQLMESH__DISABLE_ANONYMIZED_ANALYTICS=true when VS Code telemetry is disabled', async () => {
    _isTelemetryEnabled = false

    const result = await getSqlmeshEnvironment()

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.value[ANALYTICS_KEY]).toBe('true')
    }
    expect(process.env[ANALYTICS_KEY]).toBeUndefined()
  })

  it('overrides a conflicting false value when VS Code telemetry is disabled', async () => {
    _isTelemetryEnabled = false
    // Simulate an inherited env that tries to re-enable analytics.
    vi.mocked(getPythonEnvVariables).mockResolvedValue(
      ok({ [ANALYTICS_KEY]: 'false' }),
    )

    const result = await getSqlmeshEnvironment()

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.value[ANALYTICS_KEY]).toBe('true')
    }
  })

  it('does not add SQLMESH__DISABLE_ANONYMIZED_ANALYTICS when VS Code telemetry is enabled', async () => {
    _isTelemetryEnabled = true

    const result = await getSqlmeshEnvironment()

    expect(result.ok).toBe(true)
    if (result.ok) {
      // The variable must be absent, not just falsy — any presence would
      // disable analytics even when the user opted in.
      expect(Object.prototype.hasOwnProperty.call(result.value, ANALYTICS_KEY)).toBe(false)
    }
  })

  it('preserves a user-provided value unchanged when VS Code telemetry is enabled', async () => {
    _isTelemetryEnabled = true
    vi.mocked(getPythonEnvVariables).mockResolvedValue(
      ok({ [ANALYTICS_KEY]: 'true' }),
    )

    const result = await getSqlmeshEnvironment()

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.value[ANALYTICS_KEY]).toBe('true')
    }
  })
})

describe('sqlmeshLspExec telemetry', () => {
  beforeEach(() => {
    vi.mocked(getProjectRoot).mockResolvedValue({} as never)
    vi.mocked(resolveProjectPath).mockReturnValue(
      ok({
        workspaceFolder: '/workspace',
        projectPaths: undefined,
      }),
    )
    vi.mocked(getSqlmeshLspEntryPoint).mockReturnValue({
      entrypoint: '/custom/sqlmesh_lsp',
      args: ['--debug'],
    })
  })

  it('disables analytics for a configured LSP entry point when VS Code telemetry is disabled', async () => {
    _isTelemetryEnabled = false

    const result = await sqlmeshLspExec()

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.value.env[ANALYTICS_KEY]).toBe('true')
    }
    expect(process.env[ANALYTICS_KEY]).toBeUndefined()
  })

  it('does not add an analytics override for a configured LSP entry point when VS Code telemetry is enabled', async () => {
    _isTelemetryEnabled = true

    const result = await sqlmeshLspExec()

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(Object.prototype.hasOwnProperty.call(result.value.env, ANALYTICS_KEY)).toBe(false)
    }
  })
})
