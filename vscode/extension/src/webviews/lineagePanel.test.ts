// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TextEditor, Uri, WebviewView } from 'vscode'
import type { LSPClient } from '../lsp/lsp'
import { LineagePanel } from './lineagePanel'

const state = vi.hoisted(() => ({
  isWindows: true,
  editor: undefined as TextEditor | undefined,
  focus: undefined as ((editor: TextEditor | undefined) => void) | undefined,
  saved: undefined as
    | ((document: { uri: { toString: () => string } }) => void)
    | undefined,
  receive: undefined as ((message: unknown) => Promise<void>) | undefined,
  post: vi.fn(),
  dispose: vi.fn(),
}))

vi.mock('vscode', () => ({
  Uri: { joinPath: () => 'asset' },
  window: {
    get activeTextEditor() {
      return state.editor
    },
    onDidChangeActiveTextEditor: (callback: typeof state.focus) => {
      state.focus = callback
      return { dispose: state.dispose }
    },
  },
  workspace: {
    onDidSaveTextDocument: (callback: typeof state.saved) => {
      state.saved = callback
      return { dispose: state.dispose }
    },
  },
}))
vi.mock('@bus/result', () => ({ isErr: () => false }))
vi.mock('../utilities/isWindows', () => ({
  get IS_WINDOWS() {
    return state.isWindows
  },
}))

function editor(fsPath: string): TextEditor {
  return { document: { uri: { fsPath } } } as TextEditor
}

function panel() {
  const instance = new LineagePanel({} as Uri, {} as LSPClient)
  instance.resolveWebviewView({
    webview: {
      postMessage: state.post,
      asWebviewUri: (uri: unknown) => uri,
      onDidReceiveMessage: (callback: typeof state.receive) => {
        state.receive = callback
        return { dispose: state.dispose }
      },
    },
  } as unknown as WebviewView)
  return instance
}

describe('lineage editor path messages', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    state.editor = undefined
  })

  it.each([true, false])(
    'sends extension-host platform %s with active-file RPC and focus events',
    async isWindows => {
      state.isWindows = isWindows
      state.editor = editor('C:\\Project\\A %20#.sql')
      const instance = panel()
      await state.receive?.({
        key: 'rpcRequest',
        payload: { method: 'get_active_file', requestId: 'active', params: {} },
      })
      expect(state.post).toHaveBeenLastCalledWith({
        key: 'rpcResponse',
        payload: {
          requestId: 'active',
          result: { fileUri: 'C:\\Project\\A %20#.sql', isWindows },
        },
      })
      state.focus?.(editor('C:\\Project\\B.sql'))
      expect(state.post).toHaveBeenLastCalledWith({
        key: 'vscode_send',
        payload: {
          key: 'changeFocusOnFile',
          payload: { path: 'C:\\Project\\B.sql', isWindows },
        },
      })
      instance.dispose()
      expect(state.dispose).toHaveBeenCalledTimes(3)
    },
  )

  it('reports no editor and preserves saved-file URI payloads', async () => {
    state.isWindows = true
    const instance = panel()
    await state.receive?.({
      key: 'rpcRequest',
      payload: { method: 'get_active_file', requestId: 'none', params: {} },
    })
    expect(state.post).toHaveBeenLastCalledWith({
      key: 'rpcResponse',
      payload: {
        requestId: 'none',
        result: { fileUri: undefined, isWindows: true },
      },
    })
    state.focus?.(undefined)
    expect(state.post).toHaveBeenCalledOnce()
    state.saved?.({
      uri: { toString: () => 'file:///c%3A/Project/A%20%23.sql' },
    })
    expect(state.post).toHaveBeenLastCalledWith({
      key: 'vscode_send',
      payload: {
        key: 'savedFile',
        payload: { fileUri: 'file:///c%3A/Project/A%20%23.sql' },
      },
    })
    instance.dispose()
  })
})
