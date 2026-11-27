// SPDX-License-Identifier: Apache-2.0

/** Exact paths take precedence; multiple Windows fallback matches are ambiguous. */
export function findModelsByPath<T extends { full_path?: string | null }>(
  models: T[],
  filePath: string | undefined,
  isWindows = false,
): T[] {
  if (!filePath) return []
  const exact = models.find(model => model.full_path === filePath)
  if (exact) return [exact]
  if (!isWindows) return []

  const normalize = (path: string) => path.replace(/\\/g, '/').toLowerCase()
  const normalizedPath = normalize(filePath)
  return models.filter(
    model => model.full_path && normalize(model.full_path) === normalizedPath,
  )
}
