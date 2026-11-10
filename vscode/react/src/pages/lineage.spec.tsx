// SPDX-License-Identifier: Apache-2.0

import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PropsWithChildren } from 'react'
import { EventBusProvider } from '@/hooks/eventBus'
import { QueryClient } from '@tanstack/react-query'
import { LineagePage } from './lineage'

const { api, rpc, send, graph } = vi.hoisted(() => ({
  api: vi.fn(),
  rpc: vi.fn(),
  send: vi.fn(),
  graph: {} as { open?: (id: string) => void },
}))

vi.mock('@/api', () => ({ useApiModels: api }))
vi.mock('@/utils/rpc', () => ({ useRpc: () => rpc }))
vi.mock('@/hooks/vscode', () => ({ useVSCode: () => send }))
vi.mock('@/components/graph/context', () => ({
  default: ({
    children,
    handleClickModel,
  }: PropsWithChildren<{ handleClickModel: (id: string) => void }>) => {
    graph.open = handleClickModel
    return children
  },
}))
vi.mock('@/components/graph/ModelLineage', () => ({
  ModelLineage: ({ model }: { model: { displayName: string } }) => (
    <div data-testid="selected-model">{model.displayName}</div>
  ),
}))

const models = [
  { name: 'first', fqn: 'first', full_path: 'C:\\project\\first.sql' },
  { name: 'second', fqn: 'second', full_path: 'C:\\project\\second.sql' },
]

function page() {
  return (
    <EventBusProvider>
      <LineagePage />
    </EventBusProvider>
  )
}

function focus(path: string, isWindows?: boolean) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        data: {
          key: 'vscode_send',
          payload: { key: 'changeFocusOnFile', payload: { path, isWindows } },
        },
      }),
    )
  })
}

async function selected(name: string) {
  await waitFor(() => {
    expect(screen.getByTestId('selected-model').textContent).toBe(name)
  })
}

describe('lineage active-editor selection', () => {
  beforeEach(() => {
    api.mockReturnValue({ data: models, isLoading: false })
    rpc.mockResolvedValue({ fileUri: models[0].full_path, isWindows: true })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.restoreAllMocks()
  })

  it('selects the initial Windows editor despite drive case and separators', async () => {
    rpc.mockResolvedValue({ fileUri: 'c:/project/second.sql', isWindows: true })
    render(page())
    await selected('second')
  })

  it('follows a Windows editor switch despite directory and filename case', async () => {
    render(page())
    await selected('first')
    focus('c:/PROJECT/SECOND.SQL', true)
    await selected('second')
  })

  it.each([false, undefined])(
    'uses the host flag %s rather than browser platform',
    async isWindows => {
      vi.spyOn(window.navigator, 'platform', 'get').mockReturnValue('Win32')
      rpc.mockResolvedValue({ fileUri: models[0].full_path, isWindows })
      render(page())
      await selected('first')
      focus('c:/PROJECT/SECOND.SQL', isWindows)
      await selected('first')
      focus(models[1].full_path, isWindows)
      await selected('second')
    },
  )

  it('uses Windows host matching even with a Linux browser', async () => {
    vi.spyOn(window.navigator, 'platform', 'get').mockReturnValue(
      'Linux x86_64',
    )
    rpc.mockResolvedValue({ fileUri: 'c:/project/SECOND.SQL', isWindows: true })
    render(page())
    await selected('second')
  })

  it('keeps the selection for ambiguous fallback paths and prefers exact matches', async () => {
    const candidates = [
      models[0],
      { name: 'upper', fqn: 'upper', full_path: 'C:/A.sql' },
      { name: 'lower', fqn: 'lower', full_path: 'c:/a.sql' },
    ]
    api.mockReturnValue({ data: candidates, isLoading: false })
    render(page())
    await selected('first')
    focus('C:/a.SQL', true)
    await selected('first')
    focus('c:/a.sql', true)
    await selected('lower')
  })

  it('does not select an arbitrary model for an ambiguous initial path', async () => {
    api.mockReturnValue({
      data: [
        { name: 'upper', full_path: 'C:/A.sql' },
        { name: 'lower', full_path: 'c:/a.sql' },
      ],
      isLoading: false,
    })
    rpc.mockResolvedValue({ fileUri: 'C:/a.SQL', isWindows: true })
    render(page())
    await act(async () => {})
    expect(screen.queryByTestId('selected-model')).toBeNull()
  })

  it.each([undefined, 'C:/project/README.md'])(
    'falls back to the first model at initialization for %s',
    async fileUri => {
      rpc.mockResolvedValue({ fileUri, isWindows: true })
      render(page())
      await selected('first')
    },
  )

  it('keeps selection when focusing a non-model file', async () => {
    render(page())
    await selected('first')
    focus('C:/project/README.md', true)
    await selected('first')
  })

  it('handles an empty model list followed by delayed model loading', async () => {
    api.mockReturnValue({ data: [], isLoading: false })
    const view = render(page())
    expect(rpc).not.toHaveBeenCalled()
    expect(screen.queryByTestId('selected-model')).toBeNull()
    api.mockReturnValue({ data: models, isLoading: false })
    view.rerender(page())
    await selected('first')
  })

  it('does not let a late initial RPC overwrite the latest A-B-A focus', async () => {
    let resolve!: (value: { fileUri: string; isWindows: boolean }) => void
    rpc.mockReturnValue(
      new Promise(done => {
        resolve = done
      }),
    )
    render(page())
    focus('c:/project/SECOND.SQL', true)
    focus('c:/project/FIRST.SQL', true)
    await selected('first')
    await act(async () => {
      resolve({ fileUri: models[1].full_path, isWindows: true })
    })
    await selected('first')
  })

  it('ignores an old initial RPC after a newer model response', async () => {
    let resolve!: (value: { fileUri: string; isWindows: boolean }) => void
    rpc.mockReturnValueOnce(
      new Promise(done => {
        resolve = done
      }),
    )
    rpc.mockResolvedValue({ fileUri: models[1].full_path, isWindows: true })
    const view = render(page())
    api.mockReturnValue({ data: [...models], isLoading: false })
    view.rerender(page())
    await selected('second')
    await act(async () => {
      resolve({ fileUri: models[0].full_path, isWindows: true })
    })
    await selected('second')
  })

  it('does not revive an initial match after focus moves to a non-model file', async () => {
    let resolve!: (value: { fileUri: string; isWindows: boolean }) => void
    rpc.mockReturnValue(
      new Promise(done => {
        resolve = done
      }),
    )
    render(page())
    focus('C:/project/README.md', true)
    await selected('first')
    await act(async () => {
      resolve({ fileUri: models[1].full_path, isWindows: true })
    })
    await selected('first')
  })

  it('disposes an initial lookup on unmount', async () => {
    let resolve!: (value: { fileUri: string }) => void
    rpc.mockReturnValue(
      new Promise(done => {
        resolve = done
      }),
    )
    const view = render(page())
    view.unmount()
    await act(async () => {
      resolve({ fileUri: models[0].full_path })
    })
    expect(screen.queryByTestId('selected-model')).toBeNull()
  })

  it('preserves original paths for graph navigation and invalidates on save', async () => {
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries')
    render(page())
    await selected('first')
    focus('c:/PROJECT/SECOND.SQL', true)
    await selected('second')
    graph.open?.(encodeURIComponent(models[1].fqn))
    expect(send).toHaveBeenCalledWith('openFile', {
      filePath: models[1].full_path,
    })
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: {
            key: 'vscode_send',
            payload: {
              key: 'savedFile',
              payload: { fileUri: 'file:///c%3A/project/second.sql' },
            },
          },
        }),
      )
    })
    expect(invalidate).toHaveBeenCalledOnce()
  })
})
