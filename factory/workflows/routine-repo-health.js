export const meta = {
  name: 'routine-repo-health',
  description: 'factory repo-health routine: one fast read-only health check per repo, merged findings',
  phases: ['Check', 'Report'],
}

// Saved workflow for the `repo-health` routine (references/routines.md) when
// the routine covers >= 4 repos (references/parallelism.md decision rule).
// Read-only: agents inspect, they never push, open PRs, or edit the board;
// the routine's own conversation files the board issues from this report.
//
// args = {
//   org: 'acme',
//   date: '2026-09-30',                // no Date in workflows
//   perAgentTokens: 6000,
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

const a = args || {}
const repos = a.repos || []
const perAgent = a.perAgentTokens || 6000
log(`routine-repo-health: ${repos.length} repos`)

phase('Check')
const results = (await pipeline(repos, async (_, repo) => {
  if (budget.total && budget.remaining() < perAgent) {
    return { repo: repo.name, status: 'failed', findings: [], error: 'budget' }
  }
  const res = await agent(healthPrompt(repo, a), {
    model: 'fast', effort: 'low', cwd: repo.path, label: `health:${repo.name}`, phase: 'Check', schema: HEALTH,
  })
  return res ? { ...res, repo: repo.name } : { repo: repo.name, status: 'failed', findings: [], error: 'agent returned nothing' }
})).filter(Boolean)

phase('Report')
const counts = { repos: repos.length, ok: 0, findings: 0, failed: 0, high: 0 }
for (const r of results) {
  if (r.status === 'failed') counts.failed++
  else if (!r.findings.length) counts.ok++
  counts.findings += r.findings.length
  counts.high += r.findings.filter((f) => f.severity === 'high').length
}
log(`done: ${counts.findings} findings (${counts.high} high), ${counts.failed} failed`)
report({ results, counts })
