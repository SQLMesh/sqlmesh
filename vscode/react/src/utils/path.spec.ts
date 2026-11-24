// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest'
import { findModelsByPath } from './path'

describe('findModelsByPath', () => {
  it.each([
    ['C:\\project\\Model.sql', 'c:/PROJECT/model.sql'],
    ['C:/project\\Model.sql', 'c:\\project/MODEL.SQL'],
    ['\\\\server\\share\\Model.sql', '//SERVER/share/model.sql'],
    ['C:\\space ü\\a%20#b.sql', 'c:/SPACE Ü/a%20#b.sql'],
    ['\\\\?\\C:\\Model.sql', '//?/c:/model.sql'],
  ])('matches Windows path %s against %s', (full_path, filePath) => {
    const model = { full_path }
    expect(findModelsByPath([model], filePath, true)).toEqual([model])
  })

  it.each([
    ['C:/model.sql', 'D:/model.sql'],
    ['//server/share/model.sql', '//server/other/model.sql'],
    ['//server/share/model.sql', '//other/share/model.sql'],
    ['C:/a%20b.sql', 'c:/a b.sql'],
    ['C:/a#b.sql', 'c:/a.sql'],
    ['\\\\?\\C:\\model.sql', 'C:/model.sql'],
  ])('keeps distinct Windows paths %s and %s', (full_path, filePath) => {
    expect(findModelsByPath([{ full_path }], filePath, true)).toEqual([])
  })

  it.each([false, undefined])(
    'preserves POSIX paths with host flag %s',
    isWindows => {
      expect(
        findModelsByPath(
          [{ full_path: '/model.sql' }],
          '/MODEL.sql',
          isWindows,
        ),
      ).toEqual([])
      expect(
        findModelsByPath([{ full_path: '/a\\b.sql' }], '/a/b.sql', isWindows),
      ).toEqual([])
      const model = { full_path: '/a\\b.sql' }
      expect(findModelsByPath([model], '/a\\b.sql', isWindows)).toEqual([model])
    },
  )

  it('prefers an exact match over a case-folded candidate', () => {
    const models = [{ full_path: 'C:/A.sql' }, { full_path: 'c:/a.sql' }]
    expect(findModelsByPath(models, 'c:/a.sql', true)).toEqual([models[1]])
  })

  it('returns every ambiguous fallback candidate for the caller to retain selection', () => {
    const models = [{ full_path: 'C:/A.sql' }, { full_path: 'c:/a.sql' }]
    expect(findModelsByPath(models, 'C:/a.SQL', true)).toEqual(models)
  })

  it('skips missing and empty paths', () => {
    const models = [
      {},
      { full_path: undefined },
      { full_path: null },
      { full_path: '' },
    ]
    expect(findModelsByPath(models, '', true)).toEqual([])
    expect(findModelsByPath(models, undefined, true)).toEqual([])
    expect(findModelsByPath(models, 'C:/a.sql', true)).toEqual([])
    expect(findModelsByPath([], 'C:/a.sql', true)).toEqual([])
  })
})
