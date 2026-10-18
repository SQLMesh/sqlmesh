// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest'
import { detectShellFamily, printEnvironmentCommand } from './shellCommand'

const POWERSHELL_COMMAND =
  "Get-ChildItem Env: | Sort-Object Name | ForEach-Object { '{0}={1}' -f $_.Name, $_.Value }"

describe('detectShellFamily', () => {
  it('should detect fish', () => {
    expect(detectShellFamily('/usr/bin/fish', false)).toBe('fish')
    expect(detectShellFamily('/opt/homebrew/bin/fish', false)).toBe('fish')
  })

  it('should detect POSIX shells', () => {
    expect(detectShellFamily('/bin/bash', false)).toBe('posix')
    expect(detectShellFamily('/bin/zsh', false)).toBe('posix')
    expect(detectShellFamily('/bin/sh', false)).toBe('posix')
    expect(detectShellFamily('/usr/bin/dash', false)).toBe('posix')
    expect(detectShellFamily('/usr/bin/ksh', false)).toBe('posix')
  })

  it('should detect Windows shells, ignoring the executable extension', () => {
    expect(detectShellFamily('C:\\Windows\\System32\\cmd.exe', true)).toBe(
      'cmd',
    )
    expect(
      detectShellFamily(
        'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
        true,
      ),
    ).toBe('powershell')
    expect(
      detectShellFamily('C:\\Program Files\\PowerShell\\7\\pwsh.exe', true),
    ).toBe('powershell')
  })

  it('should detect a POSIX shell installed on Windows', () => {
    expect(
      detectShellFamily('C:\\Program Files\\Git\\bin\\bash.exe', true),
    ).toBe('posix')
  })

  it('should accept a bare shell name without a path', () => {
    expect(detectShellFamily('fish', false)).toBe('fish')
    expect(detectShellFamily('pwsh', false)).toBe('powershell')
  })

  it('should ignore the case of the shell name', () => {
    expect(detectShellFamily('/usr/bin/FISH', false)).toBe('fish')
    expect(detectShellFamily('C:\\WINDOWS\\SYSTEM32\\CMD.EXE', true)).toBe(
      'cmd',
    )
  })

  it('should fall back on the platform for an unknown shell', () => {
    expect(detectShellFamily('/usr/bin/elvish', false)).toBe('posix')
    expect(detectShellFamily('C:\\tools\\elvish.exe', true)).toBe('cmd')
  })

  it('should fall back on the platform when no shell is reported', () => {
    expect(detectShellFamily('', false)).toBe('posix')
    expect(detectShellFamily('', true)).toBe('cmd')
    expect(detectShellFamily(undefined, false)).toBe('posix')
    expect(detectShellFamily(undefined, true)).toBe('cmd')
  })
})

describe('printEnvironmentCommand', () => {
  it('should use the exported-only set in fish', () => {
    expect(printEnvironmentCommand('/usr/bin/fish', false)).toBe('set -x')
  })

  it('should use env | sort in POSIX shells', () => {
    expect(printEnvironmentCommand('/bin/bash', false)).toBe('env | sort')
    expect(printEnvironmentCommand('/bin/zsh', false)).toBe('env | sort')
    expect(printEnvironmentCommand('/bin/sh', false)).toBe('env | sort')
  })

  it('should use set in cmd', () => {
    expect(
      printEnvironmentCommand('C:\\Windows\\System32\\cmd.exe', true),
    ).toBe('set')
  })

  it('should not use set in PowerShell, where it prompts for a variable name', () => {
    expect(
      printEnvironmentCommand(
        'C:\\Program Files\\PowerShell\\7\\pwsh.exe',
        true,
      ),
    ).toBe(POWERSHELL_COMMAND)
  })

  it('should use the PowerShell command for pwsh off Windows', () => {
    expect(printEnvironmentCommand('/usr/local/bin/pwsh', false)).toBe(
      POWERSHELL_COMMAND,
    )
    expect(printEnvironmentCommand('pwsh', false)).toBe(POWERSHELL_COMMAND)
  })

  it('should keep the previous behaviour when the shell is unknown', () => {
    expect(printEnvironmentCommand(undefined, false)).toBe('env | sort')
    expect(printEnvironmentCommand(undefined, true)).toBe('set')
  })
})
