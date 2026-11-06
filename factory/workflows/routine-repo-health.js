// factory repo-health routine: one fast read-only health check per repo,
// merged findings. Used by the `repo-health` routine (references/routines.md)
// when it covers >= 4 repos (references/parallelism.md decision rule).
// Read-only: sub-agents inspect, they never push, open PRs, or edit the board;
// the routine's own conversation files the board issues from this report.
//
// This file is the body of an async function taking one parameter, `args`. A
// short `workflow_run` loader script reads it with `tools.shell` and runs it
// with `new AsyncFunction('args', source)(args)`; it uses only `tools.task`
// and `console`.
//
// args = {
//   org: 'acme',
//   date: '2026-09-30',                // no Date in workflows
//   repos: [{ name, path, url }],      // from state.py get repos
// }

const STR = { type: 'string' }
const FINDING = {
  type: 'object',
  properties: {
    check: { type: 'string', enum: ['ci', 'security', 'deps', 'prs', 'docs', 'other'] },
    severity: { type: 'string', enum: ['high', 'medium', 'low'] },
    title: STR,
    evidence: STR,
  },
  required: ['check', 'severity', 'title', 'evidence'],
}
const HEALTH = {
  type: 'object',
  properties: {
    repo: STR,
    status: { type: 'string', enum: ['ok', 'findings', 'failed'] },
    used_skill: { type: 'boolean' },
    findings: { type: 'array', items: FINDING },
    error: STR,
  },
  required: ['repo', 'status', 'findings'],
}

function healthPrompt(repo, a) {
  return [
    `Read-only health check of ${repo.url || repo.name} (checkout: ${repo.path}) for org ${a.org}, date ${a.date}.`,
    'If the `repo-health` skill exists (`skill_view repo-health`), follow it in report-only mode. Otherwise run these checks:',
    '- ci: `gh run list --branch <default branch> --limit 5 --json conclusion,name,url`; failing latest run = high.',
    '- security: `gh api repos/<owner>/<name>/dependabot/alerts?state=open --jq length` (403/404 = note, not a finding); critical/high alerts = high.',
    '- prs: open PRs with no update in 30 days (`gh pr list --json number,updatedAt,url`) = low, one finding listing them.',
    '- docs: no AGENTS.md or README.md at the root = low.',
    'Do NOT push, commit, open PRs or issues, comment, or change anything. Never print tokens or env var values.',
    'Return at most 8 findings, most severe first; evidence is a url or `file:line`. status ok = no findings.',
  ].join('\n')
}

const a = (typeof args === 'string' ? JSON.parse(args) : args) || {}
const repos = a.repos || []
console.log(`routine-repo-health: ${repos.length} repos`)

// One fast sub-agent per repo, all at once (the runtime queues them for a
// slot). A failed sub-agent is a failed repo; a call that throws (bad cwd,
// run-wide cap, cancellation) ends the run.
const results = await Promise.all(repos.map(async (repo) => {
  const { results: [r] } = await tools.task({
    tasks: [healthPrompt(repo, a)], agent_id: 'forge', model: 'fast', cwd: repo.path, schema: HEALTH,
  })
  return r && !r.failed
    ? { ...r.value, repo: repo.name }
    : { repo: repo.name, status: 'failed', findings: [], error: r ? String(r.output).slice(0, 300) : 'no result' }
}))

const counts = { repos: repos.length, ok: 0, findings: 0, failed: 0, high: 0 }
for (const r of results) {
  if (r.status === 'failed') counts.failed++
  else if (!r.findings.length) counts.ok++
  counts.findings += r.findings.length
  counts.high += r.findings.filter((f) => f.severity === 'high').length
}
console.log(`done: ${counts.findings} findings (${counts.high} high), ${counts.failed} failed`)
return { results, counts }
