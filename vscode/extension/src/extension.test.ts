// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ExtensionContext } from 'vscode'
import { err, ok } from '@bus/result'

const mocks = vi.hoisted(() => ({
  telemetryListener: undefined as ((enabled: boolean) => unknown) | undefined,
  start: vi.fn(),
  restart: vi.fn(),
  registerCommand: vi.fn<(command: string) => { dispose(): void }>(() => ({
    dispose: vi.fn(),
  })),
  registerContentProvider: vi.fn(() => ({ dispose: vi.fn() })),
  registerViewProvider: vi.fn(() => ({ dispose: vi.fn() })),
  setupTestController: vi.fn(() => ({ dispose: vi.fn() })),
  testController: { dispose: vi.fn() },
  handleError: vi.fn(),
}))

vi.mock('vscode', () => ({
  env: {
    onDidChangeTelemetryEnabled: (listener: (enabled: boolean) => unknown) => {
      mocks.telemetryListener = listener
      return { dispose: vi.fn() }
    },
  },
  authentication: {
    registerAuthenticationProvider: vi.fn(() => ({ dispose: vi.fn() })),
  },
  commands: { registerCommand: mocks.registerCommand },
  workspace: {
    registerTextDocumentContentProvider: mocks.registerContentProvider,
    onDidSaveTextDocument: vi.fn(() => ({ dispose: vi.fn() })),
  },
  window: { registerWebviewViewProvider: mocks.registerViewProvider },
}))

vi.mock('./lsp/lsp', () => ({
  LSPClient: class {
    start = mocks.start
    restart = mocks.restart
    hasCompletionCapability = () => true
  },
}))
vi.mock('./utilities/common/vscodeapi', () => ({
  createOutputChannel: vi.fn(() => ({ dispose: vi.fn() })),
  onDidChangeConfiguration: vi.fn(() => ({ dispose: vi.fn() })),
  registerCommand: mocks.registerCommand,
}))
vi.mock('./utilities/common/log', () => ({
  registerLogger: vi.fn(() => ({ dispose: vi.fn() })),
  traceInfo: vi.fn(),
  traceVerbose: vi.fn(),
  traceError: vi.fn(),
}))
vi.mock('./utilities/common/python', () => ({
  onDidChangePythonInterpreter: vi.fn(() => ({ dispose: vi.fn() })),
}))
vi.mock('./utilities/errors', () => ({ handleError: mocks.handleError }))
vi.mock('./auth/auth', () => ({
  AuthenticationProviderTobikoCloud: vi.fn(),
}))
vi.mock('./tests/tests', () => ({
  controller: mocks.testController,
  setupTestController: mocks.setupTestController,
}))
vi.mock('./providers/renderedModelProvider', () => ({
  RenderedModelProvider: Object.assign(vi.fn(), {
    getScheme: () => 'sqlmesh-rendered',
  }),
}))
vi.mock('./webviews/lineagePanel', () => ({ LineagePanel: vi.fn() }))
vi.mock('./completion/completion', () => ({
  selector: [],
  completionProvider: vi.fn(),
}))
vi.mock('./commands/format', () => ({ format: vi.fn() }))
vi.mock('./commands/signout', () => ({ signOut: vi.fn() }))
vi.mock('./commands/signin', () => ({ signIn: vi.fn() }))
vi.mock('./commands/signinSpecifyFlow', () => ({ signInSpecifyFlow: vi.fn() }))
vi.mock('./commands/renderModel', () => ({
  renderModel: vi.fn(),
  reRenderModelForSourceFile: vi.fn(),
}))
vi.mock('./commands/stop', () => ({ stop: vi.fn() }))
vi.mock('./commands/printEnvironment', () => ({ printEnvironment: vi.fn() }))
vi.mock('./commands/tableDiff', () => ({ showTableDiff: vi.fn() }))

describe('activation after a telemetry change during startup', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.clearAllMocks()
    mocks.telemetryListener = undefined
    mocks.restart.mockResolvedValue(ok(undefined))
    mocks.handleError.mockResolvedValue(undefined)
  })

  it.each([false, true])(
    'registers the full extension after a successful retry, initial start failed: %s',
    async initialStartFailed => {
      mocks.start.mockImplementationOnce(() => {
        mocks.telemetryListener?.(false)
        return Promise.resolve(
          initialStartFailed
            ? err({ type: 'sqlmesh_lsp_not_found' })
            : ok(undefined),
        )
      })
      const context = {
        subscriptions: [],
        extensionUri: {},
      } as unknown as ExtensionContext
      const { activate } = await import('./extension')

      await activate(context)

      expect(mocks.restart).toHaveBeenCalledOnce()
      expect(
        mocks.registerCommand.mock.calls.map(([command]) => command),
      ).toEqual([
        'sqlmesh.signin',
        'sqlmesh.signinSpecifyFlow',
        'sqlmesh.signout',
        'sqlmesh.renderModel',
        'sqlmesh.showTableDiff',
        'sqlmesh.format',
        'sqlmesh.restart',
        'sqlmesh.stop',
        'sqlmesh.printEnvironment',
      ])
      expect(mocks.registerContentProvider).toHaveBeenCalledOnce()
      expect(mocks.registerViewProvider).toHaveBeenCalledOnce()
      expect(
        context.subscriptions.filter(item => item === mocks.testController),
      ).toHaveLength(1)
    },
  )

  it('does not complete activation when both the initial start and retry fail', async () => {
    const failure = err({ type: 'sqlmesh_lsp_not_found' })
    mocks.start.mockImplementationOnce(() => {
      mocks.telemetryListener?.(false)
      return Promise.resolve(failure)
    })
    mocks.restart.mockResolvedValue(failure)
    const context = {
      subscriptions: [],
      extensionUri: {},
    } as unknown as ExtensionContext
    const { activate } = await import('./extension')

    await activate(context)

    expect(mocks.restart).toHaveBeenCalledOnce()
    expect(mocks.registerContentProvider).not.toHaveBeenCalled()
    expect(mocks.registerViewProvider).not.toHaveBeenCalled()
    expect(mocks.setupTestController).not.toHaveBeenCalled()
  })
})
