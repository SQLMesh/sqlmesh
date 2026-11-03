import * as vscode from 'vscode'
import { getSqlmeshEnvironment } from '../utilities/sqlmesh/sqlmesh'
import { isErr } from '@bus/result'
import { IS_WINDOWS } from '../utilities/isWindows'
import { printEnvironmentCommand } from '../utilities/shellCommand'

export function printEnvironment() {
  return async () => {
    const envResult = await getSqlmeshEnvironment()

    if (isErr(envResult)) {
      await vscode.window.showErrorMessage(envResult.error)
      return
    }

    const env = envResult.value

    // Create a new terminal with the SQLMesh environment
    const terminal = vscode.window.createTerminal({
      name: 'SQLMesh Environment',
      env: env,
    })

    // Show the terminal
    terminal.show()

    // Run the command the user's shell understands, since the terminal is
    // opened with the default shell rather than a shell we pick
    terminal.sendText(printEnvironmentCommand(vscode.env.shell, IS_WINDOWS))

    // Show a notification
    vscode.window.showInformationMessage(
      'SQLMesh environment variables displayed in terminal',
    )
  }
}
