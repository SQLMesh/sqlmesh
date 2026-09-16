/**
 * Serialize an async task so it never overlaps with itself, collapsing any
 * calls that arrive while it is running into a single rerun.
 *
 * Restarting the language server means stopping a client and starting another.
 * Letting two of those interleave leaves commands registered by the outgoing
 * client colliding with the incoming one, and leaves requests addressed to a
 * client that has already been disposed. Queueing one run per trigger would
 * only spread the same problem out over time, so queued triggers collapse into
 * one rerun: all the caller wants is for the task to have run after its
 * request, not for it to run once per request.
 *
 * @param task The task to serialize.
 * @returns A function that resolves once the task has run for that call.
 */
export function coalesceAsync(task: () => Promise<void>): () => Promise<void> {
  let running: Promise<void> | undefined
  let queued: Promise<void> | undefined

  const start = async (): Promise<void> => {
    try {
      await task()
    } finally {
      running = undefined
    }
  }

  return (): Promise<void> => {
    if (!running) {
      running = start()
      return running
    }

    // A rerun is already scheduled, so this call is satisfied by that one.
    if (!queued) {
      queued = running
        // A failed run must not stop the rerun that was asked for; the caller
        // waiting on the failed run is the one that sees the error.
        .catch(() => undefined)
        .then(() => {
          queued = undefined
          running = start()
          return running
        })
    }

    return queued
  }
}
