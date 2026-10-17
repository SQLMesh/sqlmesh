/**
 * The shell families we know how to print environment variables in.
 */
export type ShellFamily = 'fish' | 'powershell' | 'cmd' | 'posix'

/**
 * Commands that list the environment variables of the current shell, one per
 * shell family.
 */
const PRINT_ENVIRONMENT_COMMANDS: Record<ShellFamily, string> = {
  fish: 'set',
  powershell: 'Get-ChildItem Env: | Sort-Object Name',
  cmd: 'set',
  posix: 'env | sort',
}

const SHELL_FAMILIES_BY_NAME: Record<string, ShellFamily> = {
  fish: 'fish',
  powershell: 'powershell',
  pwsh: 'powershell',
  cmd: 'cmd',
  bash: 'posix',
  zsh: 'posix',
  sh: 'posix',
  dash: 'posix',
  ksh: 'posix',
  ash: 'posix',
}

/**
 * Extracts the executable name from a shell path, which may use either
 * separator and may carry a Windows executable extension.
 */
const shellName = (shellPath: string): string => {
  const base = shellPath.split(/[\\/]/).pop() ?? ''
  return base.toLowerCase().replace(/\.exe$/, '')
}

/**
 * Resolves a shell path to the family whose syntax it accepts.
 *
 * Unrecognized shells fall back on the platform default, which keeps POSIX
 * shells we have not listed working.
 *
 * @param shellPath The path reported by `vscode.env.shell`, which is empty in
 * environments without a shell
 * @param isWindows Whether the extension host runs on Windows
 */
export const detectShellFamily = (
  shellPath: string | undefined,
  isWindows: boolean,
): ShellFamily => {
  const family = shellPath
    ? SHELL_FAMILIES_BY_NAME[shellName(shellPath)]
    : undefined
  if (family) {
    return family
  }
  return isWindows ? 'cmd' : 'posix'
}

/**
 * Returns the command that prints all environment variables in the given
 * shell.
 *
 * @param shellPath The path reported by `vscode.env.shell`, which is empty in
 * environments without a shell
 * @param isWindows Whether the extension host runs on Windows
 */
export const printEnvironmentCommand = (
  shellPath: string | undefined,
  isWindows: boolean,
): string => PRINT_ENVIRONMENT_COMMANDS[detectShellFamily(shellPath, isWindows)]
