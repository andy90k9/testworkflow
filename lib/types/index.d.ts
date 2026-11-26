import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'

export interface Config {
  python: string
  workflowDir: string
  timeoutSec: number
}

export const name: string
export const inject: string[]
export const Config: Schema<Config>
export function apply(ctx: Context, config: Config): void
