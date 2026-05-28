import { Menu, MenuButton, MenuItem, MenuItems } from '@headlessui/react'
import { CheckIcon } from '@heroicons/react/24/outline'
import { CogIcon } from '@/components/graph/CogIcon'
import clsx from 'clsx'

interface SettingsControlProps {
  showColumns: boolean
  onWithColumnsChange: (value: boolean) => void
  withOnlyDirect: boolean
  onWithOnlyDirectChange: (value: boolean) => void
}

const itemClass = clsx(
  'group flex w-full items-center px-2 py-1 text-sm',
  'text-[var(--vscode-button-foreground)]',
  'hover:bg-[var(--vscode-button-background)] bg-[var(--vscode-button-hoverBackground)]',
)

export function SettingsControl({
  showColumns,
  onWithColumnsChange,
  withOnlyDirect,
  onWithOnlyDirectChange,
}: SettingsControlProps): JSX.Element {
  return (
    <Menu
      as="div"
      className="relative"
    >
      <MenuButton
        className="react-flow__controls-button"
        title="Settings"
      >
        <CogIcon
          className="h-3 w-3"
          aria-hidden="true"
        />
      </MenuButton>
      <MenuItems className="absolute bottom-0 left-full ml-2 w-56 origin-bottom-left divide-y bg-theme shadow-lg focus:outline-none z-50">
        <MenuItem
          as="button"
          className={itemClass}
          onClick={() => onWithColumnsChange(!showColumns)}
        >
          <span className="flex-1 text-left">Show Columns</span>
          {showColumns && (
            <CheckIcon
              className="h-4 w-4 text-primary-500"
              aria-hidden="true"
            />
          )}
        </MenuItem>
        <MenuItem
          as="button"
          className={itemClass}
          onClick={() => onWithOnlyDirectChange(!withOnlyDirect)}
        >
          <span className="flex-1 text-left">Only Direct Neighbors</span>
          {withOnlyDirect && (
            <CheckIcon
              className="h-4 w-4 text-primary-500"
              aria-hidden="true"
            />
          )}
        </MenuItem>
      </MenuItems>
    </Menu>
  )
}
