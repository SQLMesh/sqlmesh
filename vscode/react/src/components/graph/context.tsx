import {
  createContext,
  useState,
  useContext,
  useCallback,
  useMemo,
} from 'react'
import { getNodeMap, hasActiveEdge, hasActiveEdgeConnector } from './help'
import { type Node } from 'reactflow'
import { isNil } from '@/utils/index'
import type { Lineage } from '@/domain/lineage'
import type { ModelSQLMeshModel } from '@/domain/sqlmesh-model'
import type { Column } from '@/domain/column'
import type { ModelEncodedFQN, ModelName } from '@/domain/models'
import type { ColumnName } from '@/domain/column'
import type { Model } from '@/api/client'
import { toID, toKeys } from './types'
import type { ConnectedNode } from '@/components/graph/types'

export interface Connections {
  left: string[]
  right: string[]
}
export type ActiveColumns = Map<string, { ins: string[]; outs: string[] }>
export type ActiveEdges = Map<string, Array<[string, string]>>
export type ActiveNodes = Set<ModelEncodedFQN>
export type SelectedNodes = Set<ModelEncodedFQN>
export type HighlightedNodes = Record<string, string[]>

interface LineageFlow {
  lineage: Record<string, Lineage>
  lineageCache?: Record<string, Lineage>
  mainNode?: ModelEncodedFQN
  connectedNodes: Set<ModelEncodedFQN>
  activeEdges: ActiveEdges
  activeNodes: ActiveNodes
  selectedNodes: SelectedNodes
  selectedEdges: ConnectedNode[]
  models: Record<ModelName, ModelSQLMeshModel>
  unknownModels: Set<ModelEncodedFQN>
  connections: Map<string, Connections>
  withConnected: boolean
  withColumns: boolean
  hasBackground: boolean
  withImpacted: boolean
  withSecondary: boolean
  withOnlyDirect: boolean
  directNeighbors: Set<ModelEncodedFQN>
  manuallySelectedColumn?: [ModelSQLMeshModel, Column]
  highlightedNodes: HighlightedNodes
  nodesMap: Record<ModelEncodedFQN, Node>
  setHighlightedNodes: React.Dispatch<React.SetStateAction<HighlightedNodes>>
  setActiveNodes: React.Dispatch<React.SetStateAction<ActiveNodes>>
  setWithConnected: React.Dispatch<React.SetStateAction<boolean>>
  setMainNode: React.Dispatch<React.SetStateAction<ModelEncodedFQN | undefined>>
  setSelectedNodes: React.Dispatch<React.SetStateAction<SelectedNodes>>
  setWithColumns: React.Dispatch<React.SetStateAction<boolean>>
  setHasBackground: React.Dispatch<React.SetStateAction<boolean>>
  setWithImpacted: React.Dispatch<React.SetStateAction<boolean>>
  setWithSecondary: React.Dispatch<React.SetStateAction<boolean>>
  setWithOnlyDirect: React.Dispatch<React.SetStateAction<boolean>>
  setConnections: React.Dispatch<React.SetStateAction<Map<string, Connections>>>
  hasActiveEdge: (edge: [string | undefined, string | undefined]) => boolean
  addActiveEdges: (edges: Array<[string, string]>) => void
  removeActiveEdges: (edges: Array<[string, string]>) => void
  setActiveEdges: React.Dispatch<React.SetStateAction<ActiveEdges>>
  setUnknownModels: React.Dispatch<React.SetStateAction<Set<ModelEncodedFQN>>>
  setLineage: React.Dispatch<React.SetStateAction<Record<string, Lineage>>>
  setLineageCache: React.Dispatch<
    React.SetStateAction<Record<string, Lineage> | undefined>
  >
  handleClickModel?: (modelName: ModelEncodedFQN) => void
  handleError?: (error: any) => void
  setManuallySelectedColumn: React.Dispatch<
    React.SetStateAction<[ModelSQLMeshModel, Column] | undefined>
  >
  setNodeConnections: React.Dispatch<Record<string, any>>
  isActiveColumn: (
    modelName: ModelEncodedFQN,
    columnName: ColumnName,
  ) => boolean
}

export const LineageFlowContext = createContext<LineageFlow>({
  selectedEdges: [],
  lineage: {},
  lineageCache: undefined,
  withColumns: false,
  withConnected: false,
  withImpacted: true,
  withSecondary: false,
  withOnlyDirect: false,
  directNeighbors: new Set(),
  hasBackground: true,
  mainNode: undefined,
  activeEdges: new Map(),
  activeNodes: new Set(),
  models: {},
  unknownModels: new Set(),
  manuallySelectedColumn: undefined,
  connections: new Map(),
  selectedNodes: new Set(),
  connectedNodes: new Set(),
  highlightedNodes: {},
  nodesMap: {},
  setHighlightedNodes: () => {},
  setWithColumns: () => false,
  setHasBackground: () => false,
  setWithImpacted: () => false,
  setWithSecondary: () => false,
  setWithConnected: () => false,
  setWithOnlyDirect: () => false,
  hasActiveEdge: () => false,
  addActiveEdges: () => {},
  removeActiveEdges: () => {},
  setActiveEdges: () => {},
  handleClickModel: () => {},
  setManuallySelectedColumn: () => {},
  handleError: error => console.error(error),
  setLineage: () => {},
  setLineageCache: () => {},
  isActiveColumn: () => false,
  setConnections: () => {},
  setSelectedNodes: () => {},
  setMainNode: () => {},
  setActiveNodes: () => {},
  setNodeConnections: () => {},
  setUnknownModels: () => {},
})

export default function LineageFlowProvider({
  handleError,
  handleClickModel,
  children,
  showColumns = true,
  showConnected = false,
  showControls = true,
  models,
}: {
  children: React.ReactNode
  handleClickModel?: (modelName: ModelEncodedFQN) => void
  handleError?: (error: any) => void
  showColumns?: boolean
  showConnected?: boolean
  showControls?: boolean
  models: Record<string, Model>
}): JSX.Element {
  const [lineage, setLineage] = useState<Record<string, Lineage>>({})
  const [unknownModels, setUnknownModels] = useState(new Set<ModelEncodedFQN>())
  const [lineageCache, setLineageCache] = useState<
    Record<string, Lineage> | undefined
  >(undefined)
  const [nodesConnections, setNodeConnections] = useState<
    Record<ModelEncodedFQN, any>
  >({})
  const [withColumns, setWithColumns] = useState(showColumns)
  const [mainNode, setMainNode] = useState<ModelEncodedFQN | undefined>()
  const [manuallySelectedColumn, setManuallySelectedColumn] =
    useState<[ModelSQLMeshModel, Column]>()
  const [activeEdges, setActiveEdges] = useState<ActiveEdges>(new Map())
  const [connections, setConnections] = useState<Map<string, Connections>>(
    new Map(),
  )
  const [withConnected, setWithConnected] = useState(showConnected)
  const [selectedNodes, setSelectedNodes] = useState<SelectedNodes>(new Set())
  const [activeNodes, setActiveNodes] = useState<ActiveNodes>(new Set())
  const [highlightedNodes, setHighlightedNodes] = useState<HighlightedNodes>({})
  const [hasBackground, setHasBackground] = useState(true)
  const [withImpacted, setWithImpacted] = useState(true)
  const [withSecondary, setWithSecondary] = useState(false)
  const [withOnlyDirect, setWithOnlyDirect] = useState(false)

  const nodesMap = useMemo(
    () =>
      getNodeMap({
        lineage,
        // @ts-expect-error TODO: fix this, should move to internal representation
        models,
        unknownModels,
        withColumns,
      }),
    [lineage, models, withColumns, unknownModels],
  )

  const checkActiveEdge = useCallback(
    function checkActiveEdge(
      edge: [string | undefined, string | undefined],
    ): boolean {
      return hasActiveEdge(activeEdges, edge)
    },
    [activeEdges],
  )

  const addActiveEdges = useCallback(
    function addActiveEdges(edges: Array<[string, string]>): void {
      setActiveEdges(activeEdges => {
        edges.forEach(([leftConnect, rightConnect]) => {
          const left = activeEdges.get(leftConnect) ?? []
          const right = activeEdges.get(rightConnect) ?? []
          const hasDuplicateLeft = left.some(
            ([left, right]) => left === leftConnect && right === rightConnect,
          )
          const hasDuplicateRight = right.some(
            ([left, right]) => left === leftConnect && right === rightConnect,
          )

          if (!hasDuplicateLeft) {
            left.push([leftConnect, rightConnect])
          }

          if (!hasDuplicateRight) {
            right.push([leftConnect, rightConnect])
          }

          activeEdges.set(leftConnect, left)
          activeEdges.set(rightConnect, right)
        })

        return new Map(activeEdges)
      })
    },
    [setActiveEdges],
  )

  const removeActiveEdges = useCallback(
    function removeActiveEdges(edges: Array<[string, string]>): void {
      setActiveEdges(activeEdges => {
        edges.forEach(([left, right]) => {
          const edgesLeft = (activeEdges.get(left) ?? []).filter(
            e => e[0] !== left && e[1] !== right,
          )
          const edgesRight = (activeEdges.get(right) ?? []).filter(
            e => e[0] !== left && e[1] !== right,
          )

          activeEdges.set(left, edgesLeft)
          activeEdges.set(right, edgesRight)
        })

        return new Map(activeEdges)
      })

      setConnections(connections => {
        edges.forEach(([left, right]) => {
          connections.delete(left)
          connections.delete(right)
        })

        return new Map(connections)
      })
    },
    [setActiveEdges, setConnections],
  )

  const isActiveColumn = useCallback(
    function isActive(
      modelName: ModelEncodedFQN,
      columnName: ColumnName,
    ): boolean {
      const leftConnector = toID('left', modelName, columnName)
      const rightConnector = toID('right', modelName, columnName)
      return (
        hasActiveEdgeConnector(activeEdges, leftConnector) ||
        hasActiveEdgeConnector(activeEdges, rightConnector)
      )
    },
    [checkActiveEdge, activeEdges],
  )

  const connectedNodes = useMemo(
    () => new Set(toKeys(nodesConnections)),
    [nodesConnections],
  )

  // Reverse adjacency index: parent -> children. Built once per `lineage`
  // change so per-model `directNeighbors` lookups stay O(parents + children)
  // instead of scanning the whole graph on every mainNode switch.
  const childrenByParent = useMemo(() => {
    const map = new Map<ModelEncodedFQN, ModelEncodedFQN[]>()
    for (const [child, info] of Object.entries(lineage) as Array<
      [ModelEncodedFQN, Lineage]
    >) {
      for (const parent of info?.models ?? []) {
        const existing = map.get(parent)
        if (existing) {
          existing.push(child)
        } else {
          map.set(parent, [child])
        }
      }
    }
    return map
  }, [lineage])

  const directNeighbors = useMemo(() => {
    const set = new Set<ModelEncodedFQN>()
    if (isNil(mainNode)) return set
    set.add(mainNode)
    for (const parent of lineage[mainNode]?.models ?? []) {
      set.add(parent)
    }
    for (const child of childrenByParent.get(mainNode) ?? []) {
      set.add(child)
    }
    return set
  }, [mainNode, lineage, childrenByParent])

  const selectedEdges = useMemo(
    () =>
      Array.from(selectedNodes)
        .flatMap(id => nodesConnections[id])
        .filter(Boolean) as any[],
    [nodesConnections, selectedNodes],
  )

  return (
    <LineageFlowContext.Provider
      value={{
        highlightedNodes,
        connectedNodes,
        activeEdges,
        selectedEdges,
        activeNodes,
        selectedNodes,
        mainNode,
        connections,
        lineage,
        lineageCache,
        // @ts-expect-error TODO: fix this, should move to internal representation
        models,
        manuallySelectedColumn,
        withColumns,
        withConnected,
        withImpacted,
        withSecondary,
        withOnlyDirect,
        directNeighbors,
        showControls,
        hasBackground,
        nodesMap,
        unknownModels,
        setHighlightedNodes,
        setActiveNodes,
        setNodeConnections,
        setLineageCache,
        setUnknownModels,
        setWithConnected,
        setWithImpacted,
        setWithSecondary,
        setWithOnlyDirect,
        setHasBackground,
        setSelectedNodes,
        setMainNode,
        setConnections,
        setLineage,
        setWithColumns,
        setActiveEdges,
        setManuallySelectedColumn,
        addActiveEdges,
        removeActiveEdges,
        hasActiveEdge: checkActiveEdge,
        handleClickModel,
        handleError,
        isActiveColumn,
      }}
    >
      {children}
    </LineageFlowContext.Provider>
  )
}

export function useLineageFlow(): LineageFlow {
  return useContext(LineageFlowContext)
}
