import { CallbackEvent, RPCRequest } from '@bus/callbacks'
import {
  Disposable,
  Uri,
  Webview,
  WebviewView,
  WebviewViewProvider,
  window,
  workspace,
} from 'vscode'
import { getWorkspaceFolders } from '../utilities/common/vscodeapi'
import { LSPClient } from '../lsp/lsp'
import { isErr } from '@bus/result'

export class LineagePanel implements WebviewViewProvider, Disposable {
  public static readonly viewType = 'sqlmesh.lineage'

  private panel: WebviewView | undefined
  private lsp: LSPClient
  private readonly extensionUri: Uri

  private disposables: Disposable[] = []

  public constructor(extensionUri: Uri, lsp: LSPClient) {
    this.extensionUri = extensionUri
    this.lsp = lsp

    if (this.panel) {
      this.panel.webview.html = this.getHTML(this.panel.webview)
    }

    this.disposables.push(
      workspace.onDidSaveTextDocument(document => {
        this.panel?.webview.postMessage({
          key: 'vscode_send',
          payload: {
            key: 'savedFile',
            payload: { fileUri: document.uri.toString() },
          },
        })
      }),
    )
    this.disposables.push(
      window.onDidChangeActiveTextEditor(editor => {
        if (editor) {
          this.panel?.webview.postMessage({
            key: 'vscode_send',
            payload: {
              key: 'changeFocusOnFile',
              payload: { path: editor.document.uri.fsPath },
            },
          })
        }
      }),
    )
  }

  public resolveWebviewView(webviewView: WebviewView) {
    if (this.panel) {
      webviewView = this.panel
    }
    this.panel = webviewView

    webviewView.webview.options = {
      // Allow scripts in the webview
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    }

    // Set content options for external URL access
    // Set up message listener for events from the iframe
    const disposable = webviewView.webview.onDidReceiveMessage(
      async request => {
        if (!request) {
          return
        }
        if (!request.key) {
          return
        }
        const message: CallbackEvent = request
        switch (message.key) {
          case 'openFile': {
            const workspaceFolders = getWorkspaceFolders()
            if (workspaceFolders.length != 1) {
              throw new Error('Only one workspace folder is supported')
            }
            const fullPath = Uri.file(message.payload.filePath)
            const document = await workspace.openTextDocument(fullPath)
            await window.showTextDocument(document)
            break
          }
          case 'rpcRequest': {
            const payload: RPCRequest = message.payload
            const requestId = payload.requestId
            switch (payload.method) {
              case 'api_query': {
                const response = await this.lsp.call_custom_method(
                  'sqlmesh/api',
                  payload.params,
                )
                let responseCallback: CallbackEvent
                if (isErr(response)) {
                  let errorMessage: string
                  switch (response.error.type) {
                    case 'generic':
                      errorMessage = response.error.message
                      break
                    case 'invalid_state':
                      errorMessage = `Invalid state: ${response.error.message}`
                      break
                    case 'sqlmesh_outdated':
                      errorMessage = `SQLMesh version issue: ${response.error.message}`
                      break
                    default:
                      errorMessage = 'Unknown error'
                  }
                  responseCallback = {
                    key: 'rpcResponse',
                    payload: {
                      requestId,
                      result: {
                        ok: false,
                        error: errorMessage,
                      },
                    },
                  }
                } else {
                  responseCallback = {
                    key: 'rpcResponse',
                    payload: {
                      requestId,
                      result: response,
                    },
                  }
                }
                await webviewView.webview.postMessage(responseCallback)
                break
              }
              case 'get_active_file': {
                const active_file = window.activeTextEditor?.document.uri.fsPath
                const responseCallback: CallbackEvent = {
                  key: 'rpcResponse',
                  payload: {
                    requestId,
                    result: {
                      fileUri: active_file,
                    },
                  },
                }
                await webviewView.webview.postMessage(responseCallback)
                break
              }
              default: {
                throw new Error(`Unhandled RPC method: ${payload.method}`)
              }
            }
            break
          }
          default:
            console.error(
              'Unhandled message type under queryRequest: ',
              message,
            )
        }
      },
      undefined,
      [],
    )
    this.disposables.push(disposable)
    webviewView.webview.html = this.getHTML(webviewView.webview)
  }

  private getHTML(panel: Webview) {
    const cssUri = panel.asWebviewUri(
      Uri.joinPath(this.extensionUri, 'src_react', 'assets', 'index.css'),
    )
    const jsUri = panel.asWebviewUri(
      Uri.joinPath(this.extensionUri, 'src_react', 'assets', 'index.js'),
    )
    const faviconUri = panel.asWebviewUri(
      Uri.joinPath(this.extensionUri, 'src_react', 'favicon.ico'),
    )
    const logoUri = panel.asWebviewUri(
      Uri.joinPath(this.extensionUri, 'src_react', 'logo192.png'),
    )

    // Handle query requests from the React app

    return `
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link rel="icon" href="${faviconUri}" />
    <meta name="theme-color" content="#000000" />
    <meta
      name="description"
      content="Web site created using create-tsrouter-app"
    />
    <link rel="apple-touch-icon" href="${logoUri}" />
    <title>Create TanStack App - react</title>
    <script>
      window.__SQLMESH_PANEL_TYPE__ = 'lineage';
    </script>
    <script type="module" crossorigin src="${jsUri}"></script>
    <link rel="stylesheet" crossorigin href="${cssUri}">
   </head>
  <body>
    <div id="app"></div>
  </body>
</html>
`
  }

  dispose() {
    // WebviewView doesn't have a dispose method
    // We can clear references
    this.panel = undefined
    this.disposables.forEach(disposable => {
      disposable.dispose()
    })
    this.disposables = []
  }
}
