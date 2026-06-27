import { type Confirmation } from '@components/modal/ModalConfirmation'
import { ModelModuleController } from '@models/module-controller'
import { ModelSQLMeshModel } from '@models/sqlmesh-model'
import { create } from 'zustand'
import { type Model, type Environment } from '~/api/client'
import {
  EnumRelativeLocation,
  type EnvironmentName,
  ModelEnvironment,
} from '~/models/environment'
import { isNil, isStringEmptyOrNil, isTrue } from '~/utils'

interface ContextStore {
  version?: string
  nodeColors: Record<string, string>
  showConfirmation: boolean
  confirmations: Confirmation[]
  environment: ModelEnvironment
  environments: Set<ModelEnvironment>
  lastSelectedModel?: ModelSQLMeshModel
  models: Map<string, ModelSQLMeshModel>
  modules: ModelModuleController
  splitPaneSizes: number[]
  setLastSelectedModel: (model?: ModelSQLMeshModel) => void
  setSplitPaneSizes: (splitPaneSizes: number[]) => void
  setModules: (modules: ModelModuleController) => void
  setNodeColors: (nodeColors: Record<string, string>) => void
  setVersion: (version?: string) => void
  setShowConfirmation: (showConfirmation: boolean) => void
  addConfirmation: (confirmation: Confirmation) => void
  removeConfirmation: () => void
  setModels: (models?: Model[]) => void
  isModel: (nameOrPath: string) => boolean
  isExistingEnvironment: (
    environment: ModelEnvironment | EnvironmentName,
  ) => boolean
  getNextEnvironment: () => ModelEnvironment
  setEnvironment: (environment: ModelEnvironment) => void
  addLocalEnvironment: (
    environment: EnvironmentName,
    created_from?: EnvironmentName,
  ) => void
  removeLocalEnvironment: (environments: ModelEnvironment) => void
  addRemoteEnvironments: (
    environments: Environment[],
    defaultEnvironment?: string,
    pinnedEnvironments?: string[],
  ) => void
  hasRemoteEnvironments: () => boolean
}

const PROD = 'prod'

const environments = new Set(ModelEnvironment.getEnvironments())
const environment =
  ModelEnvironment.getEnvironment() ?? environments.values().next().value!

export const useStoreContext = create<ContextStore>((set, get) => ({
  version: undefined,
  nodeColors: {},
  modules: new ModelModuleController(),
  splitPaneSizes: [20, 80],
  showConfirmation: false,
  confirmations: [],
  environment,
  environments,
  initialStartDate: undefined,
  initialEndDate: undefined,
  models: new Map(),
  lastSelectedModel: undefined,
  setLastSelectedModel(model) {
    set(() => ({
      lastSelectedModel: model,
    }))
  },
  setModules(modules) {
    set(() => ({
      modules: new ModelModuleController(modules.list),
    }))
  },
  isModel(nameOrPath) {
    return get().models.has(nameOrPath)
  },
  setSplitPaneSizes(splitPaneSizes) {
    set(() => ({
      splitPaneSizes,
    }))
  },
  setNodeColors(nodeColors) {
    set(() => ({
      nodeColors,
    }))
  },
  setVersion(version) {
    set(() => ({
      version,
    }))
  },
  setShowConfirmation(showConfirmation) {
    set(() => ({
      showConfirmation,
    }))
  },
  addConfirmation(confirmation) {
    set(s => {
      s.confirmations.push(confirmation)

      return {
        confirmations: Array.from(s.confirmations),
      }
    })
  },
  removeConfirmation() {
    const s = get()

    s.confirmations.shift()

    set(() => ({
      confirmations: Array.from(s.confirmations),
    }))
  },
  setModels(models = []) {
    const s = get()

    set(() => ({
      models: models.reduce((acc: Map<string, ModelSQLMeshModel>, model) => {
        const tempModel = new ModelSQLMeshModel(model)
        let maybeModel =
          s.models.get(tempModel.path) ??
          s.models.get(tempModel.name) ??
          s.models.get(tempModel.fqn)

        if (isNil(maybeModel)) {
          maybeModel = tempModel
        } else {
          maybeModel.update(model)
        }

        acc.set(maybeModel.name, maybeModel)
        acc.set(maybeModel.fqn, maybeModel)
        acc.set(maybeModel.path, maybeModel)

        return acc
      }, new Map()),
    }))
  },
  getNextEnvironment() {
    return get().environments.values().next().value ?? environment
  },
  isExistingEnvironment(environment) {
    const s = get()

    if ((environment as ModelEnvironment).isModel)
      return s.environments.has(environment as ModelEnvironment)

    let hasEnvironment = false

    s.environments.forEach(env => {
      if (env.name === (environment as EnvironmentName)) {
        hasEnvironment = true
      }
    })

    return hasEnvironment
  },
  setEnvironment(environment) {
    set(() => {
      ModelEnvironment.save({
        environment,
      })

      return {
        environment,
      }
    })
  },
  addLocalEnvironment(localEnvironment, created_from = PROD) {
    set(s => {
      if (isStringEmptyOrNil(localEnvironment)) return s

      const environment = new ModelEnvironment(
        {
          name: localEnvironment,
        },
        EnumRelativeLocation.Local,
        created_from,
      )

      s.environments.add(environment)

      s.setEnvironment(environment)

      ModelEnvironment.save({
        environments: Array.from(s.environments),
      })

      return {
        environments: new Set(s.environments),
      }
    })
  },
  removeLocalEnvironment(localEnvironment) {
    set(s => {
      s.environments.delete(localEnvironment)

      ModelEnvironment.save({
        environments: Array.from(s.environments),
      })

      return {
        environments: new Set(s.environments),
      }
    })
  },
  addRemoteEnvironments(
    remoteEnvironments = [],
    defaultEnvironment,
    pinnedEnvs = [],
  ) {
    set(s => {
      const environments = Array.from(s.environments)

      remoteEnvironments.forEach(env => {
        let environment = environments.find(
          ({ name: envNameLocal }) => env.name === envNameLocal,
        )

        if (isNil(environment)) {
          environment = new ModelEnvironment(env, EnumRelativeLocation.Remote)

          environments.push(environment)
        } else {
          environment.update(env)
          environment.setType(EnumRelativeLocation.Remote)
        }

        if (s.environment.name === env.name) {
          s.environment.update(env)
        }

        if (environment.isInitial && environment.isDefault) {
          s.setEnvironment(environment)
        }
      })

      pinnedEnvs.forEach(envName => {
        const environment = environments.find(
          ({ name: envNameLocal }) => envName === envNameLocal,
        )

        if (isNil(environment)) {
          environments.push(
            new ModelEnvironment(
              {
                name: envName,
              },
              EnumRelativeLocation.Local,
              undefined,
              true,
            ),
          )
        } else {
          environment.isPinned = true
        }
      })

      const profileEnv = ModelEnvironment.getEnvironment()
      let prodEnv: Optional<ModelEnvironment>
      let storedEnv: Optional<ModelEnvironment>
      let defaultEnv: Optional<ModelEnvironment>

      environments.forEach(env => {
        switch (env.name) {
          case PROD:
            prodEnv = env
            break
          case profileEnv?.name:
            storedEnv = env
            break
          case defaultEnvironment:
            env.isDefault = true
            defaultEnv = env
        }
      })

      const currentEnv = s.environment ?? storedEnv ?? defaultEnv
      const environment = isTrue(prodEnv?.isInitial) ? prodEnv : currentEnv

      ModelEnvironment.save({ environments, environment })
      ModelEnvironment.sort(environments)

      return {
        environment,
        environments: new Set(environments),
      }
    })
  },
  hasRemoteEnvironments() {
    const s = get()

    return Array.from(s.environments).some(
      ({ type }) => type === EnumRelativeLocation.Remote,
    )
  },
}))
