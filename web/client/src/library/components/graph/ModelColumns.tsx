import React, {
  useEffect,
  useMemo,
  useState,
  useCallback,
  Fragment,
  useRef,
} from 'react'
import { Handle, Position, useUpdateNodeInternals } from 'reactflow'
import 'reactflow/dist/base.css'
import { mergeLineageWithColumns, mergeConnections } from './help'
import {
  debounceSync,
  isArrayNotEmpty,
  isFalse,
  isNil,
  isNotNil,
  toID,
  truncate,
} from '../../../utils'
import { EnumSide, type Side } from '~/types/enum'
import { NoSymbolIcon } from '@heroicons/react/24/solid'
import {
  InformationCircleIcon,
  ClockIcon,
  ExclamationCircleIcon,
} from '@heroicons/react/24/outline'
import clsx from 'clsx'
import {
  type ColumnDescription,
  type Column,
  type ColumnLineageApiLineageModelNameColumnsColumnNameGet200,
  type LineageColumn,
  type LineageColumnSource,
  type LineageColumnExpression,
} from '@api/client'
import Loading from '@components/loading/Loading'
import Spinner from '@components/logo/Spinner'
import './Graph.css'
import {
  type InitialSQLMeshModel,
  type ModelSQLMeshModel,
} from '@models/sqlmesh-model'
import { useLineageFlow } from './context'
import { Popover, Transition } from '@headlessui/react'
import { CodeEditorDefault } from '@components/editor/EditorCode'
import { EnumFileExtensions } from '@models/file'
import { useSQLMeshModelExtensions } from '@components/editor/hooks'
import { useApiColumnLineage } from '@api/index'
import { type Lineage } from '@context/editor'
import SourceList from '@components/sourceList/SourceList'

export default function ModelColumns({
  nodeId,
  columns,
  disabled,
  className,
  limit = 5,
  withHandles = false,
  withSource = false,
  withDescription = true,
  maxHeight = '50vh',
}: {
  nodeId: string
  columns: Column[]
  disabled?: boolean
  className?: string
  limit?: number
  withHandles?: boolean
  withSource?: boolean
  withDescription?: boolean
  maxHeight?: string
}): JSX.Element {
  const {
    mainNode,
    connections,
    isActiveColumn,
    setConnections,
    manuallySelectedColumn,
    setManuallySelectedColumn,
    setLineage,
    removeActiveEdges,
    addActiveEdges,
    lineage,
    lineageCache,
    setLineageCache,
  } = useLineageFlow()

  const [columnsSelected = [], columnsRest = []] = useMemo(() => {
    const active: Column[] = []
    const rest: Column[] = []

    columns.forEach(column => {
      if (isActiveColumn(nodeId, column.name)) {
        active.push(column)
      } else {
        rest.push(column)
      }
    })

    return [active, rest]
  }, [nodeId, columns, isActiveColumn])

  function updateColumnLineage(
    columnLineage: Record<string, Record<string, LineageColumn>> = {},
  ): void {
    let newLineageCache = lineageCache
    let currentConnections
    let currentLineage

    if (isNil(lineageCache)) {
      const mainNodeLineage = isNil(mainNode)
        ? undefined
        : (lineage[mainNode] ?? lineageCache?.[mainNode])

      newLineageCache = lineage
      currentConnections = new Map()
      currentLineage =
        isNil(mainNode) || isNil(mainNodeLineage)
          ? {}
          : { [mainNode]: { models: [] } }
    } else {
      currentConnections = connections
      currentLineage = structuredClone(lineage)
    }

    const { connections: newConnections, activeEdges } = mergeConnections(
      currentConnections,
      columnLineage,
    )

    if (newConnections.size === 0 && activeEdges.length === 0) {
      currentLineage = structuredClone(lineage)
      newLineageCache = undefined
    } else {
      setConnections(newConnections)
      addActiveEdges(activeEdges)
    }

    const mergedLineage = mergeLineageWithColumns(currentLineage, columnLineage)

    setLineageCache(newLineageCache)
    setLineage(mergedLineage)
  }

  const isSelectManually = useCallback(
    function isSelectManually(columnName: string): boolean {
      if (isNil(manuallySelectedColumn)) return false

      const [selectedModel, selectedColumn] = manuallySelectedColumn

      if (isNil(selectedModel) || isNil(selectedColumn)) return false

      return selectedModel.name === nodeId && selectedColumn.name === columnName
    },
    [nodeId, manuallySelectedColumn],
  )

  const removeEdges = useCallback(
    function removeEdges(columnId: string): void {
      const visited = new Set<string>()

      removeActiveEdges(
        walk(columnId, EnumSide.Left).concat(walk(columnId, EnumSide.Right)),
      )

      if (connections.size === 0 && isNotNil(lineageCache)) {
        setLineage(lineageCache)
        setLineageCache(undefined)
      }

      setConnections(connections)

      function walk(id: string, side: Side): Array<[string, string]> {
        if (visited.has(id)) return []

        const edges = connections.get(id)?.[side] ?? []

        connections.delete(id)

        visited.add(id)

        return edges
          .map(edge =>
            [
              side === EnumSide.Left
                ? [toID(EnumSide.Left, id), toID(EnumSide.Right, edge)]
                : [toID(EnumSide.Left, edge), toID(EnumSide.Right, id)],
            ].concat(walk(edge, side)),
          )
          .flat() as Array<[string, string]>
      }
    },
    [removeActiveEdges, connections],
  )

  return (
    <div className={clsx('overflow-hidden', className)}>
      {isArrayNotEmpty(columnsSelected) && (
        <div
          className={clsx(
            'w-full h-full overflow-hidden overflow-y-auto hover:scrollbar scrollbar--vertical border-b border-t border-neutral-10',
            withHandles && 'w-full cursor-default',
          )}
        >
          {columnsSelected.map(column => (
            <ModelColumn
              key={toID(nodeId, column.name)}
              id={toID(nodeId, column.name)}
              nodeId={nodeId}
              column={column}
              disabled={disabled}
              updateColumnLineage={updateColumnLineage}
              removeEdges={removeEdges}
              isActive={true}
              hasLeft={isArrayNotEmpty(
                connections.get(toID(nodeId, column.name))?.left,
              )}
              hasRight={isArrayNotEmpty(
                connections.get(toID(nodeId, column.name))?.right,
              )}
              selectManually={
                isSelectManually(column.name)
                  ? setManuallySelectedColumn
                  : undefined
              }
              withHandles={withHandles}
              withDescription={withDescription}
              expression={
                withSource
                  ? getColumnFromLineage(lineage, nodeId, column.name)
                      ?.expression
                  : undefined
              }
              source={
                withSource
                  ? getColumnFromLineage(lineage, nodeId, column.name)?.source
                  : undefined
              }
              isEmpty={
                isNotNil(getColumnFromLineage(lineage, nodeId, column.name)) &&
                Object.keys(
                  getColumnFromLineage(lineage, nodeId, column.name)?.models ??
                    {},
                ).length === 0
              }
              className="border-t border-neutral-10 first:border-0"
            />
          ))}
        </div>
      )}
      {columnsRest.length <= limit && (
        <div
          className={clsx(
            'w-full h-full overflow-hidden overflow-y-auto hover:scrollbar scrollbar--vertical py-2',
            withHandles && 'w-full cursor-default',
          )}
        >
          {columnsRest.map(column => (
            <ModelColumn
              key={toID(nodeId, column.name)}
              id={toID(nodeId, column.name)}
              nodeId={nodeId}
              column={column}
              disabled={disabled}
              updateColumnLineage={updateColumnLineage}
              removeEdges={removeEdges}
              isActive={false}
              hasLeft={false}
              hasRight={false}
              selectManually={
                isSelectManually(column.name)
                  ? setManuallySelectedColumn
                  : undefined
              }
              withHandles={withHandles}
              withDescription={withDescription}
              expression={
                withSource
                  ? getColumnFromLineage(lineage, nodeId, column.name)
                      ?.expression
                  : undefined
              }
              source={
                withSource
                  ? getColumnFromLineage(lineage, nodeId, column.name)?.source
                  : undefined
              }
              isEmpty={
                isNotNil(getColumnFromLineage(lineage, nodeId, column.name)) &&
                Object.keys(
                  getColumnFromLineage(lineage, nodeId, column.name)?.models ??
                    {},
                ).length === 0
              }
              className="border-t border-neutral-10 first:border-0 rounded-md"
            />
          ))}
        </div>
      )}
      {columnsRest.length > limit && (
        <div
          className={clsx(
            'w-full h-full overflow-hidden overflow-y-auto hover:scrollbar scrollbar--vertical pb-2',
            withHandles && 'w-full cursor-default',
          )}
          style={{ height: maxHeight }}
        >
          <SourceList<Column>
            keyId="name"
            keyName="name"
            items={columnsRest}
            withCounter={false}
            withFilter={columnsRest.length > limit}
            disabled={disabled}
            listItem={({ disabled, item }) => (
              <ModelColumn
                key={toID(nodeId, item.name)}
                id={toID(nodeId, item.name)}
                nodeId={nodeId}
                column={item}
                disabled={disabled}
                updateColumnLineage={updateColumnLineage}
                removeEdges={removeEdges}
                isActive={false}
                hasLeft={false}
                hasRight={false}
                selectManually={
                  isSelectManually(item.name)
                    ? setManuallySelectedColumn
                    : undefined
                }
                withHandles={withHandles}
                withDescription={withDescription}
                expression={
                  withSource
                    ? getColumnFromLineage(lineage, nodeId, item.name)
                        ?.expression
                    : undefined
                }
                source={
                  withSource
                    ? getColumnFromLineage(lineage, nodeId, item.name)?.source
                    : undefined
                }
                isEmpty={
                  isNotNil(getColumnFromLineage(lineage, nodeId, item.name)) &&
                  Object.keys(
                    getColumnFromLineage(lineage, nodeId, item.name)?.models ??
                      {},
                  ).length === 0
                }
                className="border-t border-neutral-10 first:border-0 rounded-md"
              />
            )}
          />
        </div>
      )}
    </div>
  )
}

function ModelColumn({
  id,
  nodeId,
  column,
  className,
  disabled = false,
  isActive = false,
  hasLeft = false,
  hasRight = false,
  isEmpty = false,
  updateColumnLineage,
  removeEdges,
  selectManually,
  withHandles = false,
  withDescription = true,
  source,
  expression,
}: {
  id: string
  nodeId: string
  column: Column
  disabled?: boolean
  isActive?: boolean
  hasLeft?: boolean
  hasRight?: boolean
  isEmpty?: boolean
  withHandles?: boolean
  source?: LineageColumnSource
  expression?: LineageColumnExpression
  withDescription?: boolean
  updateColumnLineage: (
    lineage: ColumnLineageApiLineageModelNameColumnsColumnNameGet200,
  ) => void
  removeEdges: (columnId: string) => void
  selectManually?: React.Dispatch<
    React.SetStateAction<
      [ModelSQLMeshModel<InitialSQLMeshModel>, Column] | undefined
    >
  >
  className?: string
}): JSX.Element {
  const {
    refetch: getColumnLineage,
    isFetching,
    isError,
    isTimeout,
  } = useApiColumnLineage(nodeId, column.name)

  useEffect(() => {
    if (isNil(selectManually)) return

    toggleColumnLineage()
    selectManually(undefined)
  }, [selectManually])

  function toggleColumnLineage(): void {
    if (disabled) return

    if (isActive) {
      removeEdges(id)
    } else {
      void getColumnLineage().then(({ data }) =>
        updateColumnLineage(data ?? {}),
      )
    }
  }

  const showHandles = withHandles && (hasLeft || hasRight)

  return (
    <div
      className={clsx(
        'transition-colors duration-200 cursor-pointer',
        disabled ? 'cursor-not-allowed' : 'hover:bg-neutral-5',
        isActive && 'bg-neutral-5',
        showHandles ? 'px-0 py-0.5' : 'px-2 py-0.5',
        className,
      )}
      onClick={debounceSync(toggleColumnLineage, 500, true)}
    >
      <div className="flex w-full items-center relative">
        {showHandles ? (
          <ColumnHandles
            id={id}
            nodeId={nodeId}
            hasLeft={hasLeft}
            hasRight={hasRight}
            disabled={disabled}
            className="px-2"
          >
            {isNotNil(source) && (
              <ColumnSource
                source={source}
                expression={expression}
              />
            )}
            <ColumnStatus
              isFetching={isFetching}
              isError={isError}
              isTimeout={isTimeout}
            />
            <ColumnDisplay
              columnName={column.name}
              columnType={column.type}
              columnDescription={column.description}
              disabled={disabled}
              withDescription={withDescription}
              className={clsx(
                isError
                  ? 'text-danger-500'
                  : isTimeout
                    ? 'text-warning-500'
                    : isEmpty
                      ? 'text-neutral-400 dark:text-neutral-600'
                      : 'text-prose',
              )}
            />
          </ColumnHandles>
        ) : (
          <>
            <ColumnStatus
              isFetching={isFetching}
              isError={isError}
              isTimeout={isTimeout}
            />
            <ColumnDisplay
              columnName={column.name}
              columnType={column.type}
              columnDescription={column.description}
              disabled={disabled}
              withDescription={withDescription}
              className={clsx(
                isError
                  ? 'text-danger-500'
                  : isTimeout
                    ? 'text-warning-500'
                    : isEmpty
                      ? 'text-neutral-400 dark:text-neutral-600'
                      : 'text-prose',
              )}
            />
          </>
        )}
      </div>
    </div>
  )
}

function ColumnHandles({
  nodeId,
  id,
  hasLeft = false,
  hasRight = false,
  disabled = false,
  children,
  className,
}: {
  nodeId: string
  id: string
  children: React.ReactNode
  className?: string
  hasLeft?: boolean
  hasRight?: boolean
  disabled?: boolean
}): JSX.Element {
  const updateNodeInternals = useUpdateNodeInternals()

  useEffect(() => {
    // TODO: This is a hack to fix the issue where the handles are not rendered yet
    setTimeout(() => {
      updateNodeInternals(nodeId)
    }, 100)
  }, [hasLeft, hasRight])

  return (
    <div
      className={clsx(
        'flex w-full items-center',
        disabled ? 'cursor-not-allowed' : 'inherit',
        className,
      )}
    >
      {hasLeft && (
        <Handle
          type="target"
          id={toID(EnumSide.Left, id)}
          position={Position.Left}
          isConnectable={false}
          className="w-2 h-2 rounded-full"
        />
      )}
      {children}
      {hasRight && (
        <Handle
          type="source"
          id={toID(EnumSide.Right, id)}
          position={Position.Right}
          isConnectable={false}
          className="w-2 h-2 rounded-full"
        />
      )}
    </div>
  )
}

function ColumnDisplay({
  columnName,
  columnType,
  columnDescription,
  className,
  disabled = false,
  withDescription = true,
}: {
  columnName: string
  columnType: string
  columnDescription?: ColumnDescription
  disabled?: boolean
  withDescription?: boolean
  className?: string
}): JSX.Element {
  let decodedColumnName = columnName

  try {
    decodedColumnName = decodeURI(columnName)
  } catch {}

  return (
    <div
      className={clsx(
        'block w-full',
        disabled ? 'cursor-not-allowed' : 'inherit',
        className,
      )}
    >
      <div className="w-full flex justify-between items-center">
        <span
          title={decodedColumnName}
          className={clsx('flex items-center', disabled && 'opacity-50')}
        >
          {disabled && (
            <NoSymbolIcon
              title="No column level lineage for Python models"
              className="w-3 h-3 mr-2"
            />
          )}
          {truncate(decodedColumnName, 50, 20)}
        </span>
        <span
          title={columnType}
          className="inline-block ml-2 text-[0.6rem] font-black opacity-60"
        >
          {truncate(columnType, 20, 10)}
        </span>
      </div>
      {isNotNil(columnDescription) && withDescription && (
        <p className="block opacity-60 text-[0.65rem]">{columnDescription}</p>
      )}
    </div>
  )
}

function ColumnStatus({
  isFetching = false,
  isError = false,
  isTimeout = false,
}: {
  isFetching: boolean
  isError: boolean
  isTimeout: boolean
}): JSX.Element {
  return (
    <>
      {isFetching && (
        <Loading className="inline-block mr-1">
          <Spinner className="w-3 h-3 border border-neutral-10" />
        </Loading>
      )}
      {isTimeout && isFalse(isFetching) && (
        <ClockIcon className="w-4 h-4 text-warning-500 mr-1" />
      )}
      {isError && isFalse(isFetching) && (
        <ExclamationCircleIcon className="w-4 h-4 text-danger-500 mr-1" />
      )}
    </>
  )
}

function ColumnSource({
  source,
  expression,
}: {
  source: LineageColumnSource
  expression?: LineageColumnExpression
}): JSX.Element {
  const elSourceContainer = useRef<HTMLDivElement>(null)
  const { handleClickModel } = useLineageFlow()

  const modelExtensions = useSQLMeshModelExtensions(undefined, model => {
    handleClickModel?.(model.name)
  })

  const [isShowing, setIsShowing] = useState(false)

  useEffect(() => {
    if (isShowing) {
      scrollToExpression()
    }
  }, [isShowing, expression])

  function scrollToExpression(): void {
    // NOTE: This is a hack to scroll to the expression
    // and should be replaced with a code mirror extension
    setTimeout(() => {
      const lines = Array.from(
        elSourceContainer.current?.querySelector('[role="textbox"].cm-content')
          ?.children ?? [],
      )

      for (const node of lines) {
        if (node.textContent?.trim() === expression) {
          node.scrollIntoView({
            behavior: 'smooth',
            block: 'center',
            inline: 'nearest',
          })
          setTimeout(() => node.classList.add('sqlmesh-expression'), 500)
          return
        }
      }
    }, 300)
  }

  return (
    <Popover
      onMouseLeave={() => setIsShowing(false)}
      onMouseOver={() => setIsShowing(true)}
      className="flex"
    >
      <InformationCircleIcon
        className={clsx(
          'inline-block mr-3 w-4 h-4',
          isShowing ? 'text-inherit' : 'text-prose',
        )}
      />
      <Transition
        show={isShowing}
        as={Fragment}
        enter="transition ease-out duration-200"
        enterFrom="opacity-0 -translate-y-[40%]"
        enterTo="opacity-100 -translate-y-[50%]"
        leave="transition ease-in duration-150"
        leaveFrom="opacity-100 -translate-y-[50%]"
        leaveTo="opacity-0 -translate-y-[40%]"
      >
        <Popover.Panel
          ref={elSourceContainer}
          className="fixed -translate-x-[100%] -translate-y-[50%] z-10 content transform cursor-pointer rounded-lg bg-theme border-4 border-neutral-200 dark:border-neutral-600"
          onClick={e => e.stopPropagation()}
        >
          <CodeEditorDefault
            content={source as string}
            type={EnumFileExtensions.SQL}
            className="w-full h-[25rem] text-xs rounded-lg scrollbar scrollbar--vertical scrollbar--horizontal overflow-auto max-w-[40rem]"
            extensions={modelExtensions}
          />
        </Popover.Panel>
      </Transition>
    </Popover>
  )
}

function getColumnFromLineage(
  lineage: Record<string, Lineage>,
  nodeId: string,
  columnName: string,
): Optional<LineageColumn> {
  return lineage?.[nodeId]?.columns?.[encodeURI(columnName)]
}
