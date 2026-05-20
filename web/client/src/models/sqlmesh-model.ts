import {
  type Column,
  type ModelDetails,
  type Model,
  type ModelDescription,
  type ModelSql,
  ModelType,
  type ModelDefaultCatalog,
  type ModelDefinition,
} from '@api/client'
import { type Lineage } from '@context/editor'
import { ModelInitial } from './initial'
import { isArrayNotEmpty } from '@utils/index'

export interface InitialSQLMeshModel extends Model {
  lineage?: Record<string, Lineage>
}

export class ModelSQLMeshModel<
  T extends InitialSQLMeshModel = InitialSQLMeshModel,
> extends ModelInitial<T> {
  _details: ModelDetails = {}
  _detailsIndex: string = ''

  name: string
  fqn: string
  path: string = ''
  full_path: string = ''
  dialect: string
  type: ModelType
  columns: Column[]
  default_catalog?: ModelDefaultCatalog
  description?: ModelDescription
  sql?: ModelSql
  definition?: ModelDefinition
  hash: string

  constructor(initial?: T | ModelSQLMeshModel) {
    super(
      (initial as ModelSQLMeshModel<T>)?.isModel
        ? (initial as ModelSQLMeshModel<T>).initial
        : {
            ...(initial as T),
            dialect: initial?.dialect ?? 'Default',
            columns: initial?.columns ?? [],
            details: initial?.details ?? {},
          },
    )

    this.name = encodeURI(this.initial.name)
    this.fqn = encodeURI(this.initial.fqn)
    this.default_catalog = this.initial.default_catalog
    this.path = this.initial.path ?? ''
    this.full_path = this.initial.full_path ?? ''
    this.dialect = this.initial.dialect
    this.description = this.initial.description
    this.sql = this.initial.sql
    this.definition = this.initial.definition
    this.columns = this.initial.columns ?? []
    this.type = this.initial.type
    this.hash = this.initial.hash
    this.details = this.initial.details ?? {}
  }

  get defaultCatalog(): Optional<ModelDefaultCatalog> {
    return this.default_catalog
  }

  get details(): ModelDetails {
    return this._details
  }

  set details(details: ModelDetails) {
    const output = []

    for (const value of Object.values(details)) {
      if (isArrayNotEmpty(value)) {
        value.forEach(v => {
          output.push(...Object.values(v))
        })
      } else {
        output.push(value)
      }
    }

    this._details = details
    this._detailsIndex = output.join(' ')
  }

  get index(): string {
    return [
      this.displayName,
      this.path,
      this.type,
      ...this.columns.map(column => Object.values(column)).flat(),
      this._detailsIndex,
      this.dialect,
      this.description,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
  }

  get isModelPython(): boolean {
    return this.type === ModelType.python
  }

  get isModelSQL(): boolean {
    return this.type === ModelType.sql
  }

  get isModelSeed(): boolean {
    return this.type === ModelType.seed
  }

  get isModelExternal(): boolean {
    return this.type === ModelType.external
  }

  get displayName(): string {
    return decodeURI(this.name)
  }

  update(initial: Partial<InitialSQLMeshModel> = {}): void {
    for (const [key, value] of Object.entries(initial)) {
      if (key === 'columns') {
        this.columns = value as Column[]
      } else if (key === 'details') {
        this.details = value as ModelDetails
      } else if (key === 'name') {
        this.name = encodeURI(value as string)
      } else if (key === 'fqn') {
        this.fqn = encodeURI(value as string)
      } else if (key === 'type') {
        this.type = value as ModelType
      } else if (key === 'default_catalog') {
        this.default_catalog = value as ModelDefaultCatalog
      } else if (key === 'description') {
        this.description = value as ModelDescription
      } else if (key in this) {
        this[key as 'path' | 'dialect' | 'sql'] = value as string
      }
    }
  }
}
