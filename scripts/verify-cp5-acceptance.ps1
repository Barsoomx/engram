# CP5 checkpoint acceptance gate.
#
# CP5 (docs/superpowers/specs/2026-07-11-checkpoint-5-conflict-only-curation.md) is
# complete only when the conflict-only API AND UI ship, and both the hermetic and the
# selected-provider evaluation gates exit zero against the frozen thresholds. This gate
# is blocking: it must pass before CP5 is closed and before any active rollout. It does
# not inspect corpus semantics; it delegates those to engram_curator_eval, whose nonzero
# exit is the authority for a threshold miss.

param(
    [ValidateNotNullOrEmpty()]
    [string] $ProviderResponsesInContainer = '/srv/app/engram/memory/evals/curation_v1/selected-policy-responses.jsonl'
)

$ErrorActionPreference = 'Stop'
$testCompose = @(
    'compose',
    '-p', 'engram-cp5-acceptance',
    '-f', 'docker-compose.yml'
)
# frontend-ci is a test-only service guarded by the `ci` profile so it never joins a
# production `up`; the profile must be enabled to enumerate and run it.
$runtimeCompose = @(
    'compose',
    '--profile', 'ci',
    '-f', 'deploy/compose/docker-compose.yml'
)

function Invoke-CheckedDocker {
    param([Parameter(Mandatory = $true)][string[]] $Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$services = & docker @runtimeCompose config --services
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to resolve the CP5 Compose contract.'
}
if ($services -notcontains 'frontend-ci') {
    throw 'CP5 acceptance incomplete: deploy/compose/docker-compose.yml has no frontend-ci service.'
}

$frontendContractTest = 'apps/frontend/src/lib/memory-conflict-actions.test.ts'
if (-not (Test-Path -LiteralPath $frontendContractTest)) {
    throw "CP5 acceptance incomplete: missing $frontendContractTest."
}

$evalCommand = Get-ChildItem `
    -Path 'apps/backend/engram' `
    -Recurse `
    -File `
    -Filter 'engram_curator_eval.py'
if (-not $evalCommand) {
    throw 'CP5 acceptance incomplete: missing engram_curator_eval management command.'
}

$evalCommandTests = Get-ChildItem `
    -Path 'apps/backend/engram' `
    -Recurse `
    -File `
    -Filter 'engram_curator_eval_tests.py'
if (-not $evalCommandTests) {
    throw 'CP5 acceptance incomplete: missing engram_curator_eval command tests.'
}

Invoke-CheckedDocker ($testCompose + @(
    'run', '--rm', 'app',
    'pytest', '-q', '-p', 'no:randomly',
    'engram/memory/deterministic_gates_tests.py',
    'engram/memory/curation_shortlist_tests.py',
    'engram/memory/curation_judge_tests.py',
    'engram/memory/curation_tests.py',
    'engram/memory/candidate_ttl_tests.py',
    'engram/console/views/memory_review_tests.py'
))

Invoke-CheckedDocker ($runtimeCompose + @(
    'run', '--rm', 'api',
    'poetry', 'run', 'python', 'manage.py',
    'engram_curator_eval', '--engine', 'fixture', '--format', 'json'
))

Invoke-CheckedDocker ($runtimeCompose + @(
    'run', '--rm', 'api',
    'poetry', 'run', 'python', 'manage.py',
    'engram_curator_eval',
    '--responses', $ProviderResponsesInContainer,
    '--format', 'json'
))

Invoke-CheckedDocker ($runtimeCompose + @(
    'run', '--rm', 'frontend-ci',
    'sh', '-ec',
    'pnpm typecheck && pnpm lint && pnpm build && node --test src/lib/memory-conflict-actions.test.ts'
))
