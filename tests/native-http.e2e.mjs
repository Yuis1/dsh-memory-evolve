// Offline composition of the installed DSH carrier and real plugin components.
// No browser profile, model service, existing DSH_HOME, or network provider is used.
import assert from 'node:assert/strict'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { execFileSync } from 'node:child_process'
import { MemoryStore, ArchiveStore, SuggestionQueue } from '../lib/store.js'
import { installApi } from '../lib/api.js'
import { installCoiApi } from '../lib/coi/api.js'
import { AdapterStore } from '../lib/coi/adapters.js'
import { SessionStore } from '../lib/coi/session-store.js'
import { TaskStore } from '../lib/coi/tasks-store.js'
import { CoiScheduler } from '../lib/coi/scheduler.js'
import { coiToolDefinitions } from '../lib/coi/tools.js'
import { skillManageTool } from '../lib/skills.js'

const runtime = resolve(process.env.DSH_RUNTIME ?? '/home/dhz/.local/share/deepseek-harness/runtime/0.2.1-rc.3')
const load = (name) => import(pathToFileURL(join(runtime, 'node_modules/@deepseek-ai', name, 'lib/index.js')))
const [{ Context }, web, connection, credentials] = await Promise.all([
  load('cordis'), load('dsh-host-webserver'), load('dsh-client-connection'), load('dsh-credentials-local'),
])
const home = mkdtempSync(join(tmpdir(), 'memory-native-e2e-'))
process.env.DSH_HOME = home
process.chdir(home)
const ctx = new Context()
const fibers = []
let scheduler
try {
  fibers.push(ctx.plugin(credentials.default, { path: join(home, 'credentials.yaml'), watch: false }))
  await fibers.at(-1).await()
  fibers.push(ctx.plugin(web.default, { host: '127.0.0.1', port: 0 }))
  await fibers.at(-1).await()
  fibers.push(ctx.plugin(connection))
  await fibers.at(-1).await()
  const base = ctx.webServer.getOrigin()
  assert.ok(base)
  const store = new MemoryStore(home)
  const archive = new ArchiveStore(home)
  const queue = new SuggestionQueue(join(home, 'SUGGESTIONS.jsonl'))
  const config = { memoryDir: home, skillDir: join(home, 'skills'), coiDataDir: join(home, 'coi'), coiTaskTimeoutMs: 5000 }
  const adapters = new AdapterStore(join(home, 'adapters.json'))
  const sessions = new SessionStore(join(home, 'sessions.json'))
  const tasks = new TaskStore(config.coiDataDir)
  scheduler = new CoiScheduler(ctx, { adapters, sessions, tasks, config })
  const state = { coiEnabled: true, reviewEnabled: true }
  fibers.push(ctx.plugin({ name: 'memory-native-e2e', inject: ['webServer'], apply(c) {
    c.effect(() => installApi(c, { store, archive, queue, config, getRuntime: () => state, updateRuntime: (patch) => Object.assign(state, patch) }))
    c.effect(() => installCoiApi(c, { scheduler, adapters, sessions, tasks, config }))
  } }))
  await fibers.at(-1).await()
  const call = async (path, method = 'GET', body, cookie, origin = base) => {
    const response = await fetch(base + path, { method, headers: { origin, ...(cookie ? { cookie } : {}), ...(body ? { 'content-type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined })
    return { status: response.status, data: await response.json() }
  }
  // RED on the original bug: the real plugin exposed its config anonymously.
  assert.equal((await call('/memory-evolve/api/config')).status, 401, 'anonymous config read must require native authentication')
  console.log('PASS anonymous config denied')
  const authUrl = ctx.connection.authenticatedUrl(base + '/connect')
  const exchanged = await fetch(authUrl, { redirect: 'manual' })
  const cookie = exchanged.headers.get('set-cookie')?.split(';')[0]
  assert.ok(cookie, 'fresh native launch token must exchange for synthetic cookie')
  assert.equal((await call('/memory-evolve/api/config', 'GET', undefined, cookie)).status, 200)
  assert.equal((await call('/memory-evolve/api/config', 'POST', { patch: { coiEnabled: false } })).status, 401)
  assert.equal((await call('/memory-evolve/api/config', 'GET', undefined, cookie, 'https://foreign.invalid')).status, 403)
  assert.equal((await call('/memory-evolve/api/memory/memory', 'POST', { content: 'Native owner memory roundtrip' }, cookie)).status, 200)
  const files = await call('/memory-evolve/api/memory-files', 'GET', undefined, cookie)
  assert.equal(files.status, 200)
  assert.match(JSON.stringify(files.data), /Native owner memory roundtrip/)
  queue.append({ time: 'test', target: 'user', content: 'Approved owner suggestion', reason: 'e2e', cwd: null })
  assert.equal((await call('/memory-evolve/api/suggestions/approve', 'POST', { indices: [1] }, cookie)).status, 200)
  assert.match(JSON.stringify((await call('/memory-evolve/api/memory-files', 'GET', undefined, cookie)).data), /Approved owner suggestion/)
  const skill = skillManageTool(ctx, { ...config, skillManageToolName: 'skill_manage', skillMaxBytes: 65536 })
  const skillBody = '---\nname: owner-example\ndescription: Native owner example\n---\n# Owner example\nUse for the isolated local example.\n'
  const created = await skill.execute({ action: 'create', name: 'owner-example', description: 'Native owner example', body: skillBody }, { agent: { session: { header: {} } } })
  assert.equal(created.ok, true)
  assert.equal((await call('/memory-evolve/api/pending-skills/approve', 'POST', { name: 'owner-example' }, cookie)).status, 200)
  const read = await skill.execute({ action: 'read', name: 'owner-example' }, { agent: { session: { header: {} } } })
  assert.equal(read.ok, true)
  assert.match(JSON.stringify(read), /Owner example/)
  // Exercise actual model tool behavior: model-supplied flags cannot authorize
  // a command when the native approval service has not granted this call.
  const dispatchTool = coiToolDefinitions(scheduler).find((tool) => tool.name === 'de_coi_dispatch')
  assert.equal((await dispatchTool.execute({ adapterId: 'bounded-echo', prompt: 'test', approved: true }, { agent: { session: { header: {} } } })).ok, false)
  const def = { id: 'bounded-echo', name: 'Bounded echo', type: 'plain-cli', binary: process.execPath, args: ['-e', 'console.log("bounded task")'], testCmd: ['-e', 'console.log("bounded adapter success")'], defaults: { timeoutMs: 2000 } }
  assert.equal((await call('/memory-evolve/api/coi/adapters', 'POST', { def })).status, 401)
  assert.equal((await call('/memory-evolve/api/coi/adapters', 'POST', { def }, cookie)).status, 200)
  assert.equal((await call('/memory-evolve/api/coi/adapters/test', 'POST', { id: def.id })).status, 401)
  const started = await call('/memory-evolve/api/coi/adapters/test', 'POST', { id: def.id }, cookie)
  assert.equal(started.status, 200)
  const settled = await scheduler.wait(started.data.taskId, 5000)
  assert.equal(settled.task.status, 'completed')
  const log = await call(`/memory-evolve/api/coi/tasks/${started.data.taskId}/log`, 'GET', undefined, cookie)
  assert.match(JSON.stringify(log.data), /bounded adapter success/)
  console.log('PASS native owner memory write/read, suggestion/skill approval, model self-authorization denied and bounded adapter command')
  await fibers[2].dispose()
  assert.equal((await call('/memory-evolve/api/config', 'GET', undefined, cookie)).status, 503)
  console.log('PASS missing native Connection fails closed')
} finally {
  scheduler?.dispose()
  for (const fiber of fibers.reverse()) await fiber.dispose()
  // Ordinary removal follows the host Trash policy, including synthetic credentials.
  execFileSync('trash-put', [home])
  console.log('CLEANUP isolated DSH_HOME moved to Trash; native listeners and scheduler disposed')
}
