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
  autoLogin: Schema.boolean().default(true),
})

const execFileAsync = promisify(execFile)
const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

function redact(text, secret) {
  const value = String(text || '')
  if (!secret) return value
  return value.split(secret).join('***')
}

function lastJson(stdout) {
  const lines = String(stdout || '').split('\n').map((line) => line.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (lines[i].startsWith('{') || lines[i].startsWith('[')) {
      try { return JSON.parse(lines[i]) } catch (_) { /* keep scanning */ }
    }
  }
  return null
}

async function askTarget(ctx, args) {
  const questions = []
  if (!args.targetUrl) {
    questions.push({
      id: 'targetUrl',
      question: '请输入被测系统链接（http:// 或 https://）',
      detail: '例如 https://test.example.com',
      header: '被测系统',
    })
  }
  if (!args.ssoUser) {
    questions.push({
      id: 'ssoUser',
      question: '请输入 SSO 账号',
      detail: '用于尝试自动登录；仅本次使用，不写入配置、报告或数据库',
      header: 'SSO 账号',
    })
  }
  questions.push({
    id: 'ssoPassword',
    question: '请输入 SSO 密码',
    detail: '仅本次使用，不写入配置、报告或数据库；留空则跳过自动登录',
    header: 'SSO 密码',
  })
  questions.push({
    id: 'confirm',
    question: '确认用以上信息启动 EGO 并尝试登录？',
    detail: '确认后将打开 EGO 浏览器、访问被测系统并尽力自动登录。',
    header: '确认',
    options: [{ label: '确认并启动' }, { label: '取消' }],
  })

  if (!ctx.userQuestions || typeof ctx.userQuestions.ask !== 'function') {
    return { unavailable: true }
  }
  const answer = await ctx.userQuestions.ask({ questions })
  const values = {}
  for (const item of answer?.answers || []) {
    const custom = String(item.custom || '').trim()
    const selected = String((item.selected || [])[0] || '').trim()
    values[item.id] = custom || selected
  }
  if (values.confirm !== '确认并启动') return { cancelled: true }
  return {
    url: String(values.targetUrl || args.targetUrl || '').trim(),
    user: String(values.ssoUser || args.ssoUser || '').trim(),
    password: String(values.ssoPassword || ''),
  }
}

async function prepareTarget(ctx, config, workflowDir, target, autoLogin) {
  const script = path.join(workflowDir, 'scripts', 'prepare_target.py')
  const env = {
    ...process.env,
    DSH_TARGET_URL: target.url,
    DSH_SSO_USER: target.user,
    DSH_SSO_PASSWORD: target.password,
    DSH_AUTO_LOGIN: String(autoLogin),
  }
  try {
    const { stdout } = await execFileAsync(config.python || 'python3', [script], {
      cwd: workflowDir,
      env,
      timeout: 180000,
      maxBuffer: 8 * 1024 * 1024,
    })
    return { data: lastJson(stdout) || {}, exitCode: 0 }
  } catch (error) {
    const e = error || {}
    return {
      data: lastJson(e.stdout) || {
        ok: false,
        error: redact(e.stderr || e.message || 'prepare_target.py 执行失败', target.password),
      },
      exitCode: e.code ?? 1,
    }
  }
}

export function apply(ctx, config) {
  ctx.logger(name).info('loaded')

  ctx.tools.register(
    defineTool({
      name: 'dsh_workflow_run',
      description: '运行 DSH 测试工作流。若未提供被测系统链接，会弹窗收集链接/SSO账号/密码并确认，然后启动 EGO、尽力登录并执行全流程。',
      parameters: {
        only: { type: 'string', description: '只执行 api / ui / all，默认 all。' },
        skipGen: { type: 'boolean', description: '跳过 Harness 生成阶段。' },
        dryDb: { type: 'boolean', description: 'MySQL 回流只演练不写库。' },
        noDb: { type: 'boolean', description: '本次不执行 MySQL 回流。' },
        timeoutSec: { type: 'number', description: '流水线超时秒数。' },
        targetUrl: { type: 'string', description: '被测系统链接；不传则弹窗询问。' },
        ssoUser: { type: 'string', description: 'SSO 账号；不传则弹窗询问。' },
        autoLogin: { type: 'boolean', description: '是否尝试自动登录，默认 true。' },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: String(value) }],
      },
      async execute(args) {
        const workflowDir = config.workflowDir ? path.resolve(config.workflowDir) : packageRoot
        const only = String(args.only || 'all')
        if (!['api', 'ui', 'all'].includes(only)) {
          throw new Error('only 必须是 api / ui / all 之一')
        }

        const asked = await askTarget(ctx, args)
        if (asked.cancelled) {
          return JSON.stringify({ ok: false, cancelled: true, message: '用户取消，未启动 EGO' })
        }
        if (asked.unavailable) {
          return JSON.stringify({
            ok: false,
            needsUserInput: true,
            message: '当前会话没有可用提问弹窗服务，请直接在对话中提供被测系统链接、SSO账号与SSO密码后重试。',
          })
        }
        if (!asked.url || !asked.url.startsWith('http')) {
          return JSON.stringify({ ok: false, needsUserInput: true, message: '被测系统链接无效，必须以 http:// 或 https:// 开头。' })
        }

        const autoLogin = args.autoLogin === undefined ? config.autoLogin !== false : Boolean(args.autoLogin)
        const prep = await prepareTarget(ctx, config, workflowDir, asked, autoLogin)
        if (prep.data?.manualLoginRequired) {
          return JSON.stringify({
            ok: false,
            needsManualLogin: true,
            message: '已打开被测系统，但无法自动登录；请在 EGO 浏览器中手动登录，登录完成后重新调用本工具。',
            detail: redact(prep.data.message || '', asked.password),
            screenshotPath: prep.data.screenshotPath || '',
          })
        }
        if (!prep.data?.ok) {
          return JSON.stringify({ ok: false, stage: 'prepare', detail: redact(JSON.stringify(prep.data || {}), asked.password) })
        }

        const script = path.join(workflowDir, 'scripts', 'run_pipeline.py')
        const cliArgs = ['--only', only]
        if (args.skipGen) cliArgs.push('--skip-gen')
        if (args.dryDb) cliArgs.push('--dry-db')
        if (args.noDb) cliArgs.push('--no-db')
        const timeout = Number(args.timeoutSec || config.timeoutSec || 3600) * 1000

        try {
          const { stdout, stderr } = await execFileAsync(config.python || 'python3', [script, ...cliArgs], {
            cwd: workflowDir,
            timeout,
            maxBuffer: 20 * 1024 * 1024,
            env: { ...process.env, DSH_UI_MODE: 'ego_skill', DSH_TARGET_URL: asked.url },
          })
          const runId = (stdout.match(/run_id = (run-[^\s]+)/) || [])[1] || ''
          return JSON.stringify({
            ok: true,
            prepared: { url: asked.url, autoLogin: prep.data.autoLogin || 'ok' },
            exitCode: 0,
            runId,
            stdout: redact(stdout.slice(-12000), asked.password),
            stderr: redact(stderr.slice(-4000), asked.password),
          })
        } catch (error) {
          const e = error || {}
          return JSON.stringify({
            ok: false,
            exitCode: e.code ?? 1,
            stdout: redact(String(e.stdout || '').slice(-12000), asked.password),
            stderr: redact(String(e.stderr || e.message || '').slice(-4000), asked.password),
          })
        }
      },
    }),
  )
}
