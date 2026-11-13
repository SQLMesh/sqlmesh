import '../App.css'
import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
  useQueryClient,
} from '@tanstack/react-query'
import { useApiModels } from '@/api'
import LineageFlowProvider from '@/components/graph/context'
import { ModelLineage } from '@/components/graph/ModelLineage'
import { useVSCode } from '@/hooks/vscode'
import React, { useRef, useState } from 'react'
import { ModelSQLMeshModel } from '@/domain/sqlmesh-model'
import { useEventBus } from '@/hooks/eventBus'
import type { RPCMethods, VSCodeEvent } from '@bus/callbacks'
import type { Model } from '@/api/client'
import { useRpc } from '@/utils/rpc'
import { findModelsByPath } from '@/utils/path'
import {
  type ModelPath,
  type ModelFullPath,
  type ModelName,
  type ModelEncodedFQN,
} from '@/domain/models'

export function LineagePage() {
  const { emit } = useEventBus()

  // Handle messages from VSCode extension
  React.useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      // Ensure the message is from VSCode
      if (event.data && event.data.key === 'vscode_send') {
        const payload: VSCodeEvent = event.data.payload
        switch (payload.key) {
          case 'changeFocusOnFile':
            emit('changeFocusedFile', {
              fileUri: payload.payload.path,
              isWindows: payload.payload.isWindows,
            })
            break
          case 'savedFile':
            emit('savedFile', { fileUri: payload.payload.fileUri })
            break
          default:
            console.error(
              'Unhandled message type in lineage page:',
              payload.key,
            )
        }
      }
    }
    window.addEventListener('message', handleMessage)
    return () => {
      window.removeEventListener('message', handleMessage)
    }
  }, [])

  const client = new QueryClient({
    queryCache: new QueryCache({}),
    defaultOptions: {
      queries: {
        networkMode: 'always',
        refetchOnWindowFocus: false,
        retry: false,
        staleTime: Infinity,
      },
    },
  })

  return (
    <QueryClientProvider client={client}>
      <Lineage />
    </QueryClientProvider>
  )
}

/** Follow editor focus without choosing arbitrarily between ambiguous model paths. */
function Lineage() {
  const [selectedModel, setSelectedModel] = useState<string | undefined>(
    undefined,
  )
  const [activeFileError, setActiveFileError] = useState(false)
  const { on } = useEventBus()
  const queryClient = useQueryClient()

  const {
    data: models,
    isLoading: isLoadingModels,
    error: modelsError,
  } = useApiModels()
  const rpc = useRpc()
  const focusVersion = useRef(0)
  React.useEffect(() => {
    if (
      selectedModel !== undefined ||
      !Array.isArray(models) ||
      !models.length
    ) {
      return
    }
    let current = true
    const version = focusVersion.current
    // get_active_file uses a plain payload on the existing RPC wire protocol.
    setActiveFileError(false)
    rpc('get_active_file', {})
      .then(response => {
        if (!current || focusVersion.current !== version) return
        const activeFile =
          response as unknown as RPCMethods['get_active_file']['result']
        const matches = findModelsByPath(
          models,
          activeFile.fileUri,
          activeFile.isWindows,
        )
        setSelectedModel(
          previous =>
            previous ??
            (matches.length > 1 ? undefined : (matches[0] ?? models[0]).name),
        )
      })
      .catch(() => {
        if (!current || focusVersion.current !== version) return
        setActiveFileError(true)
      })
    return () => {
      current = false
    }
  }, [models, selectedModel, rpc])

  const modelsRecord =
    Array.isArray(models) &&
    models.reduce(
      (acc, model) => {
        acc[model.name] = model
        return acc
      },
      {} as Record<string, Model>,
    )

  React.useEffect(() => {
    const handleChangeFocusedFile = (file: {
      fileUri: string
      isWindows?: boolean
    }) => {
      focusVersion.current += 1
      const matches = findModelsByPath(
        Object.values(modelsRecord),
        file.fileUri,
        file.isWindows,
      )
      if (matches.length < 2) {
        setSelectedModel(
          previous =>
            matches[0]?.name ??
            previous ??
            (Array.isArray(models) ? models[0]?.name : undefined),
        )
      }
    }

    const handleSavedFile = () => {
      queryClient.invalidateQueries()
    }

    const offChangeFocusedFile = on(
      'changeFocusedFile',
      handleChangeFocusedFile,
    )
    const offSavedFile = on('savedFile', handleSavedFile)

    // If your event bus returns an "off" function, call it on cleanup
    return () => {
      if (offChangeFocusedFile) offChangeFocusedFile()
      if (offSavedFile) offSavedFile()
    }
  }, [on, queryClient, modelsRecord])

  if (modelsError) {
    return <div>Error: {modelsError.message}</div>
  }

  if (isLoadingModels || models === undefined || modelsRecord === false) {
    return <div>Loading models...</div>
  }
  if (!Array.isArray(models)) {
    return <div>Error: Models data is not in the expected format</div>
  }

  if (!models.length) {
    return <div>No models available.</div>
  }
  if (selectedModel === undefined) {
    return (
      <div>
        <p>
          {activeFileError
            ? 'Unable to determine the active model. Select a model to view its lineage.'
            : 'Select a model to view its lineage.'}
        </p>
        <select
          aria-label="Select a model"
          value=""
          onChange={event => setSelectedModel(event.target.value)}
        >
          <option
            value=""
            disabled
          >
            Select a model
          </option>
          {models.map(model => (
            <option
              key={model.name}
              value={model.name}
            >
              {model.name}
            </option>
          ))}
        </select>
      </div>
    )
  }

  return (
    <LineageComponentFromWeb
      selectedModel={selectedModel}
      models={modelsRecord}
    />
  )
}

export function LineageComponentFromWeb({
  selectedModel,
  models,
}: {
  selectedModel: string
  models: Record<string, Model>
}): JSX.Element {
  const vscode = useVSCode()
  function handleClickModel(id: string): void {
    const decodedId = decodeURIComponent(id)
    const model = Object.values(models).find(m => m.fqn === decodedId)
    if (!model) {
      throw new Error('Model not found')
    }
    if (!model.full_path) {
      return
    }
    vscode('openFile', { filePath: model.full_path })
  }

  function handleError(error: any): void {
    console.error(error)
  }

  const model = models[selectedModel]
  if (!model) {
    return <div>Error: Model not found</div>
  }

  const sqlmModel = new ModelSQLMeshModel()
  sqlmModel.update({
    ...model,
    name: model.name as ModelName,
    fqn: model.fqn as ModelEncodedFQN,
    path: model.path as ModelPath,
    full_path: model.full_path as ModelFullPath,
  })

  return (
    <div className="h-[100vh] w-[100vw]">
      <LineageFlowProvider
        showColumns={true}
        handleClickModel={handleClickModel}
        handleError={handleError}
        models={models}
        showControls={false}
      >
        <ModelLineage model={sqlmModel} />
      </LineageFlowProvider>
    </div>
  )
}
