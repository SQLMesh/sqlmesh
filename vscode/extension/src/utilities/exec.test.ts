// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { execAsync } from './exec'

const { traceInfoMock } = vi.hoisted(() => ({ traceInfoMock: vi.fn() }))

vi.mock('./common/log', () => ({ traceInfo: traceInfoMock }))

/** Create a temporary directory for a test executable. */
function makeTmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'exec-test-'))
}

describe('execAsync', () => {
  const tmpdirs: string[] = []

  afterEach(() => {
    traceInfoMock.mockClear()
    for (const dir of tmpdirs.splice(0)) {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('executes an executable whose path contains a space', async () => {
    // Create a temp directory whose name contains a space.
    const baseDir = makeTmpDir()
    tmpdirs.push(baseDir)
    const spacedDir = path.join(baseDir, 'dir with spaces')
    fs.mkdirSync(spacedDir)

    // Copy the current Node.js binary into the spaced directory.
    const isWindows = process.platform === 'win32'
    const ext = isWindows ? '.exe' : ''
    const destExe = path.join(spacedDir, `node${ext}`)
    fs.copyFileSync(process.execPath, destExe)

    // Ensure the copied binary is executable on POSIX.
    if (!isWindows) {
      fs.chmodSync(destExe, 0o755)
    }

    // The path must contain a space for this test to be meaningful.
    expect(destExe).toContain(' ')

    const result = await execAsync(destExe, ['--version'])

    expect(result.exitCode).toBe(0)
    expect(result.stderr).toBe('')
    expect(result.stdout.trim()).toBe(process.version)
  })

  it('passes arguments with spaces and shell metacharacters literally', async () => {
    const argument = 'value with spaces; $(not-a-command) & more'
    const result = await execAsync(process.execPath, [
      '-e',
      'process.stdout.write(process.argv[1])',
      argument,
    ])

    expect(result.exitCode).toBe(0)
    expect(result.stdout).toBe(argument)
    expect(result.stderr).toBe('')
  })

  it('logs an unambiguous argv representation', async () => {
    const argument = 'value with spaces\nand "quotes"'

    await execAsync(process.execPath, ['-e', '', argument])

    expect(traceInfoMock).toHaveBeenCalledWith(
      `Executing command: ${JSON.stringify([
        process.execPath,
        '-e',
        '',
        argument,
      ])} in undefined`,
    )
  })

  it('resolves with a nonzero ExecResult instead of rejecting', async () => {
    // Run a Node.js one-liner that writes to stdout/stderr and exits nonzero.
    const result = await execAsync(process.execPath, [
      '-e',
      "process.stdout.write('out'); process.stderr.write('err'); process.exit(42)",
    ])

    expect(result.exitCode).toBe(42)
    expect(result.stdout).toBe('out')
    expect(result.stderr).toBe('err')
  })

  it('preserves empty stderr for a nonzero exit with no child output', async () => {
    const result = await execAsync(process.execPath, ['-e', 'process.exit(7)'])

    expect(result.exitCode).toBe(7)
    expect(result.stdout).toBe('')
    expect(result.stderr).toBe('')
  })

  it('resolves spawn failures with a useful nonzero ExecResult', async () => {
    const missingExecutable = path.join(
      os.tmpdir(),
      `missing-executable-${process.pid}-${Date.now()}`,
    )
    expect(fs.existsSync(missingExecutable)).toBe(false)

    const result = await execAsync(missingExecutable)

    expect(result.exitCode).not.toBe(0)
    expect(result.stdout).toBe('')
    expect(result.stderr).toContain(path.basename(missingExecutable))
  })

  it('rejects with AbortError when the signal is aborted', async () => {
    const controller = new AbortController()

    // Start a long-running child process.
    const promise = execAsync(
      process.execPath,
      ['-e', 'setTimeout(() => {}, 60_000)'],
      { signal: controller.signal },
    )

    // Abort immediately.
    controller.abort()

    await expect(promise).rejects.toMatchObject({ name: 'AbortError' })
  })
})
