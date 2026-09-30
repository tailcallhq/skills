// Offline harness for workflows/factory-fanout.js: stubs the workflow runtime
// (agent/pipeline/parallel/phase/log/report/args/budget) and runs the script
// against fixture agent answers. No model calls, no network.
//
//   node factory/scripts/test_fanout_workflow.mjs ARGS.json ANSWERS.json [--kill-after N] [--script NAME.js]
//
// ANSWERS maps "<stage>:<repo>" (and "connections") to the agent's return value.
// Prints {report, progress, calls:[{label, model, effort, cwd, phase, hasSchema}], killed}.
// --kill-after N makes agent call N+1 throw an uncatchable abort, simulating a
// killed run; the progress log up to that point is still printed.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const [argsFile, answersFile, ...rest] = process.argv.slice(2)
const script = rest.includes('--script') ? rest[rest.indexOf('--script') + 1] : 'factory-fanout.js'
const src = readFileSync(join(here, '..', 'workflows', script), 'utf8')
const args = JSON.parse(readFileSync(argsFile, 'utf8'))
const answers = JSON.parse(readFileSync(answersFile, 'utf8'))
const killAfter = rest[0] === '--kill-after' ? Number(rest[1]) : Infinity
const budgetTotal = rest.includes('--budget') ? Number(rest[rest.indexOf('--budget') + 1]) : 0

const progress = []
const calls = []
let reported
let current = null
let spent = 0

class Killed extends Error {}

const rt = {
  args,
  budget: { total: budgetTotal, spent: () => spent, remaining: () => budgetTotal - spent },
  phase: (t) => { current = t },
  log: (m) => progress.push({ phase: current, message: String(m) }),
  report: (v) => { reported = v },
  agent: async (prompt, opts = {}) => {
    if (calls.length >= killAfter) throw new Killed('killed')
    calls.push({ label: opts.label, model: opts.model, effort: opts.effort, cwd: opts.cwd ?? null,
      phase: opts.phase, hasSchema: !!opts.schema })
    spent += 1000
    await new Promise((r) => setTimeout(r, 1))
    return Object.prototype.hasOwnProperty.call(answers, opts.label) ? answers[opts.label] : null
  },
  // Same semantics as the host: no barrier between stages; a thrown stage drops the item.
  pipeline: async (items, ...stages) => {
    return Promise.all(items.map(async (item, i) => {
      let prev
      for (const s of stages) {
        try { prev = await s(prev, item, i) } catch (e) { if (e instanceof Killed) throw e; return null }
      }
      return prev
    }))
  },
  parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
    try { return await t() } catch (e) { if (e instanceof Killed) throw e; return null }
  })),
}

const body = src.replace(/^export const meta\s*=/m, 'const meta =')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const fn = new AsyncFunction(...Object.keys(rt), body)
let killed = false
let error = null
try {
  await fn(...Object.values(rt))
} catch (e) {
  if (e instanceof Killed) killed = true
  else error = String(e && e.message || e)
}
process.stdout.write(JSON.stringify({ report: reported ?? null, progress, calls, killed, error }, null, 2) + '\n')
