import { describe, it, expect } from 'vitest'
import { render } from '../../../../tests/utils'
import { HighlightedText } from '../SearchList'
import { highlightMatch } from '../help'

describe('highlightMatch', () => {
  it('should split source into matched and unmatched parts', () => {
    expect(highlightMatch('abcab', 'ab')).toEqual([
      { text: 'ab', isMatch: true },
      { text: 'c', isMatch: false },
      { text: 'ab', isMatch: true },
    ])
  })

  it('should handle adjacent matches and matches at the edges', () => {
    expect(highlightMatch('abab', 'ab')).toEqual([
      { text: 'ab', isMatch: true },
      { text: 'ab', isMatch: true },
    ])
    expect(highlightMatch('xab', 'ab')).toEqual([
      { text: 'x', isMatch: false },
      { text: 'ab', isMatch: true },
    ])
  })

  it('should return no parts for an empty source', () => {
    expect(highlightMatch('', 'ab')).toEqual([])
  })

  it('should return the whole source when there is no match', () => {
    expect(highlightMatch('abc', '')).toEqual([{ text: 'abc', isMatch: false }])
    expect(highlightMatch('abc', 'x')).toEqual([
      { text: 'abc', isMatch: false },
    ])
  })
})

describe('HighlightedText', () => {
  it('should render project text as text, not HTML', () => {
    const source = '<img src=x onerror="alert(1)">model'
    const { container } = render(
      <HighlightedText
        source={source}
        search="model"
      />,
    )

    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toBe(source)
    expect(container.querySelector('b')?.textContent).toBe('model')
  })

  it('should not interpret markup in the search string', () => {
    const search = '<img src=x onerror="alert(1)">'
    const { container } = render(
      <HighlightedText
        source={`a${search}b`}
        search={search}
      />,
    )

    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('b')?.textContent).toBe(search)
  })
})
