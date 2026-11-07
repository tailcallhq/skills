// Offline harness for workflows/*.js: runs a script the way
// `workflow_run_file { path, args }` does (an async function body that sees a
// frozen global `args`) against stubs of the runtime's `tools.task` and
// `tools.shell`, with fixture sub-agent answers.
// No model calls, no network. Like the runtime, the removed workflow globals
// (agent, phase, budget, log, report) throw, so a script that still uses them
// fails the tests.
//
//   node factory/scripts/test_fanout_workflow.mjs ARGS.json ANSWERS.json [--kill-after N] [--script NAME.js]
//
// ARGS.json is the script's `args`. ANSWERS maps "<stage>:<repo>" (and
// "connections", "health:<repo>") to the sub-agent's checked reply; a missing
// key is a failed sub-agent. `tools.shell` runs the command for real.
// Prints {report, progress, calls:[{label, model, cwd, agent, hasSchema}], killed, error}.
// --kill-after N makes task call N+1 throw an uncatchable abort, simulating a
// killed run.

import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const [argsFile, answersFile, ...rest] = process.argv.slice(2)
const script = rest.includes('--script') ? rest[rest.indexOf('--script') + 1] : 'factory-fanout.js'
const src = readFileSync(join(here, '..', 'workflows', script), 'utf8')
const args = JSON.parse(readFileSync(argsFile, 'utf8'))
const answers = JSON.parse(readFileSync(answersFile, 'utf8'))
const killAfter = rest.includes('--kill-after') ? Number(rest[rest.indexOf('--kill-after') + 1]) : Infinity

const progress = []
const calls = []

class Killed extends Error {}

// The fixture key of a sub-agent call ("<stage>:<repo>", "connections" or
// "health:<repo>"), from its schema and prompt.
function labelOf(prompt, schema) {
  const req = (schema && schema.required) || []
  if (req.includes('contradictions')) return 'connections'
  if (req.includes('findings')) {
    const path = (/\(checkout: ([^)]*)\)/.exec(prompt) || [])[1] || ''
    return `health:${path.split('/').pop()}`
  }
  const repo = (/repository `([^`]+)`/.exec(prompt) || [])[1]
  const stage = req.includes('system') ? 'draft' : req.includes('kind') ? 'survey'
    : req.includes('deps') ? 'graph' : req.includes('path') ? 'clone' : '?'
  return `${stage}:${repo}`
}

const tools = Object.freeze({
  task: async ({ tasks, agent_id, schema, cwd, model }) => {
    if (!schema) throw new Error('task: schema is required')
    if (tasks.length !== 1) throw new Error('harness: one task per call')
    if (calls.length >= killAfter) throw new Killed('killed')
    const label = labelOf(tasks[0], schema)
    calls.push({ label, model, cwd: cwd ?? null, agent: agent_id, hasSchema: true })
    await new Promise((r) => setTimeout(r, 1))
    const has = Object.prototype.hasOwnProperty.call(answers, label)
    return {
      mode: 'completed',
      agent_id,
      results: [has
        ? { task: tasks[0], session_id: null, output: '', failed: false, value: answers[label] }
        : { task: tasks[0], session_id: null, output: 'fixture has no answer', failed: true }],
    }
  },
  shell: async ({ command, working_dir }) => {
    const p = spawnSync('bash', ['-c', command], { cwd: working_dir, encoding: 'utf8' })
    return { exit_code: p.status ?? -1, stdout: p.stdout, stderr: p.stderr, truncated: false, timed_out: false, needs_stdin: false, elapsed_ms: 0 }
  },
})
const out = (...m) => progress.push(m.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' '))
const console = Object.freeze({ log: out, info: out, warn: out, error: out, debug: out })
const gone = (what) => () => { throw new TypeError(`${what} was removed from workflows`) }
const removed = { agent: gone('agent()'), phase: gone('phase()'), log: gone('log()'), report: gone('report()'),
  budget: new Proxy({}, { get: gone('budget') }), parallel: gone('parallel()'), pipeline: gone('pipeline()') }

const deepFreeze = (v) => { if (v && typeof v === 'object' && !Object.isFrozen(v)) { Object.freeze(v); Object.values(v).forEach(deepFreeze) } return v }
const AsyncFunction = (async () => {}).constructor
const fn = new AsyncFunction('tools', 'console', ...Object.keys(removed), 'args', src)
let killed = false
let error = null
let report = null
try {
  report = await fn(tools, console, ...Object.values(removed), deepFreeze(args))
} catch (e) {
  if (e instanceof Killed) killed = true
  else error = String(e && e.message || e)
}
process.stdout.write(JSON.stringify({ report: report ?? null, progress, calls, killed, error }, null, 2) + '\n')
