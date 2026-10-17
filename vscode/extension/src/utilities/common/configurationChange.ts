// SPDX-License-Identifier: Apache-2.0

/**
 * Configuration sections the language server reads, so a change to any of them
 * needs the server restarted to take effect.
 *
 * `sqlmesh` covers `sqlmesh.projectPaths` and `sqlmesh.lspEntrypoint`, both of
 * which decide how the server is launched. The interpreter path matters because
 * the server runs inside that interpreter.
 */
export const RESTART_CONFIGURATION_SECTIONS = [
  'sqlmesh',
  'python.defaultInterpreterPath',
]

/**
 * The part of `vscode.ConfigurationChangeEvent` this module needs. Declared
 * structurally so the check stays unit testable without the VS Code runtime.
 */
export interface ConfigurationChange {
  affectsConfiguration(section: string): boolean
}

interface Disposable {
  dispose(): unknown
}

export interface TelemetryRestartSubscription extends Disposable {
  runDuringInitialStart<T>(task: () => Promise<T>): Promise<T>
}

type TelemetryChangeEvent<TDisposable extends Disposable> = (
  listener: (enabled: boolean) => unknown,
) => TDisposable

/**
 * Whether a configuration change affects a setting the language server reads.
 *
 * `workspace.onDidChangeConfiguration` fires for every setting in the editor,
 * including ones written by other extensions, so the event has to be filtered
 * before it triggers a restart.
 */
export function requiresLspRestart(event: ConfigurationChange): boolean {
  return RESTART_CONFIGURATION_SECTIONS.some(section =>
    event.affectsConfiguration(section),
  )
}

/** Restart the language server whenever VS Code's effective telemetry preference changes. */
export function restartLspOnTelemetryChange<TDisposable extends Disposable>(
  event: TelemetryChangeEvent<TDisposable>,
  restartLsp: () => Promise<void>,
): TelemetryRestartSubscription {
  let initialStartComplete = false
  let restartPending = false
  const subscription = event(() => {
    if (!initialStartComplete) {
      restartPending = true
      return
    }
    void restartLsp()
  })

  return {
    dispose: () => subscription.dispose(),
    runDuringInitialStart: async task => {
      try {
        return await task()
      } finally {
        initialStartComplete = true
        if (restartPending) {
          restartPending = false
          await restartLsp()
        }
      }
    },
  }
}
