import { type MouseEvent } from 'react'
import { Handle, Position } from 'reactflow'
import 'reactflow/dist/base.css'
import { getModelNodeTypeTitle } from './help'
import { isNotNil, toID, truncate } from '../../../utils'
import { EnumSide } from '~/types/enum'
import { ArrowRightCircleIcon } from '@heroicons/react/24/solid'
import clsx from 'clsx'
import { type LineageNodeModelType } from './ModelNode'

export default function ModelNodeHeaderHandles({
  id,
  className,
  hasLeft = false,
  hasRight = false,
  isSelected = false,
  isDraggable = false,
  label,
  type,
  count,
  handleClick,
  handleSelect,
}: {
  id: string
  label: string
  type?: LineageNodeModelType
  hasLeft?: boolean
  hasRight?: boolean
  count?: number
  className?: string
  isSelected?: boolean
  isDraggable?: boolean
  handleClick?: (e: MouseEvent) => void
  handleSelect?: (e: MouseEvent) => void
}): JSX.Element {
  return (
    <div className={clsx('flex w-full relative items-center', className)}>
      {hasLeft && (
        <Handle
          type="target"
          id={toID(EnumSide.Left, id)}
          position={Position.Left}
          isConnectable={false}
          className="-ml-2 border rounded-full overflow-hidden border-current"
        >
          <ArrowRightCircleIcon className="w-5 text-light dark:text-dark-lighter" />
        </Handle>
      )}
      <div
        className={clsx(
          'w-full flex items-center',
          hasLeft ? 'pl-3' : 'pl-1',
          hasRight ? 'pr-3' : 'pr-1',
        )}
      >
        {isNotNil(handleSelect) && (
          <span
            onClick={handleSelect}
            className="mx-2 w-4 h-4 rounded-full cursor-pointer p-0.5 border-2 border-current"
          >
            <span
              className={clsx(
                'flex w-2 h-2 rounded-full',
                isSelected ? 'bg-current' : 'bg-neutral-10',
              )}
            ></span>
          </span>
        )}
        <span
          className={clsx(
            'flex w-full overflow-hidden py-2',
            isDraggable && 'drag-handle',
          )}
        >
          {isNotNil(type) && (
            <span className="inline-block ml-1 mr-2 px-1 rounded-[0.25rem] text-[0.5rem] bg-neutral-10">
              {getModelNodeTypeTitle(type)}
            </span>
          )}
          <span
            title={decodeURI(label)}
            className={clsx(
              'inline-block whitespace-nowrap overflow-hidden overflow-ellipsis pr-2 font-black',
              isNotNil(handleClick) && 'cursor-pointer hover:underline',
            )}
            onClick={handleClick}
          >
            {decodeURI(label)}
          </span>
          {isNotNil(count) && (
            <span className="flex justify-between ml-2 mr-1 px-2 rounded-full bg-neutral-10">
              {count}
            </span>
          )}
        </span>
      </div>
      {hasRight && (
        <Handle
          type="source"
          id={toID(EnumSide.Right, id)}
          position={Position.Right}
          isConnectable={false}
          className="-mr-2 border rounded-full overflow-hidden border-current"
        >
          <ArrowRightCircleIcon className="w-5 text-light dark:text-dark-lighter" />
        </Handle>
      )}
    </div>
  )
}
