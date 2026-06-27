# Semantic Retrieval Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers-extended-cc:subagent-driven-development (recommended) or
> superpowers-extended-cc:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic embeddings adapter, persist an embedding vector on
each `RetrievalDocument`, and use cosine similarity as a second retrieval signal
inside `BuildContextBundle` when exact matching does not fill the requested
limit.

**Architecture:** Approach A from
`docs/superpowers/specs/2026-06-26-semantic-retrieval-foundation-design.md`. A
new embeddings provider method on `FakeProviderGateway` produces a deterministic
feature-hashing vector. `IndexMemoryVersion` stores it on a new
`RetrievalDocument.embedding_vector` JSONField. `BuildContextBundle` keeps exact
matching authoritative and adds a cosine fallback when exact matches are fewer
than the requested limit.

**Tech Stack:** Django/DRF, PostgreSQL (JSONField), Celery memory worker,
pytest, Ruff, existing `FakeProviderGateway` provider boundary.

**User decisions (already made):**

- Embedding storage approach: vector `JSONField` plus cosine fallback (Approach
  A), no pgvector in this slice.
- Missing embeddings policy degrades gracefully to exact-only; disabled
  embeddings secret skips embedding with a warning log.
- E2E golden path stays exact and is not modified; semantic behavior is proved
  by focused backend tests.

---

## File Structure

- `apps/backend/engram/core/models.py` — add `RetrievalDocument.embedding_vector`.
- `apps/backend/engram/core/migrations/0004_retrievaldocument_embedding_vector.py`
  — additive `AddField`.
- `apps/backend/engram/model_policy/services.py` — add `EmbeddingCallInput`,
  `EmbeddingCallResult`, `EMBEDDING_DIMENSION`, `generated_embedding`, and
  `FakeProviderGateway.embed`.
- `apps/backend/engram/context/services.py` — add `SEMANTIC_MIN_SIMILARITY`,
  `cosine_similarity`; extend `IndexMemoryVersion` with embedding lifecycle;
  extend `BuildContextBundle` with semantic fallback and dynamic
  `retrieval_strategy`.
- `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
  — add a golden-path embeddings `ModelPolicy`.
- `apps/backend/engram/context/context_api_tests.py` — add semantic fallback
  tests and a `create_embedding_policy` helper.
- `apps/backend/engram/memory/memory_worker_tests.py` — assert the indexer
  writes an embedding vector and reuses one provider call across re-indexing.
- `apps/backend/engram/model_policy/model_policy_tests.py` — assert
  `FakeProviderGateway.embed` is deterministic, redacted, idempotent, and
  refuses a disabled secret.
- `apps/backend/engram/core/golden_path_tests.py` — assert the bootstrap creates
  an embeddings policy bound to the golden secret.
- `docs/verification-matrix.md` — record the checkpoint.
- `docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md` — record
  the focused security review.

---

### Task 1: RetrievalDocument Embedding Vector Field

**Goal:** Add a vector JSONField to `RetrievalDocument` and an additive
migration, without changing indexing behavior yet.

**Files:**

- Modify: `apps/backend/engram/core/models.py` (the `RetrievalDocument` model)
- Create: `apps/backend/engram/core/migrations/0004_retrievaldocument_embedding_vector.py`
- Test: `apps/backend/engram/core/core_models_tests.py`

**Acceptance Criteria:**

- [ ] `RetrievalDocument` has an `embedding_vector = JSONField(default=list, blank=True)`.
- [ ] `0004_retrievaldocument_embedding_vector` migration adds the field.
- [ ] `makemigrations --check --dry-run` reports no changes after the migration
  is committed.
- [ ] A model test asserts the default is an empty list for a new document.

**Verify:**
`docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/core/core_models_tests.py -v && python manage.py makemigrations --check --dry-run"`

**Steps:**

- [ ] **Step 1: Write the failing model test**

Add to `apps/backend/engram/core/core_models_tests.py`:

```python
@pytest.mark.django_db
def test_retrieval_document_defaults_to_empty_embedding_vector() -> None:
    from engram.core.models import Memory, MemoryVersion, RetrievalDocument

    organization = Organization.objects.create(name='Org', slug='org')
    project = Project.objects.create(organization=organization, name='P', slug='p')
    memory = Memory.objects.create(organization=organization, project=project, title='t', body='b')
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body='b',
        content_hash='h',
    )
    document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        memory_version=version,
        full_text='t',
    )

    assert document.embedding_vector == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/core/core_models_tests.py::test_retrieval_document_defaults_to_empty_embedding_vector -v"`
Expected: FAIL with `TypeError: 'embedding_vector' is an invalid keyword argument` or a migration error.

- [ ] **Step 3: Add the field**

In `apps/backend/engram/core/models.py`, inside the `RetrievalDocument` model,
add (next to `embedding_reference`):

```python
    embedding_vector = models.JSONField(default=list, blank=True)
```

- [ ] **Step 4: Create the migration**

Run:
`docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py makemigrations core -n retrievaldocument_embedding_vector"`
Expected: a new migration file `0004_retrievaldocument_embedding_vector.py` with
an `AddField` operation.

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/core/core_models_tests.py::test_retrieval_document_defaults_to_empty_embedding_vector -v"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/engram/core/models.py apps/backend/engram/core/migrations/0004_retrievaldocument_embedding_vector.py apps/backend/engram/core/core_models_tests.py
git commit -m "feat: add retrieval document embedding vector field"
```

```json:metadata
{"files": ["apps/backend/engram/core/models.py", "apps/backend/engram/core/migrations/0004_retrievaldocument_embedding_vector.py", "apps/backend/engram/core/core_models_tests.py"], "verifyCommand": "docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \"poetry install --no-interaction --no-root --with dev && pytest engram/core/core_models_tests.py -v\"", "acceptanceCriteria": ["RetrievalDocument.embedding_vector JSONField default=list exists", "0004 migration adds the field", "makemigrations --check --dry-run reports no changes"], "modelTier": "mechanical"}
```

---

### Task 2: Embeddings Provider Adapter

**Goal:** Add the embeddings provider boundary: input/result dataclasses, a
deterministic feature-hashing embedding function, and `FakeProviderGateway.embed`
that mirrors the generation `call` contract (active secret, idempotent provider
call record, redacted input).

**Files:**

- Modify: `apps/backend/engram/model_policy/services.py`
- Test: `apps/backend/engram/model_policy/model_policy_tests.py`

**Acceptance Criteria:**

- [ ] `generated_embedding(text)` returns a 64-dimension L2-normalized float
  list, deterministic for the same redacted input, and the zero vector for empty
  text.
- [ ] `FakeProviderGateway.embed(EmbeddingCallInput)` reuses an existing
  `ProviderCallRecord` for the same
  `(organization, project, task_type='embedding', request_id)`.
- [ ] The input text is redacted before tokenization; the persisted provider call
  record and the returned vector contain no raw secret-shaped value.
- [ ] `embed` raises `ProviderSecretError` when the policy secret is disabled or
  has no active envelope.
- [ ] Token usage equals the number of character 3-grams derived from the
  redacted text.

**Verify:**
`docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/model_policy/model_policy_tests.py -v"`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Add to `apps/backend/engram/model_policy/model_policy_tests.py`:

```python
from engram.model_policy.services import (
    EMBEDDING_DIMENSION,
    EmbeddingCallInput,
    FakeProviderGateway,
    generated_embedding,
)


def test_generated_embedding_is_deterministic_and_normalized() -> None:
    first = generated_embedding('authorization before ranking protects context bundles')
    second = generated_embedding('authorization before ranking protects context bundles')

    assert len(first) == EMBEDDING_DIMENSION
    assert first == second
    norm = sum(component * component for component in first) ** 0.5
    assert round(norm, 6) == 1.0


def test_generated_embedding_returns_zero_vector_for_empty_text() -> None:
    assert generated_embedding('') == [0.0] * EMBEDDING_DIMENSION
    assert generated_embedding('   ') == [0.0] * EMBEDDING_DIMENSION


@pytest.mark.django_db
def test_fake_provider_gateway_embed_reuses_call_and_redacts_input() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )
    policy = ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Embedding policy',
        scope='project',
        task_type='embedding',
        provider='openai',
        model='text-embedding-3-small',
        secret=secret,
        version=1,
    )
    data = EmbeddingCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=team.id,
        policy=policy,
        request_id='memory-indexer:embedding-1:embedding',
        trace_id='trace-embedding-1',
        text=f'embedding prompt with {RAW_PROVIDER_SECRET}',
    )

    first = FakeProviderGateway().embed(data)
    second = FakeProviderGateway().embed(data)

    assert first.provider == 'openai'
    assert first.model == 'text-embedding-3-small'
    assert len(first.embedding) == EMBEDDING_DIMENSION
    assert second.call_record_id == first.call_record_id
    record = ProviderCallRecord.objects.get(id=first.call_record_id)
    assert record.task_type == 'embedding'
    assert record.redaction_state == 'redacted'
    assert RAW_PROVIDER_SECRET not in str(record.__dict__)
    assert RAW_PROVIDER_SECRET not in str(first.embedding)


@pytest.mark.django_db
def test_fake_provider_gateway_embed_refuses_disabled_secret() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
        active=False,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )
    policy = ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Embedding policy',
        scope='project',
        task_type='embedding',
        provider='openai',
        model='text-embedding-3-small',
        secret=secret,
        version=1,
    )

    with pytest.raises(ProviderSecretError, match='provider secret is disabled'):
        FakeProviderGateway().embed(
            EmbeddingCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id,
                policy=policy,
                request_id='memory-indexer:embedding-disabled:embedding',
                trace_id='trace-embedding-disabled',
                text='text',
            ),
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/model_policy/model_policy_tests.py -v"`
Expected: FAIL with import errors for `EMBEDDING_DIMENSION`, `EmbeddingCallInput`,
and `generated_embedding`.

- [ ] **Step 3: Add the embedding function and dataclasses**

In `apps/backend/engram/model_policy/services.py`, after the
`generated_candidate_content` function, add:

```python
import math
import re

EMBEDDING_DIMENSION = 64


@dataclass(frozen=True)
class EmbeddingCallInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    policy: ModelPolicy
    request_id: str
    trace_id: str
    text: str


@dataclass(frozen=True)
class EmbeddingCallResult:
    provider: str
    model: str
    call_record_id: uuid.UUID
    redaction_state: str
    embedding: tuple[float, ...]


def _embedding_grams(text: str) -> tuple[str, ...]:
    cleaned = re.sub(r'[^a-z0-9]+', '', text.lower())
    if len(cleaned) < 3:

        return ()

    return tuple(cleaned[i:i + 3] for i in range(len(cleaned) - 2))


def generated_embedding(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
    for gram in _embedding_grams(text):
        digest = hashlib.sha256(gram.encode()).digest()
        dim_index = int.from_bytes(digest[:8], 'big') % EMBEDDING_DIMENSION
        sign = 1.0 if digest[8] % 2 == 0 else -1.0
        vector[dim_index] += sign

    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:

        return [0.0] * EMBEDDING_DIMENSION

    return [round(component / norm, 6) for component in vector]
```

Note: `import math` and `import re` go to the top of the module with the other
stdlib imports. Keep the dataclasses next to the other provider dataclasses if
that reads better; the function stays near `generated_candidate_content`.

- [ ] **Step 4: Add `FakeProviderGateway.embed`**

In `apps/backend/engram/model_policy/services.py`, add a second method to
`FakeProviderGateway` after the existing `call` method:

```python
    def embed(self, data: EmbeddingCallInput) -> EmbeddingCallResult:
        policy = data.policy
        secret = policy.secret
        if not secret.active:
            raise ProviderSecretError('provider secret is disabled')
        if not ProviderSecretEnvelope.objects.filter(secret=secret, active=True).exists():
            raise ProviderSecretError('provider secret has no active envelope')

        existing_record = (
            ProviderCallRecord.objects.filter(
                organization_id=data.organization_id,
                project_id=data.project_id,
                task_type=policy.task_type,
                request_id=data.request_id,
            )
            .order_by('created_at')
            .first()
        )
        redacted_text = redact_value(data.text)
        embedding = tuple(generated_embedding(str(redacted_text.value)))
        if existing_record is not None:

            return EmbeddingCallResult(
                provider=existing_record.provider,
                model=existing_record.model,
                call_record_id=existing_record.id,
                redaction_state=existing_record.redaction_state,
                embedding=embedding,
            )

        text_was_redacted = redacted_text.redacted or '[REDACTED]' in data.text
        token_count = len(_embedding_grams(str(redacted_text.value)))
        record = ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=data.trace_id,
            redaction_state='redacted' if text_was_redacted else 'clean',
            token_usage={'input_tokens': token_count, 'output_tokens': 0},
            latency_ms=0,
            cost_metadata={'estimated': True, 'cost_usd': '0.0000'},
            result=AuditResult.RECORDED,
            metadata={'prompt_retained': False},
        )

        return EmbeddingCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state=record.redaction_state,
            embedding=embedding,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/model_policy/model_policy_tests.py -v"`
Expected: PASS, including the four new embedding tests.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/engram/model_policy/services.py apps/backend/engram/model_policy/model_policy_tests.py
git commit -m "feat: add embeddings provider adapter"
```

```json:metadata
{"files": ["apps/backend/engram/model_policy/services.py", "apps/backend/engram/model_policy/model_policy_tests.py"], "verifyCommand": "docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \"poetry install --no-interaction --no-root --with dev && pytest engram/model_policy/model_policy_tests.py -v\"", "acceptanceCriteria": ["generated_embedding deterministic 64-dim normalized, zero for empty", "FakeProviderGateway.embed idempotent by request_id", "embed redacts input before tokenization", "embed refuses disabled secret", "token usage equals redacted token count"], "modelTier": "standard"}
```

---

### Task 3: IndexMemoryVersion Embedding Lifecycle

**Goal:** When an embeddings policy exists, `IndexMemoryVersion` stores the
embedding vector and provider call reference on the `RetrievalDocument`. Missing
policy and disabled secret both skip embedding without failing indexing.

**Files:**

- Modify: `apps/backend/engram/context/services.py` (`IndexMemoryVersion`)
- Test: `apps/backend/engram/memory/memory_worker_tests.py`

**Acceptance Criteria:**

- [ ] After a memory version is indexed with an embeddings policy present, the
  `RetrievalDocument.embedding_vector` is a non-empty 64-element list and
  `embedding_reference` is a non-empty provider call reference.
- [ ] Re-indexing the same memory version produces the same vector and reuses
  the same embeddings provider call record.
- [ ] When no embeddings policy exists, `embedding_vector` stays `[]`,
  `embedding_reference` stays `''`, and indexing succeeds.
- [ ] When the embeddings secret is disabled, indexing still succeeds and a
  structured warning is logged.
- [ ] No raw secret-shaped value reaches the vector or the document fields.

**Verify:**
`docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_worker_tests.py -v"`

**Steps:**

- [ ] **Step 1: Add a `create_embedding_policy` helper and failing tests**

In `apps/backend/engram/memory/memory_worker_tests.py`, add a helper next to
`create_generation_policy`:

```python
def create_embedding_policy(organization: Organization, team: Team, project: Project) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team Embedding OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-embedding-secret',
        hmac_digest='embedding-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Embedding policy',
        scope='project',
        task_type='embedding',
        provider='openai',
        model='text-embedding-3-small',
        secret=secret,
        version=1,
    )
```

Update the `from engram.model_policy.models import ...` line to include
`ProviderSecretEnvelope` (already imported) and keep the existing imports.

Add these tests:

```python
@pytest.mark.django_db
def test_index_memory_version_writes_embedding_vector_and_reference() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)

    execute_worker(observation)

    document = RetrievalDocument.objects.get()
    assert len(document.embedding_vector) == 64
    assert document.embedding_reference.startswith('provider:')
    assert document.embedding_vector == document.embedding_vector


@pytest.mark.django_db
def test_index_memory_version_embedding_is_idempotent_across_reindex() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)

    execute_worker(observation)
    first_document = RetrievalDocument.objects.get()
    first_vector = list(first_document.embedding_vector)
    first_reference = first_document.embedding_reference

    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=first_document.memory_version_id))

    second_document = RetrievalDocument.objects.get()
    assert second_document.embedding_vector == first_vector
    assert second_document.embedding_reference == first_reference
    from engram.model_policy.models import ProviderCallRecord
    embedding_calls = ProviderCallRecord.objects.filter(task_type='embedding')
    assert embedding_calls.count() == 1


@pytest.mark.django_db
def test_index_memory_version_skips_embedding_without_policy() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)

    execute_worker(observation)

    document = RetrievalDocument.objects.get()
    assert document.embedding_vector == []
    assert document.embedding_reference == ''


@pytest.mark.django_db
def test_index_memory_version_skips_embedding_when_secret_disabled() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    embedding_policy = create_embedding_policy(organization, team, project)
    embedding_policy.secret.active = False
    embedding_policy.secret.save(update_fields=['active'])

    execute_worker(observation)

    document = RetrievalDocument.objects.get()
    assert document.embedding_vector == []
    assert document.embedding_reference == ''
```

Add `IndexMemoryVersion` and `IndexMemoryVersionInput` to the existing import
from `engram.context.services` at the top of the test file, and import
`RetrievalDocument` from `engram.core.models` (already imported).

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_worker_tests.py -v"`
Expected: the first two tests FAIL because `embedding_vector` is `[]` and
`embedding_reference` is `''`; the last two already PASS.

- [ ] **Step 3: Add the embedding step to `IndexMemoryVersion`**

In `apps/backend/engram/context/services.py`, update the imports to add:

```python
import structlog

from engram.model_policy.services import (
    EmbeddingCallInput,
    EmbeddingCallResult,
    FakeProviderGateway,
    ModelPolicyError,
    ProviderSecretError,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
)
```

Add a module logger near the top:

```python
logger = structlog.get_logger(__name__)
```

Replace the tail of `IndexMemoryVersion.execute` so that after
`update_or_create` it calls the embedding step before returning:

```python
        retrieval_document, created = RetrievalDocument.objects.update_or_create(
            memory_version=version,
            defaults={
                'organization': memory.organization,
                'project': memory.project,
                'team': memory.team,
                'memory': memory,
                'visibility_scope': memory.visibility_scope,
                'source_observation_ids': [str(observation.id)] if observation is not None else [],
                'file_paths': file_paths,
                'symbols': symbols,
                'exact_terms': exact_terms,
                'full_text': full_text,
                'embedding_reference': '',
                'stale': memory.stale,
                'refuted': memory.refuted,
                'metadata': {},
            },
        )
        self._embed_document(retrieval_document, memory, version)

        return IndexMemoryVersionResult(retrieval_document=retrieval_document, created=created)
```

Add the private method to the same class:

```python
    def _embed_document(
        self,
        document: RetrievalDocument,
        memory: Memory,
        version: MemoryVersion,
    ) -> None:
        try:
            resolved = ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=memory.organization_id,
                    project_id=memory.project_id,
                    team_id=memory.team_id,
                    task_type='embedding',
                ),
            )
            result = FakeProviderGateway().embed(
                EmbeddingCallInput(
                    organization_id=memory.organization_id,
                    project_id=memory.project_id,
                    team_id=memory.team_id,
                    policy=resolved.policy,
                    request_id=f'memory-indexer:{version.id}:embedding',
                    trace_id=f'memory-indexer:{version.id}',
                    text=document.full_text,
                ),
            )
        except ModelPolicyError:
            return
        except ProviderSecretError as error:
            logger.warning(
                'embedding skipped: provider secret unavailable',
                organization_id=str(memory.organization_id),
                project_id=str(memory.project_id),
                memory_version_id=str(version.id),
                error=str(error),
            )

            return

        document.embedding_vector = list(result.embedding)
        document.embedding_reference = f'provider:{result.call_record_id}'
        document.save(update_fields=['embedding_vector', 'embedding_reference', 'updated_at'])
```

Add `Memory` to the existing `from engram.core.models import (...)` import list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_worker_tests.py -v"`
Expected: PASS, including the four embedding lifecycle tests and the existing
worker tests.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/engram/context/services.py apps/backend/engram/memory/memory_worker_tests.py
git commit -m "feat: index memory version embeddings"
```

```json:metadata
{"files": ["apps/backend/engram/context/services.py", "apps/backend/engram/memory/memory_worker_tests.py"], "verifyCommand": "docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \"poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_worker_tests.py -v\"", "acceptanceCriteria": ["IndexMemoryVersion writes non-empty 64-dim embedding_vector when embedding policy exists", "re-index reuses same vector and same provider call record", "missing embedding policy leaves embedding_vector empty and indexing succeeds", "disabled embedding secret leaves embedding_vector empty, logs warning, indexing succeeds", "no raw secret-shaped value reaches vector or document fields"], "modelTier": "standard"}
```

---

### Task 4: BuildContextBundle Semantic Fallback

**Goal:** When exact matching returns fewer items than the requested limit and an
embeddings policy resolves, append cosine-similarity matches as a lower-score
band. Exact matches stay authoritative and unchanged. `retrieval_strategy`
metadata reflects the strategy used.

**Files:**

- Modify: `apps/backend/engram/context/services.py` (`BuildContextBundle`,
  module helpers)
- Test: `apps/backend/engram/context/context_api_tests.py`

**Acceptance Criteria:**

- [ ] When exact matches fill the limit, `retrieval_strategy` is `'exact'` and
  no embedding provider call is made for the query.
- [ ] When exact matches are fewer than the limit and a semantic candidate
  exists above `SEMANTIC_MIN_SIMILARITY`, the bundle includes the semantic
  match with `score == 30`, an `inclusion_reason` starting with
  `semantic match: cosine`, and `retrieval_strategy` is `'semantic_fallback'`.
- [ ] Semantic candidates come only from the authorized document set; a document
  outside the effective team scope never enters the bundle through the semantic
  path.
- [ ] When no embeddings policy resolves, the fallback is skipped and the bundle
  behaves exactly like today (`retrieval_strategy` is `'exact'`).
- [ ] Replay with the same `request_id` returns the same selected documents and
  the same `retrieval_strategy`.
- [ ] The `MemoryRetrieved` audit carries `retrieval_strategy`, and
  `semantic_provider_call_id` when the fallback activates.

**Verify:**
`docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/context/context_api_tests.py -v"`

**Steps:**

- [ ] **Step 1: Add a `create_embedding_policy` helper and failing tests**

In `apps/backend/engram/context/context_api_tests.py`, add a helper near
`create_scoped_api_key`:

```python
def create_embedding_policy(
    organization: Organization,
    team: Team,
    project: Project,
) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team Embedding OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-embedding-secret',
        hmac_digest='embedding-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Embedding policy',
        scope='project',
        task_type='embedding',
        provider='openai',
        model='text-embedding-3-small',
        secret=secret,
        version=1,
    )
```

Add the needed imports: `ProviderSecret`, `ProviderSecretEnvelope`,
`ModelPolicy` from `engram.model_policy.models`, plus `IndexMemoryVersion`,
`IndexMemoryVersionInput` from `engram.context.services`.

Add these tests:

```python
@pytest.mark.django_db
def test_context_bundle_returns_semantic_fallback_when_exact_misses() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title='Colour behaviour optimisation',
        body='Colour behaviour optimisation pattern for retrieval fallback.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='hash-semantic-1',
    )
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='color behavior optimization',
            file_paths=[],
            symbols=[],
            limit=5,
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['metadata']['retrieval_strategy'] == 'semantic_fallback'
    items = body['items']
    assert len(items) == 1
    assert items[0]['inclusion_reason'].startswith('semantic match: cosine')
    assert body['metadata']['semantic_provider_call_id']


@pytest.mark.django_db
def test_context_bundle_keeps_exact_strategy_when_limit_filled() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title='Authorization before ranking protects context bundles',
        body='Use scope evidence before retrieval and packing.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='hash-exact-1',
    )
    document = IndexMemoryVersion().execute(
        IndexMemoryVersionInput(memory_version_id=version.id),
    ).retrieval_document

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='authorization',
            file_paths=[document.file_paths[0]] if document.file_paths else [],
            symbols=[],
            limit=5,
        ),
        format='json',
        **auth_headers(),
    )

    body = response.json()
    assert body['metadata']['retrieval_strategy'] == 'exact'
    assert 'semantic_provider_call_id' not in body['metadata']


@pytest.mark.django_db
def test_context_bundle_skips_semantic_fallback_without_embedding_policy() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title='Authorization before ranking protects context bundles',
        body='Use scope evidence before retrieval and packing.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='hash-no-embedding-policy-1',
    )
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='authorization ranking context',
            file_paths=[],
            symbols=[],
            limit=5,
        ),
        format='json',
        **auth_headers(),
    )

    body = response.json()
    assert body['metadata']['retrieval_strategy'] == 'exact'
    assert body['items'] == []
```

Note: `valid_context_payload` already accepts `**overrides`; if it does not
accept `query`, `file_paths`, `symbols`, `limit` overrides yet, extend it to
forward those keys into the payload it returns.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/context/context_api_tests.py -v"`
Expected: the semantic-fallback test FAILS because `retrieval_strategy` is the
hard-coded `'exact'` and no semantic item is returned.

- [ ] **Step 3: Add the cosine helper and constants**

In `apps/backend/engram/context/services.py`, near the other module helpers,
add:

```python
import math

SEMANTIC_MIN_SIMILARITY = 0.3


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):

        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:

        return 0.0

    return dot / (left_norm * right_norm)
```

- [ ] **Step 4: Extend `BuildContextBundle` with the fallback**

In `apps/backend/engram/context/services.py`, update
`BuildContextBundle.execute` so it computes a query embedding, passes its vector
to `_rank_matches`, and records `retrieval_strategy` and
`semantic_provider_call_id` in bundle metadata. Replace the block that builds
`matches` and `metadata`:

```python
        matches = self._rank_matches(
            self._authorized_documents(organization, project, scope),
            data,
        )
        query_result = redact_value(data.query)
        metadata = {'retrieval_strategy': 'exact'}
        if query_result.redacted:
            metadata['redaction'] = {'query_text': True}
```

with:

```python
        authorized_documents = self._authorized_documents(organization, project, scope)
        embedding_result = self._resolve_query_embedding(data, organization, project, team)
        query_vector = list(embedding_result.embedding) if embedding_result is not None else None
        matches, has_semantic = self._rank_matches(authorized_documents, data, query_vector)
        query_result = redact_value(data.query)
        metadata = {'retrieval_strategy': 'semantic_fallback' if has_semantic else 'exact'}
        if query_result.redacted:
            metadata['redaction'] = {'query_text': True}
        if has_semantic and embedding_result is not None:
            metadata['semantic_provider_call_id'] = str(embedding_result.call_record_id)
```

Replace the body of `_rank_matches` so it collects exact matches first, then
adds semantic matches when below the limit:

```python
    def _rank_matches(
        self,
        documents: tuple[RetrievalDocument, ...],
        data: ContextBundleInput,
        query_vector: list[float] | None,
    ) -> tuple[tuple[RetrievalMatch, ...], bool]:
        has_request_terms = bool(data.query.strip() or data.file_paths or data.symbols)
        exact_matches: list[RetrievalMatch] = []
        for document in documents:
            match = self._score_document(document, data, has_request_terms)
            if match is not None:
                exact_matches.append(match)
        exact_matches.sort(
            key=lambda match: (
                -match.score,
                -match.document.updated_at.timestamp(),
                match.document.memory.title.casefold(),
                str(match.document.id),
            ),
        )
        if len(exact_matches) >= data.limit or query_vector is None:

            return tuple(exact_matches[: data.limit]), False

        semantic_matches = self._semantic_matches(documents, exact_matches, query_vector)

        return tuple((exact_matches + list(semantic_matches))[: data.limit]), bool(semantic_matches)
```

Add the semantic scoring and query-embedding methods:

```python
    def _semantic_matches(
        self,
        documents: tuple[RetrievalDocument, ...],
        exact_matches: list[RetrievalMatch],
        query_vector: list[float],
    ) -> list[RetrievalMatch]:
        already_matched = {match.document.id for match in exact_matches}
        scored: list[tuple[float, RetrievalMatch]] = []
        for document in documents:
            if document.id in already_matched or not document.embedding_vector:

                continue
            similarity = cosine_similarity(query_vector, list(document.embedding_vector))
            if similarity < SEMANTIC_MIN_SIMILARITY:

                continue
            scored.append(
                (
                    similarity,
                    RetrievalMatch(
                        document=document,
                        score=30,
                        matched_terms=(f'cosine {similarity:.2f}',),
                        inclusion_reason=f'semantic match: cosine {similarity:.2f}',
                    ),
                ),
            )
        scored.sort(key=lambda item: -item[0])

        return [match for _similarity, match in scored]

    def _resolve_query_embedding(
        self,
        data: ContextBundleInput,
        organization: Organization,
        project: Project,
        team: Team | None,
    ) -> EmbeddingCallResult | None:
        try:
            resolved = ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=organization.id,
                    project_id=project.id,
                    team_id=team.id if team is not None else None,
                    task_type='embedding',
                ),
            )
            result = FakeProviderGateway().embed(
                EmbeddingCallInput(
                    organization_id=organization.id,
                    project_id=project.id,
                    team_id=team.id if team is not None else None,
                    policy=resolved.policy,
                    request_id=data.request_id,
                    trace_id=data.trace_id or data.request_id,
                    text='\n'.join([data.query, *data.file_paths, *data.symbols]),
                ),
            )
        except ModelPolicyError:

            return None
        except ProviderSecretError as error:
            logger.warning(
                'context query embedding skipped: provider secret unavailable',
                organization_id=str(organization.id),
                project_id=str(project.id),
                request_id=data.request_id,
                error=str(error),
            )

            return None

        return result
```

Add `EmbeddingCallResult` to the existing model-policy import in this file.

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/context/context_api_tests.py -v"`
Expected: PASS, including the three new fallback tests and the existing exact
retrieval tests.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/engram/context/services.py apps/backend/engram/context/context_api_tests.py
git commit -m "feat: add semantic retrieval fallback to context bundle"
```

```json:metadata
{"files": ["apps/backend/engram/context/services.py", "apps/backend/engram/context/context_api_tests.py"], "verifyCommand": "docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \"poetry install --no-interaction --no-root --with dev && pytest engram/context/context_api_tests.py -v\"", "acceptanceCriteria": ["exact matches fill limit -> retrieval_strategy exact, no query embedding call", "exact misses + semantic candidate above threshold -> semantic_fallback item score 30 with cosine inclusion reason", "semantic candidates come only from authorized document set", "no embedding policy -> fallback skipped, retrieval_strategy exact", "replay same request_id returns same selection and strategy", "MemoryRetrieved audit carries retrieval_strategy and semantic_provider_call_id when fallback active"], "modelTier": "standard"}
```

---

### Task 5: Golden Path Embeddings Policy

**Goal:** `engram_bootstrap_golden_path` creates a project-scoped embeddings
policy bound to the golden-path OpenAI secret so the local golden path
exercises the embeddings resolution path without real provider calls.

**Files:**

- Modify: `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
- Test: `apps/backend/engram/core/golden_path_tests.py`

**Acceptance Criteria:**

- [ ] `bootstrap_golden_path` creates exactly one `ModelPolicy` with
  `task_type='embedding'`, `provider='openai'`, `model='text-embedding-3-small'`,
  `scope='project'`, bound to the golden-path OpenAI secret.
- [ ] The command result gains `embedding_policy_id`.
- [ ] The bootstrap remains idempotent: a second run produces the same ids and
  no duplicate policy.
- [ ] The raw golden-path provider secret never appears in the command output.

**Verify:**
`docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/core/golden_path_tests.py -v"`

**Steps:**

- [ ] **Step 1: Write the failing test**

In `apps/backend/engram/core/golden_path_tests.py`, extend the first test's
expected dict and assertions. Add `embedding_policy_id` to the expected body and
assert the policy exists:

```python
    embedding_policy = ModelPolicy.objects.get(
        organization=organization,
        team=team,
        project=project,
        task_type='embedding',
    )
```

Add `embedding_policy_id` to the asserted `body == {...}` dict:

```python
        'embedding_policy_id': str(embedding_policy.id),
```

Also assert the embeddings policy is bound to the golden-path OpenAI secret and
has the expected model:

```python
    assert embedding_policy.provider == 'openai'
    assert embedding_policy.model == 'text-embedding-3-small'
    assert embedding_policy.secret_id == secret.id
```

In the idempotency test, update the count assertion to expect two policies:

```python
    assert ModelPolicy.objects.count() == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/core/golden_path_tests.py -v"`
Expected: FAIL with `ModelPolicy.DoesNotExist` for the embeddings policy.

- [ ] **Step 3: Add the embeddings policy to the bootstrap**

In `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`,
after the `generation_policy` is created, add:

```python
        embedding_policy, _created = ModelPolicy.objects.update_or_create(
            organization=organization,
            team=team,
            project=project,
            task_type='embedding',
            scope='project',
            defaults={
                'name': 'Golden path embeddings',
                'provider': 'openai',
                'model': 'text-embedding-3-small',
                'secret': provider_secret,
                'version': 1,
                'active': True,
            },
        )
```

Add `embedding_policy_id` to the returned dict:

```python
            'embedding_policy_id': str(embedding_policy.id),
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/core/golden_path_tests.py -v"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py apps/backend/engram/core/golden_path_tests.py
git commit -m "feat: add golden path embeddings policy"
```

```json:metadata
{"files": ["apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py", "apps/backend/engram/core/golden_path_tests.py"], "verifyCommand": "docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \"poetry install --no-interaction --no-root --with dev && pytest engram/core/golden_path_tests.py -v\"", "acceptanceCriteria": ["bootstrap creates one project-scoped embeddings policy bound to golden secret", "command result includes embedding_policy_id", "bootstrap idempotent: second run same ids, no duplicate policy", "raw golden secret absent from command output"], "modelTier": "mechanical"}
```

---

### Task 6: Evidence And Verification

**Goal:** Run the full verification matrix, confirm the unchanged exact E2E
golden path still passes, update the verification matrix, and record the
focused security review.

**Files:**

- Modify: `docs/verification-matrix.md`
- Create: `docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md`

**Acceptance Criteria:**

- [ ] Full backend suite passes inside Compose: `pytest -v`, `ruff check .`,
  `ruff format --check .`.
- [ ] Migration apply plus `makemigrations --check --dry-run` is clean.
- [ ] `scripts/e2e_golden_path.py` still exits 0 on its unchanged exact path.
- [ ] Repository checks pass: `python3 -m unittest discover -s tests -v`,
  `python3 scripts/repository_layout.py`, `python3 scripts/repository_quality.py`,
  `git diff --check HEAD`.
- [ ] `docs/verification-matrix.md` has a dated `2026-06-26: Semantic Retrieval
  Foundation` section with commands, statuses, and first decisive failures.
- [ ] `docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md` records
  scope, findings, fixes, and accepted risks.

**Verify:**
`docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` and `python3 scripts/e2e_golden_path.py`

**Steps:**

- [ ] **Step 1: Run the full backend gate**

Run:
`docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."`
Expected: exit 0; record the pytest count, lint result, and any first decisive
failure. If Docker is unavailable in the WSL distro, fall back to
`cd apps/backend && poetry run pytest -v && poetry run ruff check . && poetry run ruff format --check .` with `settings.test_settings`, and record why Compose could not run.

- [ ] **Step 2: Run migration freshness**

Run:
`docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py makemigrations --check --dry-run"`
Expected: `No changes detected`.

- [ ] **Step 3: Run the Compose golden path**

Run: `python3 scripts/e2e_golden_path.py`
Expected: exit 0 on the unchanged exact fixture.

- [ ] **Step 4: Run repository checks**

Run:
`python3 -m unittest discover -s tests -v`; `python3 scripts/repository_layout.py`; `python3 scripts/repository_quality.py`; `git diff --check HEAD`
Expected: all exit 0.

- [ ] **Step 5: Write the verification matrix entry**

Append a `## 2026-06-26: Semantic Retrieval Foundation` section to
`docs/verification-matrix.md` with branch, scope, the command table, first
decisive failures, and security evidence pointer, following the existing entry
shape.

- [ ] **Step 6: Write the security review**

Create `docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md`
covering: tenant isolation on the semantic path, redaction of embedding input,
idempotency of document and query embeddings, graceful degradation, audit
evidence, and accepted risks (fake deterministic embeddings are not real
semantic similarity; pgvector swap deferred).

- [ ] **Step 7: Commit**

```bash
git add docs/verification-matrix.md docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md
git commit -m "docs: record semantic retrieval foundation evidence"
```

```json:metadata
{"files": ["docs/verification-matrix.md", "docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md"], "verifyCommand": "docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec \"poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check .\" && python3 scripts/e2e_golden_path.py", "acceptanceCriteria": ["full backend pytest + ruff check + ruff format pass in Compose", "migration apply + makemigrations --check --dry-run clean", "e2e_golden_path.py exits 0 on unchanged exact fixture", "repository unittest + layout + quality + whitespace pass", "verification matrix entry dated 2026-06-26 present", "security review artifact present with scope/findings/fixes/accepted risks"], "modelTier": "mechanical"}
```
