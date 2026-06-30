# Design: Real session distillation + promotion gate

> Roadmap Слой 3 P0 ("зачем платить"). Base: `chore/pgvector-test-harness` (atop
> master). Branch: `feat/session-distillation`. Tests run on **postgres+pgvector**.

## Problem (current state, file:line)
- Ingest enqueues ONE task per observation (`hooks/services.py:165`
  `process_observation_recorded.delay`); on session_end it sets
  `session.status=ENDED` (`hooks/services.py:118`) but enqueues NO session distiller.
- `ProcessObservationRecorded.execute` (`memory/services.py:199`): 1 observation →
  1 generation LLM call → 1 `MemoryCandidate(confidence=Decimal('0.500'))`
  (`services.py:269`) → **unconditional** `PromoteMemoryCandidate` (`services.py:209`)
  → `Memory(status=APPROVED)`. Every candidate becomes an approved memory.
- A console review queue for `CandidateStatus.PROPOSED` already exists
  (`console/views/memory_review.py`) but is **starved** — nothing stays PROPOSED.
- Provider gateways return ONE `ProviderCallResult` (title+body); no multi-candidate
  or confidence contract. `TaskType.CURATION` exists but is unused (no migration
  needed to use it).

## Target
On session_end, batch the session's observations into a few **synthesized**
candidates each with a **real confidence**, and gate promotion: high-confidence →
auto-approve; the rest → the existing review queue.

## Design

### 1. Config + schema
- `settings/settings.py`: `ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = Decimal('0.800')`.
- `OrganizationSettings` (`core/models.py`): add
  `distillation_auto_approve_threshold = DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)`
  (null → fall back to the settings default). Migration.
- `WorkflowRunType` (`core/models.py`): add
  `SESSION_DISTILLATION = 'session_distillation', 'Session Distillation'`. Migration
  (AlterField on `WorkflowRun.run_type` choices).

### 2. Promotion gate (shared helper)
`memory/services.py`:
```python
def is_auto_promotable(confidence: Decimal | None, threshold: Decimal) -> bool:
    return confidence is not None and confidence >= threshold
```
`resolve_auto_approve_threshold(organization, override=None) -> Decimal` (override →
org setting → settings default).

### 3. Synthesis contract (provider — minimal, gateway return type UNCHANGED)
- Add `response_kind: str = 'single'` to `ProviderCallInput`
  (`model_policy/services.py`). DistillSession passes `response_kind='candidates'`.
- `FakeProviderGateway.call`: when `response_kind=='candidates'`, return a
  `ProviderCallResult` whose `generated_body` is a **deterministic JSON array** of 2
  candidates derived from the prompt, with stable confidences (e.g. `0.90` and `0.40`)
  so tests can assert one auto-promotes and one is held. (Keep the single path
  byte-identical for all existing callers.)
- Real gateways (OpenAI/Anthropic) need no return-type change: the JSON-array
  instruction lives in the distillation system prompt; their text response is parsed
  by DistillSession. (Pass `system_prompt` as already supported.)

### 4. DistillSession service (`memory/distillation.py`, new)
```python
@dataclass(frozen=True)
class DistillSessionInput:
    session_id: uuid.UUID
    request_id: str = ''
    correlation_id: str = ''
    auto_approve_threshold: Decimal | None = None

@dataclass(frozen=True)
class SynthesizedCandidate:
    title: str
    body: str
    confidence: Decimal
    supporting_observation_ids: tuple[str, ...]

@dataclass(frozen=True)
class DistillSessionResult:
    session: AgentSession
    auto_promoted: tuple[Memory, ...]
    queued_for_review: tuple[MemoryCandidate, ...]
```
`DistillSession.execute(data)`:
1. `select_for_update` the `AgentSession`; gather
   `Observation.objects.filter(organization, project, session).order_by('prompt_number','created_at')`.
   Empty → return an empty result (no-op, success).
2. Build `session_distillation_prompt(observations)` aggregating
   title/body/**facts/narrative/concepts**/files_read/files_modified across the batch
   (current `provider_prompt` ignores facts/narrative/concepts — include them), plus
   `session_distillation_system_prompt()` instructing a JSON array of
   `{title, body, confidence (0..1), supporting_observation_ids}`.
3. Resolve `ModelPolicy task_type='curation'` (fall back to `'generation'` if no
   curation policy); ONE `get_provider_gateway(policy).call(ProviderCallInput(..., response_kind='candidates'))`.
4. Parse `result.generated_body` as JSON → `SynthesizedCandidate`s. Parse failure →
   one fallback candidate from the raw body with confidence `0.500`. Clamp confidence
   to `[0,1]`, 3 decimals.
5. Per candidate: `content_hash = sha256(f'{session_id}:{title}:{body}')`
   (per-candidate, avoids the per-observation unique-collision at
   `core/models.py:469`). Idempotency: skip if a `MemoryCandidate` with that hash
   exists in the project. Create `MemoryCandidate(status=PROPOSED, confidence, content_hash, evidence=...)`.
6. **Gate**: `threshold = resolve_auto_approve_threshold(org, data.auto_approve_threshold)`.
   For each candidate: `is_auto_promotable(confidence, threshold)` → `PromoteMemoryCandidate().execute(...)` (→ APPROVED + indexed RetrievalDocument);
   else leave PROPOSED (now visible in the review queue) and write
   `AuditEvent(event_type='MemoryCandidateHeldForReview', actor_type='system', result=RECORDED, metadata={confidence, threshold})`.
7. Wrap in a `WorkflowRun(run_type=SESSION_DISTILLATION)` via the existing
   `run_*_with_tracking` pattern (QUEUED→RUNNING→SUCCEEDED/FAILED, provider_call_ids).
   Return the result.

### 5. Trigger
- `memory/tasks.py`: new `distill_session(session_id: str)` celery task (bind, retry on
  retryable `MemoryWorkerError`), calling `DistillSession`.
- `hooks/services.py`: where session_end sets `status=ENDED` (~line 118), ALSO enqueue
  `distill_session` via the SAME transactional-outbox enqueue mechanism used for
  `process_observation_recorded` (so it is atomic with the ingest commit).
- **Per-observation path**: KEEP `ProcessObservationRecorded` creating the candidate,
  but **route it through the gate** instead of unconditional promote — i.e. replace the
  unconditional `PromoteMemoryCandidate` at `services.py:209` with the gate
  (`is_auto_promotable(candidate.confidence(0.5), threshold(0.8))` → held). This makes
  per-observation candidates land in the review queue (confidence 0.5 < 0.8), while the
  session-batch synthesis produces the high-confidence auto-approved memories. Update
  the existing `memory_worker_tests.py` expectations accordingly (PROPOSED/held instead
  of auto-APPROVED) — this is the intended behavior change.

## Tests (postgres+pgvector harness)
- `memory/distillation_tests.py`: build a session + N observations; stub via the
  FakeProviderGateway (`response_kind='candidates'` → 2 candidates, conf 0.9 + 0.4):
  assert the 0.9 candidate → APPROVED Memory (+ RetrievalDocument), the 0.4 → PROPOSED
  and a `MemoryCandidateHeldForReview` audit; assert a `SESSION_DISTILLATION` WorkflowRun
  SUCCEEDED; assert per-candidate content_hash (no collision for 2 candidates); empty
  session → no-op; idempotent re-run.
- gate unit tests (`is_auto_promotable`, `resolve_auto_approve_threshold`).
- update `memory_worker_tests.py` for the gate-routed per-observation behavior.
- session_end enqueues `distill_session` (extend `hook_ingest_tests.py`).
- full suite green; `ruff` clean; `makemigrations --check` clean.

## Out of scope (next slices)
Curator (semantic near-dup/supersede), hybrid pgvector retrieval, token-budget
packing, structured weekly digest. This slice is the distiller + gate + trigger only.
