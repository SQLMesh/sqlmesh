import * as vscode from 'vscode'
import { LSPClient } from '../lsp/lsp'
import { isErr } from '@bus/result'
import { CallbackEvent, RPCRequest } from '@bus/callbacks'
import { getWorkspaceFolders } from '../utilities/common/vscodeapi'

interface ModelInfo {
  name: string
  fqn: string
  description?: string | null
}

export function showTableDiff(
  lspClient?: LSPClient,
  extensionUri?: vscode.Uri,
) {
  return async () => {
    if (!lspClient) {
      vscode.window.showErrorMessage('LSP client not available')
      return
    }

    if (!extensionUri) {
      vscode.window.showErrorMessage('Extension URI not available')
      return
    }

    // Get the current active editor
    const activeEditor = vscode.window.activeTextEditor
    let selectedModelInfo: ModelInfo | null = null

    if (!activeEditor) {
      // No active editor, show a list of all models
      const allModelsResult = await lspClient.call_custom_method(
        'sqlmesh/get_models',
        {},
      )

      if (isErr(allModelsResult)) {
        vscode.window.showErrorMessage(
          `Failed to get models: ${allModelsResult.error.message}`,
        )
        return
      }

      if (
        !allModelsResult.value.models ||
        allModelsResult.value.models.length === 0
      ) {
        vscode.window.showInformationMessage('No models found in the project')
        return
      }

      // Let user choose from all models
      const items = (allModelsResult.value.models as ModelInfo[]).map(
        (model: ModelInfo) => ({
          label: model.name,
          description: model.fqn,
          detail: model.description ? model.description : undefined,
          model: {
            name: model.name,
            fqn: model.fqn,
            description: model.description,
          },
        }),
      )

      const selected = await vscode.window.showQuickPick(items, {
        placeHolder: 'Select a model for table diff',
      })

      if (!selected) {
        return
      }

      selectedModelInfo = selected.model
    } else {
      // Get the current document URI and check if it contains models
      const documentUri = activeEditor.document.uri.toString(true)

      // Call the render model API to get models in the current file
      const result = await lspClient.call_custom_method(
        'sqlmesh/render_model',
        {
          textDocumentUri: documentUri,
        },
      )

      if (isErr(result)) {
        vscode.window.showErrorMessage(
          `Failed to get models from current file: ${result.error.message}`,
        )
        return
      }

      // Check if we got any models
      if (!result.value.models || result.value.models.length === 0) {
        vscode.window.showInformationMessage(
          'No models found in the current file',
        )
        return
      }

      // If multiple models, let user choose
      if (result.value.models.length > 1) {
        const items = result.value.models.map(model => ({
          label: model.name,
          description: model.fqn,
          detail: model.description ? model.description : undefined,
          model: model,
        }))

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: 'Select a model for table diff',
        })

        if (!selected) {
          return
        }

        selectedModelInfo = selected.model
      } else {
        selectedModelInfo = result.value.models[0]
      }
    }

    // Ensure we have a selected model
    if (!selectedModelInfo) {
      vscode.window.showErrorMessage('No model selected')
      return
    }

    // Get environments for selection
    const environmentsResult = await lspClient.call_custom_method(
      'sqlmesh/get_environments',
      {},
    )

    if (isErr(environmentsResult)) {
      vscode.window.showErrorMessage(
        `Failed to get environments: ${environmentsResult.error.message}`,
      )
      return
    }

    const environments = environmentsResult.value.environments || {}
    const environmentNames = Object.keys(environments)

    if (environmentNames.length === 0) {
      vscode.window.showErrorMessage('No environments found')
      return
    }

    // Let user select source environment
    const sourceEnvironmentItems = environmentNames.map(env => ({
      label: env,
      description: `Source environment: ${env}`,
    }))

    const selectedSourceEnv = await vscode.window.showQuickPick(
      sourceEnvironmentItems,
      {
        placeHolder: 'Select source environment',
      },
    )

    if (!selectedSourceEnv) {
      return
    }

    // Let user select target environment (excluding source)
    const targetEnvironmentItems = environmentNames
      .filter(env => env !== selectedSourceEnv.label)
      .map(env => ({
        label: env,
        description: `Target environment: ${env}`,
      }))

    if (targetEnvironmentItems.length === 0) {
      vscode.window.showErrorMessage(
        'Need at least two environments for comparison',
      )
      return
    }

    const selectedTargetEnv = await vscode.window.showQuickPick(
      targetEnvironmentItems,
      {
        placeHolder: 'Select target environment',
      },
    )

    if (!selectedTargetEnv) {
      return
    }

    // Run table diff immediately with selected parameters
    const tableDiffResult = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'SQLMesh',
        cancellable: false,
      },
      async progress => {
        progress.report({ message: 'Calculating table differences...' })

        return await lspClient.call_custom_method('sqlmesh/api', {
          method: 'GET',
          url: '/api/table_diff',
          params: {
            source: selectedSourceEnv.label,
            target: selectedTargetEnv.label,
            model_or_snapshot: selectedModelInfo.name,
          },
          body: {},
        })
      },
    )

    if (isErr(tableDiffResult)) {
      vscode.window.showErrorMessage(
        `Failed to run table diff: ${tableDiffResult.error.message}`,
      )
      return
    }

    // Determine the view column for side-by-side display
    // Find the rightmost column with an editor
    let maxColumn = vscode.ViewColumn.One
    for (const editor of vscode.window.visibleTextEditors) {
      if (editor.viewColumn && editor.viewColumn > maxColumn) {
        maxColumn = editor.viewColumn
      }
    }

    // Open in the next column after the rightmost editor
    const viewColumn = maxColumn + 1

    // Create a webview panel for the table diff
    const panel = vscode.window.createWebviewPanel(
      'sqlmesh.tableDiff',
      `SQLMesh Table Diff - ${selectedModelInfo.name} (${selectedSourceEnv.label} → ${selectedTargetEnv.label})`,
      viewColumn,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [extensionUri],
      },
    )

    // Store the initial data for the webview
    // eslint-disable-next-line prefer-const
    let initialData = {
      selectedModel: selectedModelInfo,
      sourceEnvironment: selectedSourceEnv.label,
      targetEnvironment: selectedTargetEnv.label,
      tableDiffData: tableDiffResult.value,
      environments: environments,
    }

    // Set up message listener for events from the webview
    panel.webview.onDidReceiveMessage(
      async request => {
        if (!request || !request.key) {
          return
        }
        const message: CallbackEvent = request
        switch (message.key) {
          case 'openFile': {
            const workspaceFolders = getWorkspaceFolders()
            if (workspaceFolders.length != 1) {
              throw new Error('Only one workspace folder is supported')
            }
            const fullPath = vscode.Uri.file(message.payload.filePath)
            const document = await vscode.workspace.openTextDocument(fullPath)
            await vscode.window.showTextDocument(document)
            break
          }
          case 'rpcRequest': {
            const payload: RPCRequest = message.payload
            const requestId = payload.requestId
            switch (payload.method) {
              case 'api_query': {
                const response = await lspClient.call_custom_method(
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
                await panel.webview.postMessage(responseCallback)
                break
              }
              case 'get_active_file': {
                const active_file =
                  vscode.window.activeTextEditor?.document.uri.fsPath
                const responseCallback: CallbackEvent = {
                  key: 'rpcResponse',
                  payload: {
                    requestId,
                    result: {
                      fileUri: active_file,
                    },
                  },
                }
                await panel.webview.postMessage(responseCallback)
                break
              }
              case 'get_selected_model': {
                const responseCallback: CallbackEvent = {
                  key: 'rpcResponse',
                  payload: {
                    requestId,
                    result: {
                      ok: true,
                      value: {
                        selectedModel: initialData.selectedModel,
                      },
                    },
                  },
                }
                await panel.webview.postMessage(responseCallback)
                break
              }
              case 'get_initial_data': {
                const responseCallback: CallbackEvent = {
                  key: 'rpcResponse',
                  payload: {
                    requestId,
                    result: {
                      ok: true,
                      value: {
                        selectedModel: initialData.selectedModel,
                        sourceEnvironment: initialData.sourceEnvironment,
                        targetEnvironment: initialData.targetEnvironment,
                        tableDiffData: initialData.tableDiffData,
                        environments: initialData.environments,
                      },
                    },
                  },
                }
                await panel.webview.postMessage(responseCallback)
                break
              }
              case 'get_all_models': {
                const allModelsResult = await lspClient.call_custom_method(
                  'sqlmesh/get_models',
                  {},
                )

                let responseCallback: CallbackEvent
                if (isErr(allModelsResult)) {
                  responseCallback = {
                    key: 'rpcResponse',
                    payload: {
                      requestId,
                      result: {
                        ok: false,
                        error: `Failed to get models: ${allModelsResult.error.message}`,
                      },
                    },
                  }
                } else {
                  responseCallback = {
                    key: 'rpcResponse',
                    payload: {
                      requestId,
                      result: {
                        ok: true,
                        value: {
                          ok: true,
                          models: allModelsResult.value.models || [],
                        },
                      },
                    },
                  }
                }
                await panel.webview.postMessage(responseCallback)
                break
              }
              case 'set_selected_model': {
                const modelInfo = payload.params?.model
                if (modelInfo) {
                  initialData.selectedModel = modelInfo
                  // Update the panel title to reflect the new selection
                  panel.title = `SQLMesh Table Diff - ${modelInfo.name} (${initialData.sourceEnvironment} → ${initialData.targetEnvironment})`
                }

                const responseCallback: CallbackEvent = {
                  key: 'rpcResponse',
                  payload: {
                    requestId,
                    result: {
                      ok: true,
                      value: {
                        ok: true,
                        selectedModel: initialData.selectedModel,
                      },
                    },
                  },
                }
                await panel.webview.postMessage(responseCallback)
                break
              }
              case 'get_environments': {
                const responseCallback: CallbackEvent = {
                  key: 'rpcResponse',
                  payload: {
                    requestId,
                    result: {
                      ok: true,
                      value: {
                        ok: true,
                        environments: initialData.environments,
                      },
                    },
                  },
                }
                await panel.webview.postMessage(responseCallback)
                break
              }
              case 'run_table_diff': {
                const { sourceModel, sourceEnvironment, targetEnvironment } =
                  payload.params || {}

                if (!sourceModel || !sourceEnvironment || !targetEnvironment) {
                  const responseCallback: CallbackEvent = {
                    key: 'rpcResponse',
                    payload: {
                      requestId,
                      result: {
                        ok: false,
                        error:
                          'Missing required parameters: sourceModel, sourceEnvironment, or targetEnvironment',
                      },
                    },
                  }
                  await panel.webview.postMessage(responseCallback)
                  break
                }

                const tableDiffResult = await vscode.window.withProgress(
                  {
                    location: vscode.ProgressLocation.Notification,
                    title: 'SQLMesh',
                    cancellable: false,
                  },
                  async progress => {
                    progress.report({
                      message: 'Calculating table differences...',
                    })

                    return await lspClient.call_custom_method('sqlmesh/api', {
                      method: 'GET',
                      url: '/api/table_diff',
                      params: {
                        source: sourceEnvironment,
                        target: targetEnvironment,
                        model_or_snapshot: sourceModel,
                      },
                      body: {},
                    })
                  },
                )

                let responseCallback: CallbackEvent
                if (isErr(tableDiffResult)) {
                  responseCallback = {
                    key: 'rpcResponse',
                    payload: {
                      requestId,
                      result: {
                        ok: false,
                        error: `Failed to run table diff: ${tableDiffResult.error.message}`,
                      },
                    },
                  }
                } else {
                  responseCallback = {
                    key: 'rpcResponse',
                    payload: {
                      requestId,
                      result: {
                        ok: true,
                        value: {
                          ok: true,
                          data: tableDiffResult.value,
                        },
                      },
                    },
                  }
                }
                await panel.webview.postMessage(responseCallback)
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

    // Set the HTML content
    panel.webview.html = getHTML(panel.webview, extensionUri)
  }
}

function getHTML(webview: vscode.Webview, extensionUri: vscode.Uri): string {
  const cssUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'src_react', 'assets', 'index.css'),
  )
  const jsUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'src_react', 'assets', 'index.js'),
  )
  const faviconUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'src_react', 'favicon.ico'),
  )
  const logoUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'src_react', 'logo192.png'),
  )

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
    <title>SQLMesh Table Diff</title>
    <script>
      window.__SQLMESH_PANEL_TYPE__ = 'tablediff';
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
