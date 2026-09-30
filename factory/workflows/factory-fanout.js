export const meta = {
  name: 'factory-fanout',
  description: 'factory phases 2-3: clone, survey, repo_graph and KB system draft per repo on fast models; one intelligent connections pass',
  phases: ['Clone', 'Survey', 'Graph', 'Draft', 'Connections', 'Report'],
}

// Saved workflow for factory phases 2 (bootstrap) and 3 (research).
// Contract: references/parallelism.md. Args are built by
// `scripts/fanout.py args` from `state.py pending-repos <stage> --ready`; the
// orchestrator (not this script: it has no filesystem) records results with
// `scripts/fanout.py record`, which calls state.py set-repo per repo/stage.
//
// args = {
//   stage: 'bootstrap' | 'research',
//   kbPath: '/abs/knowledge',             // KB checkout, read-only here
//   skillDir: '/abs/factory',             // for scripts/repo_graph.py
//   verified: '2026-09-30',               // date stamped on facts (no Date in workflows)
//   jobs: 8,                              // --jobs passed to scripts
//   perAgentTokens: 6000,                 // budget guard estimate per agent() call
//   repos: [{ name, path, url, todo: ['clone', ...], prior: { survey: {...}, ... } }],
//   contextDrafts: [SystemDraft, ...],    // drafts of repos already done (for connections)
// }
//
// Every per-repo agent: model 'fast', effort 'low', cwd = repo path, schema.
// Exactly one barrier (research only): a single 'intelligent' connections agent.
// Each finished stage logs one `FANOUT {json}` line: if the run is killed the
// orchestrator recovers finished stages from the progress log
// (`fanout.py record --log`), so a resume re-fans-out only unfinished repos.

const STAGES_BY_RUN = { bootstrap: ['clone'], research: ['survey', 'graph', 'draft'] }
const PHASE_OF = { clone: 'Clone', survey: 'Survey', graph: 'Graph', draft: 'Draft' }

const STR = { type: 'string' }
const STRS = { type: 'array', items: { type: 'string' } }
const FACT = {
  type: 'object',
  properties: { text: STR, source: STR },
  required: ['text', 'source'],
}
const EDGE = {
  type: 'object',
  properties: { to: STR, kind: STR, protocol: STR, evidence: STR, source: STR },
  required: ['to', 'kind', 'evidence', 'source'],
}

const SCHEMAS = {
  clone: {
    type: 'object',
    properties: {
      repo: STR,
      status: { type: 'string', enum: ['cloned', 'present', 'failed'] },
      path: STR,
      default_branch: STR,
      head: STR,
      error: STR,
    },
    required: ['repo', 'status', 'path'],
  },
  survey: {
    type: 'object',
    properties: {
      repo: STR,
      kind: { type: 'string', enum: ['service', 'web-app', 'cli', 'library', 'monorepo', 'infra', 'docs', 'other'] },
      languages: STRS,
      build: STRS,
      test: STRS,
      run: STRS,
      ci: STRS,
      guidance_files: STRS,
      missing_prerequisites: STRS,
      summary: STR,
    },
    required: ['repo', 'kind', 'languages', 'build', 'test', 'summary'],
  },
  graph: {
    type: 'object',
    properties: {
      repo: STR,
      source: { type: 'string', enum: ['repo_graph.py', 'local-scan'] },
      languages: STRS,
      deps: {
        type: 'array',
        items: {
          type: 'object',
          properties: { name: STR, ecosystem: STR, internal: { type: 'boolean' } },
          required: ['name', 'ecosystem', 'internal'],
        },
      },
      edges: { type: 'array', items: EDGE },
      api_calls: { type: 'integer' },
    },
    required: ['repo', 'source', 'deps', 'edges'],
  },
  draft: {
    type: 'object',
    properties: {
      system: STR,
      repos: STRS,
      purpose: STR,
      interfaces: STRS,
      deps: STRS,
      edges: { type: 'array', items: EDGE },
      facts: { type: 'array', items: FACT },
      questions: STRS,
    },
    required: ['system', 'repos', 'purpose', 'edges', 'facts'],
  },
}

const CONNECTIONS = {
  type: 'object',
  properties: {
    edges: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          from: STR,
          to: STR,
          protocol: STR,
          auth: STR,
          evidence: STRS,
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
        required: ['from', 'to', 'protocol', 'evidence', 'confidence'],
      },
    },
    contradictions: {
      type: 'array',
      items: {
        type: 'object',
        properties: { topic: STR, claims: STRS, resolution: STR },
        required: ['topic', 'claims', 'resolution'],
      },
    },
    questions: STRS,
  },
  required: ['edges', 'contradictions', 'questions'],
}

function stagePrompt(stage, repo, a) {
  const head = `You are one step of factory's per-repo fan-out for repository \`${repo.name}\` at ${repo.path}. ` +
    'Work only inside that directory. Never print secrets or env var values. Never push, commit, or modify tracked files. ' +
    'Answer with the JSON object only.'
  if (stage === 'clone') {
    return `${head}\nTask: make sure ${repo.url} is checked out at ${repo.path}. ` +
      `If ${repo.path}/.git exists, verify \`git -C ${repo.path} remote get-url origin\` points at the same repo and report status "present"; ` +
      `otherwise run \`git clone --filter=blob:none ${repo.url} ${repo.path}\` (gh auth is already the git credential helper) and report "cloned". ` +
      'On any error report status "failed" with the first error line. Include default_branch and the short HEAD sha.'
  }
  if (stage === 'survey') {
    return `${head}\nTask: read-only survey in the style of the repo-setup skill's "Look before asking" step ` +
      '(guidance files, manifests/lockfiles, CI config, workspace layout, `command -v` for required tools). ' +
      'Do NOT apply any setup and do not run builds. List the real build/test/run commands as found in CI or docs.'
  }
  if (stage === 'graph') {
    return `${head}\nTask: run \`python3 ${a.skillDir}/scripts/repo_graph.py ${repo.url} --jobs ${a.jobs} --out -\` and summarize its JSON ` +
      '(source "repo_graph.py", copy api_calls). If the script is missing or fails, scan manifests locally instead ' +
      '(package.json, Cargo.toml, pyproject.toml, go.mod, .gitmodules, docker-compose*, k8s/helm values, .github/workflows) and use source "local-scan". ' +
      'Mark a dep internal when it names another repo of the same org. Every edge needs evidence (file:line or api path) and a source.'
  }
  const prior = { survey: repo.prior.survey || null, graph: repo.prior.graph || null }
  return `${head}\nTask: draft the knowledge-base system entry for this repo (one system per repo unless the survey says monorepo; ` +
    `then name the main system). Use only the facts below plus files you read; every fact needs source (repo path or gh api call) ` +
    `and will be stamped verified ${a.verified}. Put anything you cannot derive into questions. Do not write into ${a.kbPath}.\n` +
    `INPUT ${JSON.stringify(prior)}`
}

function connectionsPrompt(drafts) {
  return 'You reconcile factory knowledge-base system drafts into candidate `connections.md` edges. ' +
    'Merge duplicate edges (same from/to/protocol), keep all evidence, set confidence high only with two independent sources. ' +
    'When drafts disagree, list a contradiction with each claim and its source; never silently pick one ' +
    '(precedence for the eventual resolution: user > infra:* > manifest-derived). Drop edges to systems not in the drafts only if ' +
    'they are third-party; keep internal unknowns as questions. Answer with the JSON object only.\n' +
    `DRAFTS ${JSON.stringify(drafts)}`
}

// Mechanical merge; kept byte-for-byte equivalent to scripts/fanout.py merge.
function merge(stage, perRepo, connections) {
  const repos = {}
  for (const r of [...perRepo].sort((x, y) => (x.name < y.name ? -1 : x.name > y.name ? 1 : 0))) {
    repos[r.name] = { status: r.status, results: r.results, failed_stage: r.failed_stage || null }
  }
  const names = Object.keys(repos)
  return {
    stage,
    repos,
    connections: connections || null,
    counts: {
      repos: names.length,
      ok: names.filter((n) => repos[n].status === 'ok').length,
      failed: names.filter((n) => repos[n].status !== 'ok').length,
    },
  }
}

// ------------------------------------------------------------------ body

const a = args || {}
if (!STAGES_BY_RUN[a.stage]) throw new Error(`args.stage must be bootstrap|research, got ${a.stage}`)
const repos = a.repos || []
const runStages = STAGES_BY_RUN[a.stage]
const perAgent = a.perAgentTokens || 6000
log(`factory-fanout ${a.stage}: ${repos.length} repos, stages ${runStages.join(',')}, budget ${budget.total || 'unbounded'}`)

function guard(what) {
  if (budget.total && budget.remaining() < perAgent) {
    throw new Error(`budget: ${budget.remaining()} tokens left < ${perAgent} needed for ${what}; resume later`)
  }
}

function stageFn(stage) {
  return async (acc, repo) => {
    if (!acc) return null
    if (acc.failed_stage) return acc
    if (!(repo.todo || []).includes(stage)) {
      acc.results[stage] = (repo.prior || {})[stage] || null
      return acc
    }
    try {
      guard(`${repo.name}:${stage}`)
    } catch (e) {
      log(`FANOUT ${JSON.stringify({ repo: repo.name, stage, status: 'skipped-budget' })}`)
      acc.failed_stage = stage
      acc.status = 'budget'
      return acc
    }
    const res = await agent(stagePrompt(stage, { ...repo, prior: { ...(repo.prior || {}), ...acc.results } }, a), {
      model: 'fast', effort: 'low', cwd: stage === 'clone' ? a.workspace || repo.path : repo.path,
      label: `${stage}:${repo.name}`, phase: PHASE_OF[stage], schema: SCHEMAS[stage],
    })
    const ok = res !== null && !(stage === 'clone' && res.status === 'failed')
    log(`FANOUT ${JSON.stringify({ repo: repo.name, stage, status: ok ? 'done' : 'failed', result: res })}`)
    acc.results[stage] = res
    if (!ok) {
      acc.failed_stage = stage
      acc.status = 'failed'
    }
    return acc
  }
}

const perRepo = (await pipeline(
  repos,
  async (_, repo) => ({ name: repo.name, status: 'ok', results: {}, failed_stage: null }),
  ...runStages.map(stageFn),
)).filter(Boolean)

let connections = null
if (a.stage === 'research') {
  phase('Connections')
  const drafts = [...(a.contextDrafts || []), ...perRepo.filter((r) => r.status === 'ok').map((r) => r.results.draft)]
    .filter(Boolean)
  if (drafts.length) {
    guard('connections')
    connections = await agent(connectionsPrompt(drafts), {
      model: 'intelligent', effort: 'medium', label: 'connections', phase: 'Connections', schema: CONNECTIONS,
    })
    log(`connections: ${connections ? connections.edges.length : 0} edges, ${connections ? connections.contradictions.length : 0} contradictions`)
  } else {
    log('connections: no drafts, skipped')
  }
}

phase('Report')
const out = merge(a.stage, perRepo, connections)
log(`done: ${out.counts.ok}/${out.counts.repos} ok, ${out.counts.failed} failed`)
report(out)
