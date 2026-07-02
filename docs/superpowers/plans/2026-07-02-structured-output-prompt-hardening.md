# Provider Structured Output + Prompt Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce JSON output at the provider layer (OpenAI/DeepSeek `response_format`, Anthropic forced tool_use + per-kind `max_tokens`) and harden the four memory-pipeline prompts (session distillation contract `{"memories": [...]}` with confidence rubric, per-observation SKIP protocol + richer fields, curation judge `reason`).

**Architecture:** All LLM calls flow through `apps/backend/engram/model_policy/services.py` gateways keyed by `ProviderCallInput.response_kind` (`single` | `candidates` | `curation_judgment`). Structured-output enforcement lives entirely inside the gateways (no caller interface change). The session-distillation output contract changes from a bare JSON array to `{"memories": [...]}` — prompt, fake gateway, and parser change together; the parser stays tolerant of the legacy bare array. Per-observation distillation gains a SKIP protocol: the model answers `SKIP`, `ProcessObservationRecorded` then creates no candidate.

**Tech Stack:** Django 5 + pytest-django, pgvector pg18 test DB in docker (`engram-testpg` on `engram-net`), python:3.12-slim tester container, poetry.

**User decisions (already made):** user delegated all architecture-layer decisions ("Ты fable - сам решай что делать на архитектурном слое"); direction approved from the prompt-comparison assessment (JSON mode + prompt improvements vs claude-mem).

**Environment constraints:**
- Work ONLY in the worktree `/mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so` (branch `feat/provider-structured-output`, based on `origin/master` @ `fa019739`). The main checkout is owned by a parallel agent editing `distillation.py`/`services.py` (chunked distillation WIP) — do not touch it.
- Commit with `--no-verify` (repo hooksPath points at a gitignored `.githooks` that is broken in worktrees).
- Run tests only inside docker: tester container `engram-tester-so` (created in Task 0).
- Known merge-risk: the parallel chunking WIP touches `session_distillation_prompt`/`_synthesize`. Keep changes to those functions minimal (we only change `session_distillation_system_prompt`, `parse_synthesized_candidates`, `_fallback_candidate` usage — not `_synthesize`).

---

### Task 0: Test environment for the worktree

**Goal:** A tester container mounted on the worktree backend, its own test database, and a green baseline for the affected test modules.

**Files:** none (infrastructure only, no commit)

**Acceptance Criteria:**
- [ ] `engram-tester-so` container runs on `engram-net` with the worktree's `apps/backend` mounted at `/app`
- [ ] Baseline run of the three affected test modules passes

**Verify:** `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py engram/memory/distillation_tests.py engram/memory/curation_tests.py -q` → all pass

**Steps:**

- [ ] **Step 1: Create tester container + database**

```bash
docker rm -f engram-tester-so 2>/dev/null
docker run -d --name engram-tester-so --network engram-net \
  -v /mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so/apps/backend:/app -w /app \
  -e ENGRAM_DATABASE_URL=postgresql://engram:engram@engram-testpg:5432/engram_so \
  python:3.12-slim sleep infinity
docker exec engram-testpg psql -U engram -c 'DROP DATABASE IF EXISTS engram_so' -c 'CREATE DATABASE engram_so'
docker exec engram-tester-so bash -c 'pip install poetry==1.8.2 -q && poetry config virtualenvs.create false && poetry install --no-interaction -q'
```

- [ ] **Step 2: Baseline test run**

Run: `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py engram/memory/distillation_tests.py engram/memory/curation_tests.py -q`
Expected: PASS (0 failures). If baseline fails, STOP and report — do not proceed on a red base.

---

### Task 1: Session distillation contract `{"memories": [...]}` + confidence rubric + tolerant parser + fake gateway

**Goal:** The session-distillation prompt asks for a single JSON object `{"memories": [...]}` with a confidence rubric and explicit empty-permission; `parse_synthesized_candidates` accepts the object form, the legacy bare array, and treats a valid-but-empty result as "no durable memories" (no fallback garbage candidate); the fake gateway emits the new shape.

**Files:**
- Modify: `apps/backend/engram/memory/distillation.py` (`session_distillation_system_prompt` ~line 77, `parse_synthesized_candidates` ~line 145)
- Modify: `apps/backend/engram/model_policy/services.py` (`generated_candidates_payload` ~line 800)
- Test: `apps/backend/engram/memory/distillation_tests.py`

**Acceptance Criteria:**
- [ ] `parse_synthesized_candidates('{"memories": [...]}')` returns the candidates
- [ ] `parse_synthesized_candidates('{"memories": []}')` returns `()` (empty tuple, no fallback candidate)
- [ ] `parse_synthesized_candidates('[]')` returns `()`
- [ ] Legacy bare array `'[{...}]'` still parses (existing tests keep passing)
- [ ] `parse_synthesized_candidates('not json at all')` still returns the fallback candidate
- [ ] `parse_synthesized_candidates('{"other": 1}')` returns the fallback candidate
- [ ] System prompt mentions: single JSON object, key `"memories"`, empty-array permission, confidence rubric anchors, good/bad example
- [ ] `generated_candidates_payload` returns `{"memories": [...]}` JSON and fake-mode distillation e2e still passes
- [ ] `DistillSession.execute` with zero synthesized candidates returns an empty result without error

**Verify:** `docker exec engram-tester-so pytest engram/memory/distillation_tests.py engram/model_policy/services_tests.py -q` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `apps/backend/engram/memory/distillation_tests.py`:

```python
def test_parse_synthesized_candidates_reads_memories_object() -> None:
    raw = json.dumps(
        {
            'memories': [
                {
                    'title': 'Retry queue drops messages on Redis restart',
                    'body': 'Consumer acks before processing in worker/queue.py.',
                    'confidence': 0.9,
                    'supporting_observation_ids': ['obs-1'],
                },
            ],
        },
    )

    candidates = parse_synthesized_candidates(raw)

    assert len(candidates) == 1
    assert candidates[0].title == 'Retry queue drops messages on Redis restart'
    assert candidates[0].confidence == Decimal('0.900')
    assert candidates[0].supporting_observation_ids == ('obs-1',)


def test_parse_synthesized_candidates_empty_memories_means_no_candidates() -> None:
    assert parse_synthesized_candidates('{"memories": []}') == ()
    assert parse_synthesized_candidates('[]') == ()


def test_parse_synthesized_candidates_object_without_memories_falls_back() -> None:
    candidates = parse_synthesized_candidates('{"other": 1}')

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.500')


def test_session_distillation_system_prompt_declares_memories_object_contract() -> None:
    prompt = session_distillation_system_prompt()

    assert '"memories"' in prompt
    assert '{"memories": []}' in prompt
    assert '0.9' in prompt
```

Add `session_distillation_system_prompt` to the existing import block from `engram.memory.distillation` and `Decimal`/`json` imports if missing (both are already imported in this file — check first).

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec engram-tester-so pytest engram/memory/distillation_tests.py -q -k 'memories_object or empty_memories or without_memories or declares_memories'`
Expected: FAIL (contract not implemented yet)

- [ ] **Step 3: Implement in `apps/backend/engram/memory/distillation.py`**

Replace `session_distillation_system_prompt` with:

```python
def session_distillation_system_prompt() -> str:
    return (
        'You are a session distillation engine for software engineering sessions.\n'
        'Given structured observations from one agent session, synthesize durable, '
        'runtime-neutral engineering memories.\n'
        '\n'
        'Rules:\n'
        '- Output a single JSON object only, with exactly one key "memories".\n'
        '- "memories" is an array of objects with the keys '
        '"title", "body", "confidence", "supporting_observation_ids".\n'
        '- If the session contains no durable engineering signal, output {"memories": []}.\n'
        '- "confidence" is a number between 0 and 1: 0.9 or higher for verified facts with direct '
        'evidence (a fix confirmed by tests, an observed error with its cause), 0.6-0.8 for plausible '
        'conclusions consistent with the observations, 0.3-0.5 for unverified hypotheses, below 0.3 '
        'for speculation.\n'
        '- "supporting_observation_ids" lists the observation ids the memory is derived from.\n'
        '- Consolidate related observations into a small number of high-signal memories.\n'
        '- Preserve exact identifiers verbatim: file paths, function names, class names, '
        'CLI commands, error strings, ticket identifiers, URLs, and config keys.\n'
        '- Drop session chatter, acknowledgements, timestamps, and credential-shaped values.\n'
        '- Do not invent facts not present in the input.\n'
        '- Do not name any AI assistant, tool, or product by brand.\n'
        '\n'
        'Good memory: {"title": "Retry queue drops messages on Redis restart", '
        '"body": "The consumer in worker/queue.py acknowledges messages before processing; '
        'a Redis restart during processing loses them. Fixed by acknowledging after processing.", '
        '"confidence": 0.9, "supporting_observation_ids": ["<id>"]}\n'
        'Bad memory (never produce): {"title": "Worked on the queue", '
        '"body": "Investigated some queue issues and made progress."} '
        '- vague, no identifiers, not durable.'
    )
```

Replace `parse_synthesized_candidates` with (keep `_fallback_candidate` and `_clamp_confidence` as they are):

```python
def parse_synthesized_candidates(raw_body: str) -> tuple[SynthesizedCandidate, ...]:
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return (_fallback_candidate(raw_body),)

    if isinstance(parsed, dict):
        items = parsed.get('memories')
        if not isinstance(items, list):
            return (_fallback_candidate(raw_body),)
    elif isinstance(parsed, list):
        items = parsed
    else:
        return (_fallback_candidate(raw_body),)

    candidates: list[SynthesizedCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get('title') or '').strip()
        body = str(item.get('body') or '').strip()
        if not title and not body:
            continue
        supporting = tuple(str(value) for value in (item.get('supporting_observation_ids') or []))
        candidates.append(
            SynthesizedCandidate(
                title=(title or body)[:255],
                body=body or title,
                confidence=_clamp_confidence(item.get('confidence')),
                supporting_observation_ids=supporting,
            ),
        )

    return tuple(candidates)
```

Note the behavior change: a syntactically valid container (object with `memories` list, or bare list) that yields zero items now returns `()` instead of a fallback candidate. Only unparseable/blob output falls back.

In `apps/backend/engram/model_policy/services.py` replace the return of `generated_candidates_payload` (~line 817):

```python
    return json.dumps({'memories': candidates})
```

- [ ] **Step 4: Run the full affected modules**

Run: `docker exec engram-tester-so pytest engram/memory/distillation_tests.py engram/model_policy/services_tests.py engram/memory/tasks_tests.py -q`
Expected: PASS. If an existing test asserts the old bare-array fake payload or the old empty-list fallback, update that test's expectation to the new contract (it is a deliberate contract change), and say so in the commit message.

- [ ] **Step 5: Commit**

```bash
cd /mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so
git add apps/backend/engram/memory/distillation.py apps/backend/engram/memory/distillation_tests.py apps/backend/engram/model_policy/services.py
git commit --no-verify -m 'feat: session distillation memories-object contract with confidence rubric'
```

```json:metadata
{"files": ["apps/backend/engram/memory/distillation.py", "apps/backend/engram/model_policy/services.py", "apps/backend/engram/memory/distillation_tests.py"], "verifyCommand": "docker exec engram-tester-so pytest engram/memory/distillation_tests.py engram/model_policy/services_tests.py -q", "acceptanceCriteria": ["memories-object parses", "empty memories -> ()", "bare array still parses", "garbage -> fallback", "fake gateway emits memories object"], "modelTier": "mechanical"}
```

---

### Task 2: OpenAI-compatible gateway sends `response_format: json_object` for structured kinds

**Goal:** `OpenAICompatibleGateway._chat_completion` adds `response_format={'type': 'json_object'}` when the call's `response_kind` is `candidates` or `curation_judgment`; `single` calls are unchanged.

**Files:**
- Modify: `apps/backend/engram/model_policy/services.py` (`OpenAICompatibleGateway.call` ~line 868, `_chat_completion` ~line 982)
- Test: `apps/backend/engram/model_policy/real_provider_tests.py`

**Acceptance Criteria:**
- [ ] Request payload for `response_kind='candidates'` contains `"response_format": {"type": "json_object"}`
- [ ] Request payload for `response_kind='curation_judgment'` contains the same
- [ ] Request payload for default (`single`) calls contains NO `response_format` key
- [ ] DeepSeek thinking override still applied (extra dict merge preserved)

**Verify:** `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `apps/backend/engram/model_policy/real_provider_tests.py` (reuse existing `_opener_returning` / `make_real_policy` helpers):

```python
@pytest.mark.django_db
def test_openai_gateway_sends_json_mode_for_candidates() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation')
    completion = {'choices': [{'message': {'content': '{"memories": []}'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-1',
            trace_id='json-mode-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['response_format'] == {'type': 'json_object'}


@pytest.mark.django_db
def test_openai_gateway_sends_json_mode_for_curation_judgment() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation')
    completion = {'choices': [{'message': {'content': '{"decision": "keep_both"}'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-2',
            trace_id='json-mode-2',
            prompt='prompt text',
            response_kind='curation_judgment',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['response_format'] == {'type': 'json_object'}


@pytest.mark.django_db
def test_openai_gateway_omits_json_mode_for_single() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {'choices': [{'message': {'content': 'Title\nBody'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-3',
            trace_id='json-mode-3',
            prompt='prompt text',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'response_format' not in sent_body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q -k json_mode`
Expected: FAIL (`response_format` absent)

- [ ] **Step 3: Implement in `apps/backend/engram/model_policy/services.py`**

Add a module-level constant + helper near `deepseek_thinking_override` (~line 848):

```python
_STRUCTURED_RESPONSE_KINDS = frozenset({'candidates', 'curation_judgment'})


def structured_response_format(response_kind: str) -> dict[str, object]:
    if response_kind in _STRUCTURED_RESPONSE_KINDS:
        return {'response_format': {'type': 'json_object'}}

    return {}
```

In `OpenAICompatibleGateway.call` (~line 886), merge it into `extra`:

```python
        extra: dict[str, object] = {}
        extra.update(deepseek_thinking_override(policy.provider, policy.task_type))
        extra.update(structured_response_format(data.response_kind))
        content = self._chat_completion(
            policy.model,
            prompt_text,
            system_prompt=data.system_prompt,
            extra=extra,
        )
```

`_chat_completion` itself is unchanged (it already merges `extra` into the payload).

- [ ] **Step 4: Run test module**

Run: `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so
git add apps/backend/engram/model_policy/services.py apps/backend/engram/model_policy/real_provider_tests.py
git commit --no-verify -m 'feat: openai-compatible gateway json mode for structured response kinds'
```

```json:metadata
{"files": ["apps/backend/engram/model_policy/services.py", "apps/backend/engram/model_policy/real_provider_tests.py"], "verifyCommand": "docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q", "acceptanceCriteria": ["candidates/judgment requests carry response_format json_object", "single requests do not", "deepseek thinking override preserved"], "modelTier": "mechanical"}
```

---

### Task 3: Anthropic gateway forced tool_use + per-kind max_tokens

**Goal:** `AnthropicMessagesGateway` forces a tool call with a per-kind JSON schema for structured kinds (serializing the tool input back to a JSON string so callers are unchanged) and resolves `max_tokens` per response kind with a `ModelPolicy.metadata['max_tokens']` override (fixes the hardcoded 1024 truncation risk for session distillation).

**Files:**
- Modify: `apps/backend/engram/model_policy/services.py` (`AnthropicMessagesGateway.call` ~line 1094, `_messages` ~line 1179)
- Test: `apps/backend/engram/model_policy/real_provider_tests.py`

**Acceptance Criteria:**
- [ ] `candidates` request payload carries `tools=[...]` with an `input_schema` requiring `memories`, and `tool_choice={'type': 'tool', 'name': 'emit_memories'}`
- [ ] `curation_judgment` request payload carries the `emit_judgment` tool with a `decision` enum and forced tool_choice
- [ ] `single` request payload has no `tools`/`tool_choice`
- [ ] A `tool_use` response block is returned to callers as its JSON-serialized `input`
- [ ] `max_tokens`: 1024 for `single`, 8192 for `candidates`, 1024 for `curation_judgment`; `policy.metadata['max_tokens']` overrides all kinds
- [ ] A text-only response for a structured kind still returns the text (defensive fallback)

**Verify:** `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `apps/backend/engram/model_policy/real_provider_tests.py`:

```python
@pytest.mark.django_db
def test_anthropic_gateway_forces_tool_for_candidates() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {
        'content': [
            {
                'type': 'tool_use',
                'name': 'emit_memories',
                'input': {'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9}]},
            },
        ],
    }
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-1',
            trace_id='anthropic-tool-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['tool_choice'] == {'type': 'tool', 'name': 'emit_memories'}
    assert sent_body['tools'][0]['name'] == 'emit_memories'
    assert sent_body['tools'][0]['input_schema']['required'] == ['memories']
    assert sent_body['max_tokens'] == 8192
    assert json.loads(result.generated_body) == {'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9}]}


@pytest.mark.django_db
def test_anthropic_gateway_forces_tool_for_curation_judgment() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {
        'content': [
            {'type': 'tool_use', 'name': 'emit_judgment', 'input': {'decision': 'merge', 'reason': 'same fact'}},
        ],
    }
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-2',
            trace_id='anthropic-tool-2',
            prompt='prompt text',
            response_kind='curation_judgment',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['tool_choice'] == {'type': 'tool', 'name': 'emit_judgment'}
    assert sent_body['tools'][0]['input_schema']['properties']['decision']['enum'] == [
        'merge',
        'keep_both',
        'reject',
    ]
    assert sent_body['max_tokens'] == 1024
    assert json.loads(result.generated_body) == {'decision': 'merge', 'reason': 'same fact'}


@pytest.mark.django_db
def test_anthropic_gateway_single_kind_has_no_tools_and_default_budget() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {'content': [{'type': 'text', 'text': 'Title\nBody'}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-3',
            trace_id='anthropic-tool-3',
            prompt='prompt text',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'tools' not in sent_body
    assert 'tool_choice' not in sent_body
    assert sent_body['max_tokens'] == 1024


@pytest.mark.django_db
def test_anthropic_gateway_max_tokens_metadata_override() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    policy.metadata = {**policy.metadata, 'max_tokens': 2048}
    policy.save(update_fields=['metadata'])
    message = {'content': [{'type': 'tool_use', 'name': 'emit_memories', 'input': {'memories': []}}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-4',
            trace_id='anthropic-tool-4',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['max_tokens'] == 2048


@pytest.mark.django_db
def test_anthropic_gateway_structured_kind_falls_back_to_text_block() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {'content': [{'type': 'text', 'text': '{"memories": []}'}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-5',
            trace_id='anthropic-tool-5',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    assert result.generated_body == '{"memories": []}'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q -k anthropic_gateway`
Expected: new tests FAIL (no tools/tool_choice, max_tokens fixed at 1024)

- [ ] **Step 3: Implement in `apps/backend/engram/model_policy/services.py`**

Add module-level definitions near `structured_response_format`:

```python
_DEFAULT_MAX_TOKENS = 1024
_MAX_TOKENS_BY_KIND = {'candidates': 8192}
_ANTHROPIC_STRUCTURED_TOOLS: dict[str, dict[str, object]] = {
    'candidates': {
        'name': 'emit_memories',
        'description': 'Return the synthesized engineering memories.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'memories': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'title': {'type': 'string'},
                            'body': {'type': 'string'},
                            'confidence': {'type': 'number'},
                            'supporting_observation_ids': {'type': 'array', 'items': {'type': 'string'}},
                        },
                        'required': ['title', 'body', 'confidence'],
                    },
                },
            },
            'required': ['memories'],
        },
    },
    'curation_judgment': {
        'name': 'emit_judgment',
        'description': 'Return the curation judgment.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'decision': {'type': 'string', 'enum': ['merge', 'keep_both', 'reject']},
                'reason': {'type': 'string'},
            },
            'required': ['decision'],
        },
    },
}


def resolve_max_tokens(policy: ModelPolicy, response_kind: str) -> int:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    try:
        override = int(metadata.get('max_tokens'))
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return override

    return _MAX_TOKENS_BY_KIND.get(response_kind, _DEFAULT_MAX_TOKENS)
```

Rewrite `AnthropicMessagesGateway._messages` and the call site:

```python
    def _messages(
        self,
        model: str,
        prompt: str,
        system_prompt: str = '',
        *,
        response_kind: str = 'single',
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        payload_dict: dict[str, object] = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if system_prompt:
            payload_dict['system'] = system_prompt
        tool = _ANTHROPIC_STRUCTURED_TOOLS.get(response_kind)
        if tool is not None:
            payload_dict['tools'] = [tool]
            payload_dict['tool_choice'] = {'type': 'tool', 'name': tool['name']}
        payload = json.dumps(payload_dict).encode()
        response = self._open(self._base_url + '/v1/messages', payload)

        return _anthropic_content_text(response)
```

Add near `_split_completion`:

```python
def _anthropic_content_text(response: dict[str, Any]) -> str:
    blocks = response.get('content') or []
    for block in blocks:
        if isinstance(block, dict) and block.get('type') == 'tool_use':
            return json.dumps(block.get('input') or {})
    for block in blocks:
        if isinstance(block, dict) and block.get('type') == 'text':
            return str(block.get('text') or '')

    return str(blocks[0]['text']) if blocks else ''
```

Update the call site in `AnthropicMessagesGateway.call` (~line 1112):

```python
        content = self._messages(
            policy.model,
            prompt_text,
            system_prompt=data.system_prompt,
            response_kind=data.response_kind,
            max_tokens=resolve_max_tokens(policy, data.response_kind),
        )
```

Note: the existing `test_anthropic_gateway_call_parses_message` builds `{'content': [{'type': 'text', 'text': ...}]}` — the text-block loop in `_anthropic_content_text` keeps it passing; the final `blocks[0]['text']` line is defensive for typeless blocks only. Do not break the existing anthropic tests.

- [ ] **Step 4: Run test module**

Run: `docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q`
Expected: PASS (including pre-existing anthropic tests)

- [ ] **Step 5: Commit**

```bash
cd /mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so
git add apps/backend/engram/model_policy/services.py apps/backend/engram/model_policy/real_provider_tests.py
git commit --no-verify -m 'feat: anthropic gateway forced tool_use structured output and per-kind max_tokens'
```

```json:metadata
{"files": ["apps/backend/engram/model_policy/services.py", "apps/backend/engram/model_policy/real_provider_tests.py"], "verifyCommand": "docker exec engram-tester-so pytest engram/model_policy/real_provider_tests.py -q", "acceptanceCriteria": ["forced tool_use for structured kinds", "tool_use input serialized back to JSON string", "max_tokens 8192 candidates / 1024 default / metadata override", "text fallback preserved"], "modelTier": "standard"}
```

---

### Task 4: Per-observation distillation — richer prompt fields + SKIP protocol

**Goal:** `provider_prompt` includes `facts`/`narrative`/`concepts` (present on observations since #126 but currently dropped); the system prompt allows the model to answer `SKIP` for observations with no durable signal; `ProcessObservationRecorded` creates no candidate on SKIP and records an audit event.

**Files:**
- Modify: `apps/backend/engram/memory/services.py` (`MemoryCandidateWorkerResult` ~line 59, `ProcessObservationRecorded.execute` ~line 221, `distillation_system_prompt` ~line 465, `provider_prompt` ~line 483)
- Modify: `apps/backend/engram/memory/tasks.py` (`process_observation_recorded` return ~line 55)
- Test: `apps/backend/engram/memory/services_tests.py`

**Acceptance Criteria:**
- [ ] `provider_prompt` output contains `Facts:`, `Narrative:`, `Concepts:` lines (redacted like the others)
- [ ] `distillation_system_prompt` contains the SKIP rule
- [ ] When the provider returns `SKIP` as the whole completion (title `SKIP`, empty-or-`SKIP` body), `ProcessObservationRecorded.execute` creates NO `MemoryCandidate`, returns `skipped=True`, and writes an `AuditEvent` with `event_type='MemoryCandidateSkipped'`
- [ ] Non-SKIP flow is byte-for-byte unchanged (existing tests pass)
- [ ] Celery task returns `'skipped'` when no candidate was created

**Verify:** `docker exec engram-tester-so pytest engram/memory/services_tests.py engram/memory/tasks_tests.py engram/memory/memory_worker_tests.py -q` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `apps/backend/engram/memory/services_tests.py`. The module already imports `create_generation_policy` and `create_observation_recorded_scope` from `engram.memory.memory_worker_tests` and `FakeProviderGateway` from `engram.model_policy.services`; the gateway-substitution pattern in this file is `monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: _StubGateway())` (see `test_generate_candidate_provider_call_has_no_open_transaction` ~line 237). Test code:

```python
def test_provider_prompt_includes_facts_narrative_concepts() -> None:
    observation = Observation(
        title='T',
        body='B',
        facts=['fact one'],
        narrative='narrative text',
        concepts=['gotcha'],
        files_read=[],
        files_modified=[],
        source_metadata={},
    )

    prompt = provider_prompt(observation)

    assert 'Facts:' in prompt
    assert 'fact one' in prompt
    assert 'Narrative: narrative text' in prompt
    assert 'Concepts:' in prompt


def test_distillation_system_prompt_declares_skip_protocol() -> None:
    assert 'SKIP' in distillation_system_prompt()


@pytest.mark.django_db
def test_process_observation_skip_creates_no_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)

    class _SkipGateway(FakeProviderGateway):
        def call(self, data: object) -> ProviderCallResult:
            real = FakeProviderGateway.call(self, data)

            return ProviderCallResult(
                provider=real.provider,
                model=real.model,
                call_record_id=real.call_record_id,
                redaction_state=real.redaction_state,
                generated_title='SKIP',
                generated_body='',
            )

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: _SkipGateway())

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert result.candidate is None
    assert not MemoryCandidate.objects.filter(source_observation=observation).exists()
    assert AuditEvent.objects.filter(
        event_type='MemoryCandidateSkipped',
        target_id=str(observation.id),
    ).exists()
```

Extend the existing import blocks: from `engram.memory.services` also import `MemoryCandidateWorkerInput`, `ProcessObservationRecorded`, `distillation_system_prompt`, `provider_prompt`; from `engram.model_policy.services` also import `ProviderCallResult`; from `engram.core.models` import `AuditEvent`, `MemoryCandidate`, `Observation`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec engram-tester-so pytest engram/memory/services_tests.py -q -k 'skip or facts_narrative'`
Expected: FAIL (`skipped` field does not exist, prompt lacks fields)

- [ ] **Step 3: Implement in `apps/backend/engram/memory/services.py`**

`MemoryCandidateWorkerResult` (~line 59) — make candidate optional and add the flag:

```python
class MemoryCandidateWorkerResult:
    candidate: MemoryCandidate | None
    duplicate: bool
    memory: Memory | None = None
    memory_version: MemoryVersion | None = None
    retrieval_document: RetrievalDocument | None = None
    held_for_review: bool = False
    curated_decision: str = ''
    skipped: bool = False
```

`distillation_system_prompt` — add one rule line before the brand-neutrality line:

```python
        '- If the observation contains no durable engineering signal (routine status checks, empty '
        'search results, plain acknowledgements), output exactly SKIP as the entire response.\n'
```

`provider_prompt` — add the three lines after `Body:`:

```python
def provider_prompt(observation: Observation) -> str:
    return '\n'.join(
        [
            f'Title: {redact_text(observation.title)}',
            f'Body: {redact_text(observation.body)}',
            f'Facts: {redact_value(observation.facts)}',
            f'Narrative: {redact_text(observation.narrative)}',
            f'Concepts: {redact_value(observation.concepts)}',
            f'Files read: {redact_value(observation.files_read)}',
            f'Files modified: {redact_value(observation.files_modified)}',
            f'Source metadata: {redact_value(observation.source_metadata)}',
        ],
    )
```

`ProcessObservationRecorded.execute` — right after `generated = self._generate_candidate(...)` (~line 228):

```python
        if _is_skip(generated):
            self._audit_skipped(observation)
            logger.info(
                'memory_candidate_skipped',
                observation_id=str(observation.id),
            )

            return MemoryCandidateWorkerResult(candidate=None, duplicate=False, skipped=True)
```

Module-level helper + method (near the class):

```python
def _is_skip(generated: GeneratedMemoryCandidate) -> bool:
    title = generated.title.strip()
    body = generated.body.strip()

    return title.upper() == 'SKIP' and body.upper() in ('', 'SKIP')
```

```python
    def _audit_skipped(self, observation: Observation) -> None:
        AuditEvent.objects.create(
            organization=observation.organization,
            project=observation.project,
            team=observation.team,
            event_type='MemoryCandidateSkipped',
            actor_type='system',
            target_type='observation',
            target_id=str(observation.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            metadata={'reason': 'no_durable_signal'},
        )
```

In `apps/backend/engram/memory/tasks.py` `process_observation_recorded` — change the tail return:

```python
    if result.memory is not None:
        return str(result.memory.id)

    if result.candidate is None:
        return 'skipped'

    return str(result.candidate.id)
```

- [ ] **Step 4: Run affected modules**

Run: `docker exec engram-tester-so pytest engram/memory/services_tests.py engram/memory/tasks_tests.py engram/memory/memory_worker_tests.py -q`
Expected: PASS. If other modules reference `result.candidate.id` unconditionally, fix those call sites the same way (`candidate is None` guard).

- [ ] **Step 5: Commit**

```bash
cd /mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so
git add apps/backend/engram/memory/services.py apps/backend/engram/memory/tasks.py apps/backend/engram/memory/services_tests.py
git commit --no-verify -m 'feat: observation distillation skip protocol and richer prompt fields'
```

```json:metadata
{"files": ["apps/backend/engram/memory/services.py", "apps/backend/engram/memory/tasks.py", "apps/backend/engram/memory/services_tests.py"], "verifyCommand": "docker exec engram-tester-so pytest engram/memory/services_tests.py engram/memory/tasks_tests.py engram/memory/memory_worker_tests.py -q", "acceptanceCriteria": ["provider_prompt carries facts/narrative/concepts", "SKIP creates no candidate + audit event", "non-SKIP flow unchanged", "celery task returns skipped"], "modelTier": "standard"}
```

---

### Task 5: Curation judge asks for a reason and logs it

**Goal:** The judge prompt requests `{"decision": ..., "reason": ...}` (cheap chain-of-thought + audit trail); `_judge_decision` logs the reason; decision parsing semantics unchanged.

**Files:**
- Modify: `apps/backend/engram/memory/curation.py` (`curation_judge_system_prompt` ~line 92, `parse_curation_decision` area ~line 129, `_judge_decision` ~line 337)
- Test: `apps/backend/engram/memory/curation_tests.py`

**Acceptance Criteria:**
- [ ] System prompt requires keys `"decision"` and `"reason"`
- [ ] New `parse_curation_reason(raw_body)` returns the reason string, `''` on any parse failure
- [ ] `_judge_decision` emits `logger.info('curation_judge_decision', ...)` with `decision` and `reason`
- [ ] `parse_curation_decision` behavior unchanged (existing tests pass)

**Verify:** `docker exec engram-tester-so pytest engram/memory/curation_tests.py -q` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `apps/backend/engram/memory/curation_tests.py`:

```python
def test_curation_judge_system_prompt_requires_reason() -> None:
    prompt = curation_judge_system_prompt()

    assert '"reason"' in prompt
    assert '"decision"' in prompt


def test_parse_curation_reason_reads_reason() -> None:
    assert parse_curation_reason('{"decision": "merge", "reason": "same fact"}') == 'same fact'
    assert parse_curation_reason('{"decision": "merge"}') == ''
    assert parse_curation_reason('not json') == ''
    assert parse_curation_reason('[]') == ''
```

Add `parse_curation_reason` to the import block from `engram.memory.curation`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec engram-tester-so pytest engram/memory/curation_tests.py -q -k reason`
Expected: FAIL (ImportError on `parse_curation_reason`)

- [ ] **Step 3: Implement in `apps/backend/engram/memory/curation.py`**

`curation_judge_system_prompt` — replace the first rule line and add a reason rule:

```python
def curation_judge_system_prompt() -> str:
    return (
        'You are a memory curation judge for a software engineering memory store.\n'
        'You are given a new candidate memory and an existing near-duplicate memory.\n'
        'Decide how to reconcile them.\n'
        '\n'
        'Rules:\n'
        '- Output a single JSON object only, with exactly two keys "decision" and "reason".\n'
        '- "decision" is one of "merge", "keep_both", "reject".\n'
        '- "reason" is one short sentence explaining the decision.\n'
        '- "merge": the same durable fact; the new candidate should supersede the existing memory.\n'
        '- "keep_both": the two memories are distinct durable facts and both should be kept.\n'
        '- "reject": the new candidate adds no durable value beyond the existing memory.\n'
        '- Do not name any AI assistant, tool, or product by brand.'
    )
```

Add after `parse_curation_decision`:

```python
def parse_curation_reason(raw_body: str) -> str:
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return ''

    if not isinstance(parsed, dict):
        return ''

    return str(parsed.get('reason') or '').strip()
```

In `_judge_decision`, replace the final `return parse_curation_decision(result.generated_body)` with:

```python
        decision = parse_curation_decision(result.generated_body)
        logger.info(
            'curation_judge_decision',
            candidate_id=str(candidate.id),
            memory_id=str(memory.id),
            decision=decision,
            reason=parse_curation_reason(result.generated_body),
        )

        return decision
```

- [ ] **Step 4: Run test module**

Run: `docker exec engram-tester-so pytest engram/memory/curation_tests.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so
git add apps/backend/engram/memory/curation.py apps/backend/engram/memory/curation_tests.py
git commit --no-verify -m 'feat: curation judge reason field with decision logging'
```

```json:metadata
{"files": ["apps/backend/engram/memory/curation.py", "apps/backend/engram/memory/curation_tests.py"], "verifyCommand": "docker exec engram-tester-so pytest engram/memory/curation_tests.py -q", "acceptanceCriteria": ["prompt requires decision+reason", "parse_curation_reason defensive", "decision+reason logged", "decision parsing unchanged"], "modelTier": "mechanical"}
```

---

### Task 6: Full verification + PR

**Goal:** Whole backend suite + lint green in the tester container; draft PR opened with evidence.

**Files:** none (verification + PR only)

**Acceptance Criteria:**
- [ ] `pytest` full backend suite passes in `engram-tester-so`
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] Draft PR from `feat/provider-structured-output` to `master` with commands + exit codes in the body, and a note about the expected merge conflict with the parallel chunked-distillation work

**Verify:** `docker exec engram-tester-so bash -c 'pytest -q && ruff check . && ruff format --check .'` → exit 0

**Steps:**

- [ ] **Step 1: Full suite + lint**

Run: `docker exec engram-tester-so bash -c 'pytest -q 2>&1 | tail -20 && ruff check . && ruff format --check .'`
Expected: 0 failures; record the exact tail output and exit code.

- [ ] **Step 2: Push and open draft PR**

```bash
cd /mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-so
git push -u origin feat/provider-structured-output
gh pr create --draft --title 'feat: provider-side structured output + memory prompt hardening' --body '<summary + commands with exit codes + merge-risk note>'
```

PR body must include: what changed per file, the contract change (`{"memories": [...]}`), the behavior change (valid-empty → no fallback candidate; SKIP → no candidate), test commands with exit codes, and the merge-risk note re parallel chunking WIP in `distillation.py`.

```json:metadata
{"files": [], "verifyCommand": "docker exec engram-tester-so bash -c 'pytest -q && ruff check . && ruff format --check .'", "acceptanceCriteria": ["full suite green", "lint green", "draft PR opened with evidence"], "modelTier": "standard"}
```
