import { useApiModelLineage, useApiModels } from '@/api/index'
import { useEffect, useMemo, useState } from 'react'
import { type ModelSQLMeshModel } from '@/domain/sqlmesh-model'
import { type HighlightedNodes, useLineageFlow } from './context'
import ReactFlow, {
  Controls,
  Background,
  BackgroundVariant,
  type EdgeChange,
  applyEdgeChanges,
  applyNodeChanges,
  type NodeChange,
  useReactFlow,
  type Edge,
  type Node,
  ReactFlowProvider,
} from 'reactflow'
import Loading from '@/components/loading/Loading'
import Spinner from '@/components/logo/Spinner'
import { createLineageWorker } from '@/components/graph/workers/index'
import { isArrayEmpty, isFalse, isNil, isNotNil } from '@/utils/index'
import clsx from 'clsx'
import ModelNode from './ModelNode'
import {
  getEdges,
  getLineageIndex,
  getActiveNodes,
  getUpdatedNodes,
  getUpdatedEdges,
  createGraphLayout,
} from './help'
import { SettingsControl } from '@/components/graph/SettingsControl'
import {
  toModelLineage,
  type ModelLineage as ModelLineageType,
} from '@/domain/lineage'
import './Graph.css'
import {
  toKeys,
  type LineageWorkerMessage,
  type LineageWorkerRequestMessage,
  type LineageWorkerResponseMessage,
  type LineageWorkerErrorMessage,
} from './types'
import { encode } from '@/domain/models'

const WITH_COLUMNS_LIMIT = 30

export function ModelLineage({
  model,
  highlightedNodes,
}: {
  model: ModelSQLMeshModel
  highlightedNodes?: HighlightedNodes
}): JSX.Element {
  const {
    setActiveNodes,
    setActiveEdges,
    setConnections,
    setLineage,
    handleError,
    setSelectedNodes,
    setMainNode,
    setWithColumns,
    setHighlightedNodes,
    setNodeConnections,
    setLineageCache,
    setUnknownModels,
    models,
    unknownModels,
    setWithSecondary,
    setWithConnected,
    setWithImpacted,
  } = useLineageFlow()

  useEffect(() => {
    setWithColumns(true)
    setWithSecondary(true)
    setWithConnected(true)
    setWithImpacted(true)
  }, [setWithSecondary])

  const { refetch: getModelLineage, isFetching: isFetchingModelLineage } =
    useApiModelLineage(model.name)
  const { isFetching: isFetchingModels } = useApiModels()

  const [isMergingModels, setIsMergingModels] = useState(false)
  const [modelLineage, setModelLineage] = useState<
    ModelLineageType | undefined
  >(undefined)

  useEffect(() => {
    const lineageWorker = new createLineageWorker()

    lineageWorker.addEventListener('message', handleLineageWorkerMessage)

    getModelLineage()
      .then(({ data }) => {
        setModelLineage(data ? toModelLineage(data) : undefined)
        if (isNil(data)) return

        setIsMergingModels(true)

        const message: LineageWorkerRequestMessage = {
          topic: 'lineage',
          payload: {
            currentLineage: {},
            newLineage: data,
            mainNode: model.fqn,
          },
        }
        lineageWorker.postMessage(message)
      })
      .catch(error => {
        handleError?.(error)
      })
      .finally(() => {
        setActiveNodes(new Set())
        setActiveEdges(new Map())
        setConnections(new Map())
        setSelectedNodes(new Set())
        setLineageCache(undefined)
        setMainNode(model.fqn)
      })

    return () => {
      lineageWorker.removeEventListener('message', handleLineageWorkerMessage)
      lineageWorker.terminate()

      setLineage({})
      setNodeConnections({})
      setMainNode(undefined)
      setHighlightedNodes({})
    }
  }, [model.name, model.hash])

  useEffect(() => {
    const modelNames = toKeys(modelLineage ?? {})
    for (const modelName of modelNames) {
      const encodedModelName = encode(modelName)
      if (
        isFalse(encodedModelName in models) &&
        isFalse(encodedModelName in unknownModels)
      ) {
        unknownModels.add(encodedModelName)
      }
    }

    setUnknownModels(new Set(unknownModels))
  }, [modelLineage, models])

  useEffect(() => {
    setHighlightedNodes(highlightedNodes ?? {})
  }, [highlightedNodes])

  function handleLineageWorkerMessage(
    e: MessageEvent<LineageWorkerMessage>,
  ): void {
    if (e.data.topic === 'lineage') {
      const message = e.data as LineageWorkerResponseMessage
      setIsMergingModels(false)
      setNodeConnections(message.payload.nodesConnections)
      setLineage(message.payload.lineage)

      if (
        Object.values(message.payload.lineage ?? {}).length > WITH_COLUMNS_LIMIT
      ) {
        setWithColumns(false)
      }
    }

    if (e.data.topic === 'error') {
      const message = e.data as LineageWorkerErrorMessage
      handleError?.(message.error)
      setIsMergingModels(false)
    }
  }

  const isFetching =
    isFetchingModelLineage || isFetchingModels || isMergingModels

  return (
    <div className="relative h-full w-full overflow-hidden">
      {isFetching && (
        <div className="absolute top-0 left-0 z-10 flex justify-center items-center w-full h-full">
          <span className="absolute w-full h-full z-10 bg-transparent-20 backdrop-blur-lg"></span>
          <Loading className="inline-block z-10">
            <Spinner className="w-3 h-3 border border-neutral-10 mr-4" />
            <h3 className="text-md whitespace-nowrap">
              {isFetching ? "Loading Model's Lineage..." : "Merging Model's..."}
            </h3>
          </Loading>
        </div>
      )}
      <ReactFlowProvider>
        <ModelColumnLineage />
      </ReactFlowProvider>
    </div>
  )
}

function ModelColumnLineage(): JSX.Element {
  const {
    withColumns,
    lineage,
    mainNode,
    selectedEdges,
    selectedNodes,
    withConnected,
    withImpacted,
    withSecondary,
    withOnlyDirect,
    directNeighbors,
    hasBackground,
    activeEdges,
    connectedNodes,
    connections,
    nodesMap,
    handleError,
    setActiveNodes,
    setWithColumns,
    setWithOnlyDirect,
  } = useLineageFlow()

  const { setCenter } = useReactFlow()

  const [isBuildingLayout, setIsBuildingLayout] = useState(false)

  const nodeTypes = useMemo(() => ({ model: ModelNode }), [])

  const allEdges = useMemo(() => getEdges(lineage), [lineage])
  const lineageIndex = useMemo(() => getLineageIndex(lineage), [lineage])

  const [nodes, setNodes] = useState<Node[]>([])
  const [edges, setEdges] = useState<Edge[]>([])

  useEffect(() => {
    if (isArrayEmpty(allEdges) || isNil(mainNode)) return

    setIsBuildingLayout(true)

    const newActiveNodes = getActiveNodes(
      allEdges,
      activeEdges,
      selectedEdges,
      nodesMap,
    )
    const newNodes = getUpdatedNodes(
      Object.values(nodesMap),
      newActiveNodes,
      mainNode,
      connectedNodes,
      selectedNodes,
      connections,
      withConnected,
      withImpacted,
      withSecondary,
      withOnlyDirect,
      directNeighbors,
    )
    const newEdges = getUpdatedEdges(
      allEdges,
      connections,
      activeEdges,
      newActiveNodes,
      selectedEdges,
      selectedNodes,
      connectedNodes,
      withConnected,
      withImpacted,
      withSecondary,
      withOnlyDirect,
      directNeighbors,
    )
    const createLayout = createGraphLayout({
      nodesMap,
      nodes: newNodes,
      edges: newEdges,
    })

    createLayout
      .create()
      .then(layout => {
        setEdges(layout.edges)
        setNodes(layout.nodes)
      })
      .catch(error => {
        handleError?.(error)
        setEdges([])
        setNodes([])
      })
      .finally(() => {
        const node = isNil(mainNode) ? undefined : nodesMap[mainNode]

        if (isNotNil(node)) {
          setCenter(node.position.x, node.position.y, {
            zoom: 0.5,
            duration: 0,
          })
        }

        setTimeout(() => {
          setIsBuildingLayout(false)
        }, 100)
      })

    return () => {
      createLayout.terminate()

      setEdges([])
      setNodes([])
    }
  }, [activeEdges, nodesMap, lineageIndex])

  useEffect(() => {
    if (isNil(mainNode) || isArrayEmpty(nodes)) return

    const newActiveNodes = getActiveNodes(
      allEdges,
      activeEdges,
      selectedEdges,
      nodesMap,
    )
    const newNodes = getUpdatedNodes(
      nodes,
      newActiveNodes,
      mainNode,
      connectedNodes,
      selectedNodes,
      connections,
      withConnected,
      withImpacted,
      withSecondary,
      withOnlyDirect,
      directNeighbors,
    )

    const newEdges = getUpdatedEdges(
      allEdges,
      connections,
      activeEdges,
      newActiveNodes,
      selectedEdges,
      selectedNodes,
      connectedNodes,
      withConnected,
      withImpacted,
      withSecondary,
      withOnlyDirect,
      directNeighbors,
    )

    setEdges(newEdges)
    setNodes(newNodes)
    setActiveNodes(newActiveNodes)
  }, [
    connections,
    nodesMap,
    allEdges,
    activeEdges,
    selectedNodes,
    selectedEdges,
    connectedNodes,
    withConnected,
    withImpacted,
    withSecondary,
    withOnlyDirect,
    directNeighbors,
    withColumns,
    mainNode,
  ])

  function onNodesChange(changes: NodeChange[]): void {
    setNodes(applyNodeChanges(changes, nodes))
  }

  function onEdgesChange(changes: EdgeChange[]): void {
    setEdges(applyEdgeChanges(changes, edges))
  }

  return (
    <>
      {isBuildingLayout && (
        <div className="absolute top-0 left-0 z-10 flex justify-center items-center w-full h-full">
          <span className="absolute w-full h-full z-10 bg-transparent-20 backdrop-blur-lg"></span>
          <Loading className="inline-block z-10">
            <Spinner className="w-3 h-3 border border-neutral-10 mr-4" />
            <h3 className="text-md whitespace-nowrap">Building Lineage...</h3>
          </Loading>
        </div>
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeOrigin={[0.5, 0.5]}
        minZoom={0.05}
        maxZoom={1.5}
        snapGrid={[16, 16]}
        snapToGrid
      >
        <Controls
          className="bg-light p-1 rounded-md !border-none !shadow-lg"
          showInteractive={false}
        >
          <SettingsControl
            showColumns={withColumns}
            onWithColumnsChange={setWithColumns}
            withOnlyDirect={withOnlyDirect}
            onWithOnlyDirectChange={setWithOnlyDirect}
          />
        </Controls>
        <Background
          variant={BackgroundVariant.Cross}
          gap={32}
          size={4}
          className={clsx(
            hasBackground
              ? 'opacity-100 stroke-neutral-200 dark:stroke-neutral-800'
              : 'opacity-0',
          )}
        />
      </ReactFlow>
    </>
  )
}
