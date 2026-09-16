import { describe, expect, it } from 'vitest'
import { coalesceAsync } from './coalesceAsync'

/**
 * Let pending microtasks run. A rerun is scheduled off the promise of the run
 * that precedes it, so it starts a few hops after that run settles.
 */
const flush = () => new Promise<void>(resolve => setTimeout(resolve, 0))

/** A task that only settles when the test releases it. */
function deferredTask() {
  let release: (() => void) | undefined
  let rejectWith: ((error: Error) => void) | undefined
  let calls = 0

  const run = () => {
    calls += 1
    return new Promise<void>((resolve, reject) => {
      release = resolve
      rejectWith = reject
    })
  }

  return {
    run,
    get calls() {
      return calls
    },
    release: () => release?.(),
    reject: (error: Error) => rejectWith?.(error),
  }
}

describe('coalesceAsync', () => {
  it('runs the task immediately when idle', async () => {
    const task = deferredTask()
    const run = coalesceAsync(task.run)

    const first = run()
    expect(task.calls).toBe(1)

    task.release()
    await first
  })

  // A burst of triggers used to start a restart per event, which left clients
  // disposing and starting concurrently. See #5642.
  it('collapses every call made while running into a single rerun', async () => {
    const task = deferredTask()
    const run = coalesceAsync(task.run)

    const first = run()
    expect(task.calls).toBe(1)

    const queued = [run(), run(), run(), run()]
    expect(task.calls).toBe(1)

    task.release()
    await first
    await flush()

    // Exactly one rerun is scheduled, no matter how many calls arrived.
    expect(task.calls).toBe(2)

    task.release()
    await Promise.all(queued)
    await flush()
    expect(task.calls).toBe(2)
  })

  it('runs again for calls made after the previous run finished', async () => {
    const task = deferredTask()
    const run = coalesceAsync(task.run)

    const first = run()
    task.release()
    await first
    expect(task.calls).toBe(1)

    const second = run()
    expect(task.calls).toBe(2)
    task.release()
    await second
  })

  it('resolves the callers that were coalesced together', async () => {
    const task = deferredTask()
    const run = coalesceAsync(task.run)

    const first = run()
    const queued = [run(), run()]

    task.release()
    await first
    // The rerun has to be in flight before it can be released.
    await flush()
    task.release()

    await expect(Promise.all(queued)).resolves.toEqual([undefined, undefined])
  })

  it('surfaces a failure without wedging later calls', async () => {
    const task = deferredTask()
    const run = coalesceAsync(task.run)

    const first = run()
    task.reject(new Error('restart failed'))
    await expect(first).rejects.toThrow('restart failed')

    const second = run()
    expect(task.calls).toBe(2)
    task.release()
    await second
  })
})
