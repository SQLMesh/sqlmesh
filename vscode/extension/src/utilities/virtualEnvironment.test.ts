// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from 'vitest'
import { warmUpVirtualEnvironment } from '../../tests/utils'
import { execAsync } from './exec'

vi.mock('./exec', () => ({ execAsync: vi.fn() }))

describe('warmUpVirtualEnvironment', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('passes the interpreter and import arguments separately', async () => {
    vi.mocked(execAsync).mockResolvedValue({
      exitCode: 0,
      stdout: '',
      stderr: '',
    })
    const pythonPath = '/project with spaces/.venv/bin/python'

    await warmUpVirtualEnvironment({ pythonPath, pipPath: '' })

    expect(execAsync).toHaveBeenCalledExactlyOnceWith(pythonPath, [
      '-c',
      'import sqlmesh.lsp.main',
    ])
  })

  it('reports a failed interpreter import', async () => {
    vi.mocked(execAsync).mockResolvedValue({
      exitCode: 1,
      stdout: '',
      stderr: 'No module named sqlmesh',
    })

    await expect(
      warmUpVirtualEnvironment({ pythonPath: '/venv/bin/python', pipPath: '' }),
    ).rejects.toThrow(
      'Failed to warm up the virtual environment: No module named sqlmesh',
    )
  })
})
