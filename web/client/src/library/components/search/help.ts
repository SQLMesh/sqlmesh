export { filterListBy, highlightMatch }

export const EMPTY_STRING = ''
const MATCHED_LENGTH_TO_DISPLAY = 40

export interface HighlightPart {
  text: string
  isMatch: boolean
}

// NOTE: Returns plain text parts so callers can render them as React text nodes
function highlightMatch(source: string, match: string): HighlightPart[] {
  if (match === EMPTY_STRING) return [{ text: source, isMatch: false }]

  return source
    .split(match)
    .flatMap((text, idx) =>
      idx === 0
        ? [{ text, isMatch: false }]
        : [
            { text: match, isMatch: true },
            { text, isMatch: false },
          ],
    )
    .filter(part => part.text !== EMPTY_STRING)
}

// NOTE: This function is used to filter the list of models in the search bar
function filterListBy<T extends Record<string, any> = Record<string, any>>(
  indices: Array<[T, string]> = [],
  search: string,
): Array<[T, string]> {
  search = search.toLocaleLowerCase()

  return indices.reduce((acc: Array<[T, string]>, [model, index]) => {
    const idx = index.toLocaleLowerCase().indexOf(search)

    if (idx < 0) return acc

    if (index.length < MATCHED_LENGTH_TO_DISPLAY) {
      acc.push([model, index])
    } else {
      const min = Math.max(0, idx - MATCHED_LENGTH_TO_DISPLAY)
      const max = Math.min(
        index.length - 1,
        idx + search.length + MATCHED_LENGTH_TO_DISPLAY,
      )

      acc.push([
        model,
        `${min > 0 ? '...' : EMPTY_STRING}${index.slice(min, max)}${
          max < index.length ? '...' : EMPTY_STRING
        }`,
      ])
    }

    return acc
  }, [])
}
