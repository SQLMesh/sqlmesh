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
import React, { useState } from 'react'
import { ModelSQLMeshModel } from '@/domain/sqlmesh-model'
import { useEventBus } from '@/hooks/eventBus'
import type { VSCodeEvent } from '@bus/callbacks'
import type { Model } from '@/api/client'
import { useRpc } from '@/utils/rpc'
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
            emit('changeFocusedFile', { fileUri: payload.payload.path })
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

function Lineage() {
  const [selectedModel, setSelectedModel] = useState<string | undefined>(
    undefined,
  )
  const { on } = useEventBus()
  const queryClient = useQueryClient()

  const {
    data: models,
    isLoading: isLoadingModels,
    error: modelsError,
  } = useApiModels()
  const rpc = useRpc()
  React.useEffect(() => {
    const fetchFirstTimeModelIfNotSet = async (
      models: Model[],
    ): Promise<string | undefined> => {
      if (!Array.isArray(models)) {
        return undefined
      }
      const activeFile = await rpc('get_active_file', {})
      // @ts-ignore
      if (!activeFile.fileUri) {
        return models[0].name
      }
      // @ts-ignore
      const filePath: string = activeFile.fileUri
      const model = models.find((m: Model) => {
        if (!m.full_path) {
          return false
        }
        return m.full_path === filePath
      })
      if (model) {
        return model.name
      }
      return undefined
    }
    if (selectedModel === undefined && Array.isArray(models)) {
      fetchFirstTimeModelIfNotSet(models).then(modelName => {
        if (modelName && selectedModel === undefined) {
          setSelectedModel(modelName)
        } else {
          setSelectedModel(models[0].name)
        }
      })
    }
  }, [models, selectedModel])

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
    const handleChangeFocusedFile = (fileUri: { fileUri: string }) => {
      const model = Object.values(modelsRecord).find(
        m => m.full_path === fileUri.fileUri,
      )
      if (model) {
        setSelectedModel(model.name)
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

  if (
    isLoadingModels ||
    models === undefined ||
    modelsRecord === false ||
    selectedModel === undefined
  ) {
    return <div>Loading models...</div>
  }
  if (!Array.isArray(models)) {
    return <div>Error: Models data is not in the expected format</div>
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
