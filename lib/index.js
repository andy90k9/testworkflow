import { defineTool } from '@deepseek-ai/dsh-tools'
import Schema from '@deepseek-ai/schemastery'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

export const name = 'dsh-workflow'
export const inject = ['tools']

export const Config = Schema.object({
  python: Schema.string().default('python3'),
  workflowDir: Schema.string().default(''),
  timeoutSec: Schema.number().default(3600),
})

const execFileAsync = promisify(execFile)
const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export function apply(ctx, config) {
  ctx.logger(name).info('loaded')

  ctx.tools.register(
    defineTool({
      name: 'dsh_workflow_run',
      description: '运行 DSH 测试工作流：生成三类用例、执行测试、生成报告并按配置回流 MySQL。',
      parameters: {
        only: {
          type: 'string',
          required: false,
          enum: ['api', 'ui', 'all'],
          description: '只执行 api / ui，默认 all。',
        },
        skipGen: { type: 'boolean', required: false, description: '跳过 Harness 生成阶段。' },
        dryDb: { type: 'boolean', required: false, description: 'MySQL 回流只演练不写库。' },
        noDb: { type: 'boolean', required: false, description: '本次不执行 MySQL 回流。' },
        timeoutSec: { type: 'number', required: false, description: '流水线超时秒数。' },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: String(value) }],
      },
      async execute(args) {
        const workflowDir = config.workflowDir ? path.resolve(config.workflowDir) : packageRoot
        const script = path.join(workflowDir, 'scripts', 'run_pipeline.py')
        const cliArgs = ['--only', String(args.only || 'all')]
        if (args.skipGen) cliArgs.push('--skip-gen')
        if (args.dryDb) cliArgs.push('--dry-db')
        if (args.noDb) cliArgs.push('--no-db')
        const timeout = Number(args.timeoutSec || config.timeoutSec || 3600) * 1000

        try {
          const { stdout, stderr } = await execFileAsync(config.python || 'python3', [script, ...cliArgs], {
            cwd: workflowDir,
            timeout,
            maxBuffer: 20 * 1024 * 1024,
          })
          const runId = (stdout.match(/run_id = (run-[^\s]+)/) || [])[1] || ''
          return JSON.stringify({
            ok: true,
            exitCode: 0,
            runId,
            stdout: stdout.slice(-12000),
            stderr: stderr.slice(-4000),
          })
        } catch (error) {
          const e = error || {}
          return JSON.stringify({
            ok: false,
            exitCode: e.code ?? 1,
            stdout: String(e.stdout || '').slice(-12000),
            stderr: String(e.stderr || e.message || '').slice(-4000),
          })
        }
      },
    }),
  )
}
