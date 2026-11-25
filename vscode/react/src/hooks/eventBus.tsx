// event-bus.tsx
import {
  createContext,
  useContext,
  useMemo,
  useRef,
  useEffect,
  type PropsWithChildren,
} from 'react'

/**
 * 1️⃣ List every event & its payload shape here
 */
export type EventMap = {
  changeFocusedFile: {
    fileUri: string
    isWindows?: boolean
  }
  savedFile: {
    fileUri: string
  }
}

/**
 * 2️⃣ Generic bus API — strongly–typed by EventMap
 */
export type EventBus<M extends Record<string, any>> = {
  emit<K extends keyof M>(type: K, payload: M[K]): void
  on<K extends keyof M>(type: K, handler: (payload: M[K]) => void): () => void // returns “unsubscribe”
}

function createEventBus<M extends Record<string, any>>(): EventBus<M> {
  const listeners = new Map<keyof M, Set<(p: any) => void>>()

  return {
    emit(type, payload) {
      listeners.get(type)?.forEach(fn => fn(payload))
    },
    on(type, handler) {
      let set = listeners.get(type)
      if (!set) {
        set = new Set()
        listeners.set(type, set)
      }
      set.add(handler)
      // remove listener on demand
      return () => set!.delete(handler)
    },
  }
}

/**
 * 3️⃣ React Context wrapper
 */
const EventBusContext = createContext<EventBus<EventMap> | null>(null)

export const EventBusProvider = ({ children }: PropsWithChildren<{}>) => {
  const bus = useMemo(() => createEventBus<EventMap>(), [])
  return (
    <EventBusContext.Provider value={bus}>{children}</EventBusContext.Provider>
  )
}

/**
 * 4️⃣ Convenience hooks
 */
export function useEventBus() {
  const bus = useContext(EventBusContext)
  if (!bus) throw new Error('useEventBus must be inside <EventBusProvider>')
  return bus
}

export function useEvent<K extends keyof EventMap>(
  type: K,
  handler: (payload: EventMap[K]) => void,
) {
  const bus = useEventBus()
  // keep latest handler ref without resubscribing each render
  const saved = useRef<typeof handler>(handler)
  useEffect(() => {
    saved.current = handler
  })
  useEffect(() => {
    const unsub = bus.on(type, payload => saved.current(payload))
    return unsub // unsubscribe on unmount
  }, [bus, type])
}
