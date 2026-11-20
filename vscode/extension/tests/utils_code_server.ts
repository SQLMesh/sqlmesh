import { spawn, ChildProcess } from 'child_process'
import path from 'path'
import fs from 'fs-extra'
import os from 'os'
import { clearTimeout } from 'node:timers'

export interface CodeServerContext {
  codeServerProcess: ChildProcess
  codeServerPort: number
  tempDir: string
  defaultPythonInterpreter: string
}

/**
 * Get the path to the extensions directory set up by global setup
 * @returns The extensions directory path
 */
function getExtensionsDir(): string {
  const extensionDir = path.join(__dirname, '..')
  const extensionsDir = path.join(extensionDir, '.test_setup', 'extensions')

  if (!fs.existsSync(extensionsDir)) {
    throw new Error(
      `Extensions directory not found at ${extensionsDir}. Make sure global setup has run.`,
    )
  }

  return extensionsDir
}

/**
 * Creates a .vscode/settings.json specifier for the Python interpreter
 */
export const createPythonInterpreterSettingsSpecifier = async (
  directory: string,
): Promise<string> => {
  const defaultPythonInterpreter = path.join(
    __dirname,
    '..',
    '..',
    '..',
    '.venv',
    'bin',
    'python',
  )
  const vscodeDir = path.join(directory, '.vscode')
  await fs.ensureDir(vscodeDir)
  const settingsFilePath = path.join(vscodeDir, 'settings.json')
  await fs.writeJson(settingsFilePath, {
    'python.defaultInterpreterPath': defaultPythonInterpreter,
  })
  return settingsFilePath
}

/**
 * @param tempDir - The temporary directory to use for the code-server instance
 * @param placeFileWithPythonInterpreter - Whether to place a vscode/settings.json file in the temp directory that points to the python interpreter of the environmen the test is running in.
 * @returns The code-server context
 */
export async function startCodeServer({
  tempDir,
}: {
  tempDir: string
}): Promise<CodeServerContext> {
  // Get the extensions directory set up by global setup
  const extensionsDir = getExtensionsDir()

  // Find an available port
  const codeServerPort = Math.floor(Math.random() * 10000) + 50000
  const defaultPythonInterpreter = path.join(
    __dirname,
    '..',
    '..',
    '..',
    '.venv',
    'bin',
    'python',
  )

  const userDataDir = await fs.mkdtemp(
    path.join(os.tmpdir(), 'vscode-test-sushi-user-data-dir-'),
  )
  const userSettingsDir = path.join(userDataDir, 'User')
  await fs.ensureDir(userSettingsDir)
  await fs.writeJson(path.join(userSettingsDir, 'settings.json'), {
    // `--disable-telemetry` disables code-server's own telemetry, but VS Code
    // derives `env.isTelemetryEnabled` from this global user preference.
    'telemetry.telemetryLevel': 'off',
  })

  // Start code-server instance using the shared extensions directory
  const codeServerProcess = spawn(
    'pnpm',
    [
      'run',
      'code-server',
      '--bind-addr',
      `127.0.0.1:${codeServerPort}`,
      '--auth',
      'none',
      '--disable-telemetry',
      '--disable-update-check',
      '--disable-workspace-trust',
      '--user-data-dir',
      userDataDir,
      '--extensions-dir',
      extensionsDir,
      tempDir,
    ],
    {
      stdio: 'pipe',
      cwd: path.join(__dirname, '..'),
    },
  )

  // Wait for code-server to be ready
  await new Promise<void>((resolve, reject) => {
    let output = ''
    const timeout = setTimeout(() => {
      reject(new Error('Code-server failed to start within timeout'))
    }, 30000)

    codeServerProcess.stdout?.on('data', (data: Buffer) => {
      output += data.toString()
      if (output.includes('HTTP server listening on')) {
        clearTimeout(timeout)
        resolve()
      }
    })

    codeServerProcess.stderr?.on('data', (data: Buffer) => {
      console.error('Code-server stderr:', data.toString())
    })

    codeServerProcess.on('error', error => {
      clearTimeout(timeout)
      reject(error)
    })

    codeServerProcess.on('exit', code => {
      if (code !== 0) {
        clearTimeout(timeout)
        console.error('Code-server exited with code:', code)
      }
    })
  })

  return {
    codeServerProcess,
    codeServerPort,
    tempDir,
    defaultPythonInterpreter,
  }
}

export async function stopCodeServer(
  context: CodeServerContext,
): Promise<void> {
  const { codeServerProcess, tempDir } = context

  // Clean up code-server process
  codeServerProcess.kill('SIGTERM')

  // Wait for process to exit
  await new Promise<void>(resolve => {
    codeServerProcess.on('exit', () => {
      resolve()
    })
    // Force kill after 5 seconds
    setTimeout(() => {
      if (!codeServerProcess.killed) {
        codeServerProcess.kill('SIGKILL')
      }
      resolve()
    }, 5000)
  })

  // Clean up temporary directory
  try {
    await fs.remove(tempDir)
  } catch (error) {
    // Ignore errors when removing temp directory
    console.warn(`Failed to remove temp directory ${tempDir}:`, error)
  }
}
