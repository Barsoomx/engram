# Memory Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend memory feedback loop that lets an authorized reviewer mark memory as `stale` or `refuted` and proves future context no longer injects it.

**Architecture:** Add a small DRF endpoint that delegates to a transaction-scoped service. The service reuses `ResolveApiKeyScope`, updates existing `Memory.stale/refuted` and `RetrievalDocument.stale/refuted` fields, and records a redacted audit event. Existing context retrieval remains unchanged and acts as the regression proof.

**Tech Stack:** Django 5.2, Django REST Framework, pytest, PostgreSQL through Docker Compose, existing Engram access/audit/context services.

## Global Constraints

- Work from branch `feat/memory-feedback-loop`.
- Do not add a new outbox or queue for this checkpoint.
- Do not add provider, semantic retrieval, MCP, frontend, or Celery changes.
- Use absolute imports.
- Use single quotes in Python code.
- Test files live next to the tested module and use pytest function tests.
- Fixture arguments use `f_` for real fixtures and `m_` for mocks.
- Run Python tests and management commands inside Docker Compose.
- All denied or invalid feedback requests must leave `Memory`, `RetrievalDocument`, and `MemoryFeedbackRecorded` audit state unchanged.

---

## File Structure

- Create `apps/backend/engram/memory/serializers.py`: request validation, size caps, and action choices.
- Modify `apps/backend/engram/memory/services.py`: add `RecordMemoryFeedback` service and result dataclasses.
- Create `apps/backend/engram/memory/views.py`: DRF view and error mapping.
- Create `apps/backend/engram/memory/urls.py`: memory feedback route.
- Modify `apps/backend/settings/urls.py`: mount `v1/memories/`.
- Create `apps/backend/engram/memory/memory_feedback_tests.py`: endpoint, service, auth, audit, and context regression tests.
- Modify `docs/verification-matrix.md`: append this checkpoint evidence.
- Create `docs/security/reviews/2026-06-25-memory-feedback-loop.md`: focused security review output.

---

### Task 1: Backend Memory Feedback API

**Files:**
- Create: `apps/backend/engram/memory/serializers.py`
- Modify: `apps/backend/engram/memory/services.py`
- Create: `apps/backend/engram/memory/views.py`
- Create: `apps/backend/engram/memory/urls.py`
- Modify: `apps/backend/settings/urls.py`
- Create: `apps/backend/engram/memory/memory_feedback_tests.py`

**Interfaces:**
- Consumes: existing `ResolveApiKeyScope.execute(...)`, `Memory`, `RetrievalDocument`, `AuditEvent`, `BuildContextBundle` behavior.
- Produces: `RecordMemoryFeedback.execute(data: MemoryFeedbackInput) -> MemoryFeedbackResult`; route `POST /v1/memories/<uuid:memory_id>/feedback`.

- [ ] **Step 1: Write failing endpoint tests**

Create `apps/backend/engram/memory/memory_feedback_tests.py` with tests for:

```python
def test_memory_feedback_stale_updates_memory_documents_and_audit() -> None:
    ...

def test_memory_feedback_refuted_removes_memory_from_future_context() -> None:
    ...

def test_memory_feedback_requires_memories_review_capability() -> None:
    ...

def test_memory_feedback_denies_wrong_project_without_mutating_memory() -> None:
    ...

def test_memory_feedback_rejects_oversized_reason_before_mutating_memory() -> None:
    ...
```

Use helper setup matching `engram.context.context_api_tests.create_project_scope`, but assign an `organization_admin` or `organization_owner` role to the reviewer identity so `memories:review` is present in owner capabilities. The API key must still explicitly include `memories:review`; a separate negative key should include only `memories:read`.

The success tests must assert:

```python
assert response.status_code == 200
memory.refresh_from_db()
document.refresh_from_db()
assert memory.stale is True
assert document.stale is True
audit = AuditEvent.objects.get(event_type='MemoryFeedbackRecorded')
assert audit.capability == 'memories:review'
assert audit.target_type == 'memory'
assert audit.target_id == str(memory.id)
assert RAW_KEY not in str(response.json())
assert RAW_KEY not in str(audit.metadata)
```

The context regression must call the feedback endpoint with `action='refuted'`,
then call `/v1/context/session-start` for the same query and assert:

```python
assert context_response.status_code == 200
assert context_response.json()['items'] == []
assert str(memory.id) not in str(context_response.json())
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_feedback_tests.py -v"
```

Expected: FAIL because `/v1/memories/{memory_id}/feedback` is not routed or the new files/classes do not exist yet.

- [ ] **Step 3: Add serializer**

Create `apps/backend/engram/memory/serializers.py`:

```python
from __future__ import annotations

from rest_framework import serializers

MEMORY_FEEDBACK_REASON_MAX_LENGTH = 2000
MEMORY_FEEDBACK_METADATA_MAX_LENGTH = 255


class MemoryFeedbackSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    action = serializers.ChoiceField(choices=('stale', 'refuted'))
    reason = serializers.CharField(max_length=MEMORY_FEEDBACK_REASON_MAX_LENGTH, allow_blank=False)
    request_id = serializers.CharField(max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH)
    correlation_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH,
    )
```

- [ ] **Step 4: Add service**

Append to `apps/backend/engram/memory/services.py`:

```python
@dataclass(frozen=True)
class MemoryFeedbackInput:
    raw_key: str
    memory_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    action: str
    reason: str
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True)
class MemoryFeedbackResult:
    memory: Memory
    action: str
    retrieval_documents_updated: int
    already_applied: bool

    def to_response(self) -> dict[str, object]:
        return {
            'memory_id': str(self.memory.id),
            'project_id': str(self.memory.project_id),
            'team_id': str(self.memory.team_id) if self.memory.team_id else '',
            'action': self.action,
            'stale': self.memory.stale,
            'refuted': self.memory.refuted,
            'retrieval_documents_updated': self.retrieval_documents_updated,
            'already_applied': self.already_applied,
        }


class MemoryFeedbackError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecordMemoryFeedback:
    def execute(self, data: MemoryFeedbackInput) -> MemoryFeedbackResult:
        scope = ResolveApiKeyScope().execute(...)
        with transaction.atomic():
            memory = self._lock_memory(data, scope)
            self._ensure_team_scope(memory, scope)
            already_applied = self._already_applied(memory, data.action)
            self._apply(memory, data.action)
            updated = self._sync_retrieval_documents(memory, data.action)
            self._audit(memory, scope, data, updated, already_applied)
            return MemoryFeedbackResult(...)
```

The concrete implementation must:

- call `ResolveApiKeyScope` with `required_capability='memories:review'`,
  `requested_project_id=data.project_id`, `requested_team_id=data.team_id`,
  `target_type='memory'`, and `target_id=str(data.memory_id)`;
- load memory with `select_for_update()` filtered by `organization_id`,
  `project_id`, and `id`;
- raise `MemoryFeedbackError('memory_not_found', 'Memory was not found')`
  when the scoped memory is absent;
- raise `AccessDeniedError('team_scope_denied', ...)` when a team-scoped
  memory's `team_id` is not in `scope.team_ids`;
- set only the selected flag: `stale=True` for `stale`, `refuted=True` for
  `refuted`;
- update all retrieval documents for that memory with the same selected flag;
- write `AuditEvent(event_type='MemoryFeedbackRecorded', result=AuditResult.ALLOWED, capability='memories:review')`;
- store redacted `reason` in audit metadata using existing `redact_text`.

- [ ] **Step 5: Add view and URLs**

Create `apps/backend/engram/memory/views.py`:

```python
from __future__ import annotations

import uuid
from typing import Any

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError
from engram.context.views import access_error_response, bearer_key
from engram.memory.serializers import MemoryFeedbackSerializer
from engram.memory.services import MemoryFeedbackError, MemoryFeedbackInput, RecordMemoryFeedback


MEMORY_FEEDBACK_STATUS = {
    'memory_not_found': status.HTTP_404_NOT_FOUND,
}


class MemoryFeedbackView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request, memory_id: uuid.UUID) -> Response:
        serializer = MemoryFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = RecordMemoryFeedback().execute(self._input(request, memory_id, data))
        except AccessDeniedError as error:
            return access_error_response(error)
        except MemoryFeedbackError as error:
            return Response(
                {'code': error.code, 'detail': str(error)},
                status=MEMORY_FEEDBACK_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
            )

        return Response(result.to_response())

    def _input(self, request: Request, memory_id: uuid.UUID, data: dict[str, Any]) -> MemoryFeedbackInput:
        return MemoryFeedbackInput(
            raw_key=bearer_key(request),
            memory_id=memory_id,
            project_id=data['project_id'],
            team_id=data.get('team_id'),
            action=data['action'],
            reason=data['reason'],
            request_id=data['request_id'],
            correlation_id=data.get('correlation_id', ''),
        )
```

Create `apps/backend/engram/memory/urls.py`:

```python
from django.urls import path

from engram.memory.views import MemoryFeedbackView

urlpatterns = [
    path('<uuid:memory_id>/feedback', MemoryFeedbackView.as_view(), name='memory-feedback'),
]
```

Modify `apps/backend/settings/urls.py`:

```python
path('v1/memories/', include('engram.memory.urls')),
```

- [ ] **Step 6: Run focused tests and fix to GREEN**

Run:

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_feedback_tests.py -v"
```

Expected: PASS for all memory feedback tests.

- [ ] **Step 7: Run adjacent regression tests**

Run:

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/context/context_api_tests.py engram/access/access_scope_tests.py -v"
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/backend/engram/memory/serializers.py apps/backend/engram/memory/services.py apps/backend/engram/memory/views.py apps/backend/engram/memory/urls.py apps/backend/settings/urls.py apps/backend/engram/memory/memory_feedback_tests.py
git commit -m "feat: add memory feedback loop"
```

---

### Task 2: Evidence And Security Review Artifacts

**Files:**
- Modify: `docs/verification-matrix.md`
- Create: `docs/security/reviews/2026-06-25-memory-feedback-loop.md`

**Interfaces:**
- Consumes: Task 1 commit and command output.
- Produces: checkpoint evidence for PR review and merge.

- [ ] **Step 1: Run full local verification**

Run:

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check . && ruff format --check ."
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"
python3 scripts/e2e_golden_path.py
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
git diff --check HEAD
```

Record exit codes and first decisive failure if any command fails.

- [ ] **Step 2: Write focused security review artifact**

Create `docs/security/reviews/2026-06-25-memory-feedback-loop.md` with:

```markdown
# Memory Feedback Loop Security Review

Date: 2026-06-25

Branch: `feat/memory-feedback-loop`

Result: SECURITY APPROVED / CHANGES_REQUIRED

## Scope Reviewed

- `POST /v1/memories/{memory_id}/feedback`
- `memories:review` capability enforcement
- cross-project and team-scope denial
- `Memory` and `RetrievalDocument` flag consistency
- audit metadata redaction
- context retrieval exclusion after stale/refuted feedback

## Commands And Tools Run

| Check | Result |
| --- | --- |
| focused memory feedback tests | exit code and summary |
| adjacent context/access tests | exit code and summary |
| full backend tests | exit code and summary |
| lint/format | exit code and summary |
| migration freshness | exit code and summary |
| Compose golden path | exit code and summary |

## Findings By Severity

### CRITICAL

None or listed findings.

### IMPORTANT

None or listed findings.

### MINOR

None or listed findings.

## Fixes Applied

List fixes or `None`.

## Accepted Risk

List accepted risks or `None`.
```

- [ ] **Step 3: Append verification matrix entry**

Append `## 2026-06-25: Memory Feedback Loop` to
`docs/verification-matrix.md` with rows for the commands in Step 1 and notes
that the checkpoint proves only backend stale/refuted feedback, not frontend,
MCP, daily curator, provider policy, or semantic retrieval.

- [ ] **Step 4: Commit**

```bash
git add docs/security/reviews/2026-06-25-memory-feedback-loop.md docs/verification-matrix.md
git commit -m "test: record memory feedback evidence"
```

---

## Final Verification

Before PR:

```bash
git status --short --branch
gh pr list --state open --json number,title,headRefName,baseRefName,url,isDraft
```

Open a draft PR and include commands with exit codes. Promote only after local
verification and CI are green.

## Self-Review

- Spec coverage: endpoint, authorization, transaction, audit, request caps,
  retrieval exclusion, docs, and security artifact all have tasks.
- Placeholder scan: no incomplete-work placeholder markers.
- Simplicity: no new outbox, provider, semantic retrieval, frontend, MCP, or
  migration is planned because existing fields and retrieval filters already
  support this loop.
