import { describe, it, expect, vi } from 'vitest'
import { MemoryRouter } from 'react-router'
import { render, fireEvent } from '../../../../tests/utils'
import SearchList from '../SearchList'

// jsdom has no ResizeObserver, which TBKResizeObserver needs to mount
vi.stubGlobal(
  'ResizeObserver',
  class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  },
)

const PAYLOAD = '<img src=x onerror="alert(1)">'

function renderSearch(
  name: string,
  search: string,
  showIndex: boolean,
): HTMLElement {
  const { container } = render(
    <MemoryRouter>
      <SearchList<{ name: string }>
        list={[{ name }]}
        searchBy="name"
        displayBy="name"
        showIndex={showIndex}
      />
    </MemoryRouter>,
  )

  fireEvent.input(container.querySelector('input')!, {
    target: { value: search },
  })

  return container.querySelector('[role="menuitem"]')!
}

describe.each([true, false])('SearchList with showIndex=%s', showIndex => {
  it('should render markup in project text as text', () => {
    const name = `${PAYLOAD}model`
    const result = renderSearch(name, 'model', showIndex)

    expect(result.querySelector('img')).toBeNull()
    expect(result.textContent).toContain(name)
    expect(result.querySelector('b')?.textContent).toBe('model')
  })

  it('should render markup in the search string as text', () => {
    const result = renderSearch(`a${PAYLOAD}b`, PAYLOAD, showIndex)

    expect(result.querySelector('img')).toBeNull()
    expect(result.querySelector('b')?.textContent).toBe(PAYLOAD)
  })

  it('should highlight every match, including adjacent and trailing ones', () => {
    const result = renderSearch('ababxab', 'ab', showIndex)

    expect(
      Array.from(result.querySelectorAll('b'), b => b.textContent),
    ).toEqual(['ab', 'ab', 'ab'])
    expect(result.textContent).toContain('ababxab')
  })
})
