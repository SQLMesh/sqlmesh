import { Divider } from '@components/divider/Divider'
import { useStoreContext } from '@context/context'
import ModuleNavigation from '@components/moduleNavigation/ModuleNavigation'
import Navigation from './Navigation'
import Container from '@components/container/Container'
import { Outlet, useLocation, useNavigate } from 'react-router'
import {
  EnumErrorKey,
  type ErrorIDE,
  useNotificationCenter,
} from './context/notificationCenter'
import { type Tests, useStoreProject } from '@context/project'
import {
  useApiEnvironments,
  useApiFiles,
  useApiMeta,
  useApiModels,
  useApiPlanRun,
} from '@api/index'
import {
  type Directory,
  Status,
  type Model,
  type Environments,
  type EnvironmentsEnvironments,
} from '@api/client'
import { EnumRoutes } from '~/routes'
import { useCallback, useEffect } from 'react'
import ModalConfirmation, {
  type Confirmation,
} from '@components/modal/ModalConfirmation'
import {
  isArrayEmpty,
  isFalse,
  isFalseOrNil,
  isNil,
  isNotNil,
  isObjectNotEmpty,
  isTrue,
} from '@utils/index'
import { Button } from '@components/button/Button'
import { type FormatFileStatus, ModelFile } from '@models/file'
import { createLocalFile, useStoreEditor } from '@context/editor'
import { ModelDirectory } from '@models/directory'
import {
  EnumFileExplorerChange,
  type FileExplorerChange,
} from '@components/fileExplorer/context'
import { useStorePlan } from '@context/plan'
import { type PlanOverviewTracker } from '@models/tracker-plan-overview'
import { type PlanApplyTracker } from '@models/tracker-plan-apply'
import { type PlanCancelTracker } from '@models/tracker-plan-cancel'
import { type EnvironmentName } from '@models/environment'
import { EnumPlanAction, ModelPlanAction } from '@models/plan-action'
import { useChannelEvents, type EventSourceChannel } from '@api/channels'

export default function Root({
  content,
}: {
  content?: React.ReactNode
}): JSX.Element {
  const location = useLocation()
  const navigate = useNavigate()

  const { removeError, addError } = useNotificationCenter()

  const modules = useStoreContext(s => s.modules)
  const environment = useStoreContext(s => s.environment)
  const environments = useStoreContext(s => s.environments)
  const showConfirmation = useStoreContext(s => s.showConfirmation)
  const confirmations = useStoreContext(s => s.confirmations)
  const setShowConfirmation = useStoreContext(s => s.setShowConfirmation)
  const removeConfirmation = useStoreContext(s => s.removeConfirmation)
  const setModels = useStoreContext(s => s.setModels)
  const addRemoteEnvironments = useStoreContext(s => s.addRemoteEnvironments)
  const addLocalEnvironment = useStoreContext(s => s.addLocalEnvironment)
  const setEnvironment = useStoreContext(s => s.setEnvironment)
  let channel: EventSourceChannel

  if (isFalse(modules.hasOnlyDataCatalog)) {
    channel = useChannelEvents()
  }

  const planApply = useStorePlan(s => s.planApply)
  const planOverview = useStorePlan(s => s.planOverview)
  const planCancel = useStorePlan(s => s.planCancel)
  const setPlanApply = useStorePlan(s => s.setPlanApply)
  const setPlanOverview = useStorePlan(s => s.setPlanOverview)
  const setPlanCancel = useStorePlan(s => s.setPlanCancel)
  const setPlanAction = useStorePlan(s => s.setPlanAction)

  const files = useStoreProject(s => s.files)
  const selectedFile = useStoreProject(s => s.selectedFile)
  const setProject = useStoreProject(s => s.setProject)
  const setFiles = useStoreProject(s => s.setFiles)
  const refreshFiles = useStoreProject(s => s.refreshFiles)
  const findArtifactByPath = useStoreProject(s => s.findArtifactByPath)
  const setTests = useStoreProject(s => s.setTests)
  const setActiveRange = useStoreProject(s => s.setActiveRange)

  const storedTabs = useStoreEditor(s => s.storedTabs)
  const storedTabId = useStoreEditor(s => s.storedTabId)
  const selectTab = useStoreEditor(s => s.selectTab)
  const createTab = useStoreEditor(s => s.createTab)
  const addTabs = useStoreEditor(s => s.addTabs)
  const closeTab = useStoreEditor(s => s.closeTab)
  const inTabs = useStoreEditor(s => s.inTabs)
  const setVersion = useStoreContext(s => s.setVersion)
  const setNodeColors = useStoreContext(s => s.setNodeColors)

  const { refetch: getMeta, cancel: cancelRequestMeta } = useApiMeta()
  const { refetch: getModels, cancel: cancelRequestModels } = useApiModels()
  const { refetch: getFiles, cancel: cancelRequestFile } = useApiFiles()
  const { refetch: getEnvironments, cancel: cancelRequestEnvironments } =
    useApiEnvironments()
  const { refetch: getPlan, cancel: cancelRequestPlan } = useApiPlanRun(
    environment.name,
    {
      planOptions: { skip_tests: true, include_unmodified: true },
    },
  )

  const synchronizeEnvironment = useCallback(
    function synchronizeEnvironment(env: EnvironmentName): void {
      const found = Array.from(environments).find(e => e.name === env)

      if (isNil(found)) {
        addLocalEnvironment(env)
      } else {
        setEnvironment(found)
      }
    },
    [environments],
  )

  const updatePlanOverviewTracker = useCallback(
    function updatePlanOverviewTracker(data: PlanOverviewTracker): void {
      planOverview.update(data)

      if (data.environment !== environment.name) {
        synchronizeEnvironment(data.environment)
      }

      setPlanOverview(planOverview)
    },
    [synchronizeEnvironment, environment],
  )

  const updatePlanApplyTracker = useCallback(
    function updatePlanApplyTracker(data: PlanApplyTracker): void {
      planApply.update(data, planOverview)

      if (data.environment !== environment.name) {
        synchronizeEnvironment(data.environment)
      }

      setPlanApply(planApply)

      const isFinished =
        isTrue(data.meta?.done) && data.meta?.status !== Status.init

      if (planCancel.isFinished || planCancel.isRunning || isFalse(isFinished))
        return

      void getEnvironments().then(({ data }) => {
        void getPlan()

        updateEnviroments(data)
      })
    },
    [planCancel, planOverview, synchronizeEnvironment, environment],
  )

  const updatePlanCancelTracker = useCallback(
    function updatePlanCancelTracker(data: PlanCancelTracker): void {
      planCancel.update(data)

      if (data.environment !== environment.name) {
        synchronizeEnvironment(data.environment)
      }

      setPlanCancel(planCancel)

      if (isFalseOrNil(data.meta?.done) || data.meta?.status === Status.init)
        return

      // Sync backfills after plan apply canceled
      void getPlan()
    },
    [synchronizeEnvironment, environment],
  )

  const updateFiles = useCallback(
    function updateFiles({
      changes,
      directories,
    }: {
      changes: Array<{
        change: FileExplorerChange
        path: string
        file: ModelFile
      }>
      directories: Record<string, Directory>
    }): void {
      changes.sort((a: any) =>
        a.change === EnumFileExplorerChange.Deleted ? -1 : 1,
      )

      changes.forEach(({ change, path, file }) => {
        if (change === EnumFileExplorerChange.Modified) {
          const currentFile = files.get(path)

          if (isNil(currentFile) || isNil(file)) return

          currentFile.update(file)
        }

        if (change === EnumFileExplorerChange.Deleted) {
          const artifact = findArtifactByPath(path) ?? files.get(path)

          if (isNil(artifact)) return

          if (artifact instanceof ModelDirectory) {
            artifact.parent?.removeDirectory(artifact)
          }

          if (artifact instanceof ModelFile) {
            artifact.parent?.removeFile(artifact)

            if (inTabs(artifact)) {
              closeTab(artifact)
            }
          }
        }
      })

      for (const path in directories) {
        const directory = directories[path]!

        const currentDirectory = findArtifactByPath(
          path,
        ) as Optional<ModelDirectory>

        if (isNil(currentDirectory)) continue

        directory.directories?.forEach(d => {
          const directory = findArtifactByPath(
            d.path,
          ) as Optional<ModelDirectory>

          if (isNil(directory)) {
            currentDirectory.addDirectory(
              new ModelDirectory(d, currentDirectory),
            )
          }
        })

        directory.files?.forEach(f => {
          const file = files.get(f.path)

          if (isNil(file)) {
            currentDirectory.addFile(new ModelFile(f, currentDirectory))
          }
        })

        currentDirectory.directories.sort((a, b) => (a.name > b.name ? 1 : -1))
        currentDirectory.files.sort((a, b) => (a.name > b.name ? 1 : -1))
      }

      refreshFiles()
      setActiveRange()

      void getModels().then(({ data }) => updateModels(data as Model[]))
    },
    [files],
  )

  const formatFile = useCallback(
    function formatFile(formatFileStatus: FormatFileStatus): void {
      const file = files.get(formatFileStatus.path)

      if (isNotNil(file)) {
        file.isFormatted = formatFileStatus.status === Status.success

        refreshFiles()
      }
    },
    [files],
  )

  const restoreEditorTabsFromSaved = useCallback(
    function restoreEditorTabsFromSaved(files: ModelFile[]): void {
      if (isArrayEmpty(storedTabs)) return

      const tabs = storedTabs.map(({ id, content }) => {
        const file = files.find(file => file.id === id) ?? createLocalFile(id)
        const storedTab = createTab(file)

        storedTab.file.content = content ?? storedTab.file.content ?? ''

        return storedTab
      })
      const tab = tabs.find(tab => tab.file.id === storedTabId)

      addTabs(tabs)

      if (isNotNil(tab) && isNil(selectedFile)) {
        selectTab(tab)
      }
    },
    [selectTab],
  )

  useEffect(() => {
    void getMeta().then(({ data }) => {
      setVersion(data?.version)
      setNodeColors(data?.node_colors ?? {})

      if (isTrue(data?.has_running_task)) {
        setPlanAction(
          new ModelPlanAction({ value: EnumPlanAction.RunningTask }),
        )
      }
    })

    void getModels().then(({ data }) => updateModels(data as Model[]))

    const channelErrors = channel?.('errors', displayErrors)
    const channelTests = channel?.('tests', updateTests)
    const channelModels = channel?.('models', updateModels)

    channelModels?.subscribe()

    if (modules.hasPlans) {
      channelTests?.subscribe()
      void getEnvironments().then(({ data }) => updateEnviroments(data))
    }

    if (modules.hasErrors) {
      channelErrors?.subscribe()
    }

    if (modules.hasFiles) {
      void getFiles().then(({ data }) => {
        if (isNil(data)) return

        const project = new ModelDirectory(data)
        const files = project.allFiles

        restoreEditorTabsFromSaved(files)
        setFiles(files)
        setProject(project)
      })
    }

    return () => {
      void cancelRequestMeta()
      void cancelRequestModels()

      channelModels?.unsubscribe()

      if (modules.hasPlans) {
        cancelRequestEnvironments()
        cancelRequestPlan()
      }

      if (modules.hasErrors) {
        channelErrors?.unsubscribe()
      }

      if (modules.hasTests) {
        channelTests?.unsubscribe()
      }

      if (modules.hasFiles) {
        cancelRequestFile()
      }
    }
  }, [modules])

  useEffect(() => {
    const channelPlanApply = channel?.('plan-apply', updatePlanApplyTracker)

    if (modules.hasPlans) {
      channelPlanApply?.subscribe()
    }

    return () => {
      if (modules.hasPlans) {
        channelPlanApply?.unsubscribe()
      }
    }
  }, [updatePlanApplyTracker, modules])

  useEffect(() => {
    const channelPlanOverview = channel?.(
      'plan-overview',
      updatePlanOverviewTracker,
    )

    if (modules.hasPlans) {
      channelPlanOverview?.subscribe()
    }

    return () => {
      if (modules.hasPlans) {
        channelPlanOverview?.unsubscribe()
      }
    }
  }, [updatePlanOverviewTracker, modules])

  useEffect(() => {
    const channelPlanCancel = channel?.('plan-cancel', updatePlanCancelTracker)

    if (modules.hasPlans) {
      channelPlanCancel?.subscribe()
    }

    return () => {
      if (modules.hasPlans) {
        channelPlanCancel?.unsubscribe()
      }
    }
  }, [updatePlanCancelTracker, modules])

  useEffect(() => {
    const channelFile = channel?.('file', updateFiles)

    channelFile?.subscribe()

    return () => {
      channelFile?.unsubscribe()
    }
  }, [updateFiles])

  useEffect(() => {
    const channelFormatFile = channel?.('format-file', formatFile)

    channelFormatFile?.subscribe()

    return () => {
      channelFormatFile?.unsubscribe()
    }
  }, [formatFile])

  useEffect(() => {
    if (
      (isFalse(modules.isEmpty) && location.pathname === EnumRoutes.Home) ||
      location.pathname === ''
    ) {
      navigate(modules.defaultNavigationRoute(), { replace: true })
    }
  }, [location, modules])

  useEffect(() => {
    setShowConfirmation(confirmations.length > 0)
  }, [confirmations])

  function updateModels(models?: Model[]): void {
    if (isNotNil(models)) {
      removeError(EnumErrorKey.Models)
      setModels(models)
    }
  }

  function displayErrors(data: ErrorIDE): void {
    addError(EnumErrorKey.General, data)
  }

  function updateTests(tests?: Tests): void {
    setTests(tests)
  }

  function updateEnviroments(data: Optional<Environments>): void {
    const { environments, default_target_environment, pinned_environments } =
      data ?? {}

    if (isObjectNotEmpty<EnvironmentsEnvironments>(environments)) {
      addRemoteEnvironments(
        Object.values(environments),
        default_target_environment,
        pinned_environments,
      )
    }
  }

  function closeModalConfirmation(confirmation?: Confirmation): void {
    confirmation?.cancel?.()

    setShowConfirmation(false)
  }

  const confirmation = confirmations[0]

  return (
    <Container.Page layout="horizontal">
      {modules.showModuleNavigation && (
        <>
          <ModuleNavigation className="overflow-hidden py-1 px-2 flex flex-col items-center" />
          <Divider orientation="vertical" />
        </>
      )}
      <div className="flex flex-col overflow-hidden w-full h-full">
        {modules.showNavigation && (
          <>
            <Navigation />
            <Divider />
          </>
        )}
        {content ?? <Outlet />}
        <ModalConfirmation
          show={showConfirmation}
          onClose={() => {
            closeModalConfirmation(confirmation)
          }}
          afterLeave={() => {
            removeConfirmation()
          }}
          onKeyDown={(e: React.KeyboardEvent) => {
            if (e.key === 'Escape') {
              closeModalConfirmation(confirmation)
            }
          }}
        >
          <ModalConfirmation.Main>
            {isNotNil(confirmation?.headline) && (
              <ModalConfirmation.Headline>
                {confirmation?.headline}
              </ModalConfirmation.Headline>
            )}
            {isNotNil(confirmation?.tagline) && (
              <ModalConfirmation.Tagline>
                {confirmation?.tagline}
              </ModalConfirmation.Tagline>
            )}
            {isNotNil(confirmation?.details) && (
              <ModalConfirmation.Details details={confirmation?.details} />
            )}
            {isNotNil(confirmation?.description) && (
              <ModalConfirmation.Description>
                {confirmation?.description}
              </ModalConfirmation.Description>
            )}
            {confirmation?.children}
          </ModalConfirmation.Main>
          <ModalConfirmation.Actions>
            <Button
              className="font-bold"
              size="md"
              variant="danger"
              onClick={(e: React.MouseEvent) => {
                e.stopPropagation()

                confirmation?.action?.()
                setShowConfirmation(false)
              }}
            >
              {confirmation?.yesText ?? 'Confirm'}
            </Button>
            <Button
              size="md"
              variant="alternative"
              onClick={(e: React.MouseEvent) => {
                e.stopPropagation()

                closeModalConfirmation(confirmation)
              }}
            >
              {confirmation?.noText ?? 'Cancel'}
            </Button>
          </ModalConfirmation.Actions>
        </ModalConfirmation>
      </div>
    </Container.Page>
  )
}
