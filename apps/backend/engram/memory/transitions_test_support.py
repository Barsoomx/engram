from __future__ import annotations

import hashlib
import importlib
import uuid
from decimal import Decimal
from typing import Any

from django.utils import timezone

from engram.core.models import (
    Agent,
    AgentSession,
    CandidateStatus,
    DistillationStage,
    DistillationStageKind,
    DistillationStageStatus,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryConflict,
    Observation,
    ObservationSource,
    Organization,
    Project,
    Team,
    VisibilityScope,
)
from engram.memory.distillation_provenance import (
    candidate_source_anchors,
    canonical_source_manifest,
    session_candidate_content_hash,
)
from engram.memory.distillation_provider_stage import stage_key as provider_stage_key
from engram.memory.distillation_provider_stage import stage_target_key
from engram.memory.distillation_window import materialize_distillation_window
from engram.memory.import_provenance import import_candidate_content_hash, import_candidate_source_anchors
from engram.memory.session_lifecycle import EndSession
from engram.memory.workflow_work import canonical_json_bytes, observation_content_digest
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret


def transitions_module() -> Any:
    return importlib.import_module('engram.memory.transitions')


def _create_scope(suffix: str) -> tuple[Organization, Project, AgentSession]:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    project = Project.objects.create(organization=organization, name=f'Project {suffix}', slug=f'project-{suffix}')
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id=f'agent-{suffix}')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime='codex',
        observation_sequence_cursor=0,
    )
    return organization, project, session


def _ended_session_work(scope: tuple[Organization, Project, AgentSession]) -> Any:
    organization, project, session = scope
    Observation.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title='observation 1',
        content_hash=f'content-{session.id}-1',
        session_sequence=1,
        source_metadata={'event_type': 'post_tool_use'},
    )
    result = EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )
    from engram.core.models import WorkflowWork

    return WorkflowWork.objects.get(id=result.work_id)


def _stage_policy(scope: tuple[Organization, Project, AgentSession], suffix: str) -> ModelPolicy:
    organization, project, session = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=session.team,
        name=f'Transition support {suffix} secret',
        provider='openai',
        scope='team',
    )
    return ModelPolicy.objects.create(
        organization=organization,
        team=session.team,
        project=project,
        name=f'Transition support {suffix} policy',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )


def _stage_history(
    scope: tuple[Organization, Project, AgentSession],
    window: Any,
) -> tuple[DistillationStage, DistillationStage]:
    organization, project, session = scope
    chunk = window.chunks.get(ordinal=0)
    primary_policy = _stage_policy(scope, 'primary')
    fallback_policy = _stage_policy(scope, 'fallback')
    target_key = stage_target_key(
        work_id=str(window.work_id),
        work_input_fingerprint=window.work.input_fingerprint,
        window_input_hash=window.input_hash,
        stage_kind=DistillationStageKind.EXTRACT,
        level=0,
        ordinal=chunk.ordinal,
        chunk_ordinal=chunk.ordinal,
        input_hash=chunk.input_hash,
        prompt_contract='distill_extract.v1',
    )
    primary_stage = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        window=window,
        chunk=chunk,
        stage_kind=DistillationStageKind.EXTRACT,
        level=0,
        ordinal=chunk.ordinal,
        target_key=target_key,
        stage_key=provider_stage_key(
            target_key=target_key,
            policy_id=str(primary_policy.id),
            policy_version=primary_policy.version,
            policy_role='primary',
        ),
        input_hash=chunk.input_hash,
        input_manifest=chunk.input_manifest,
        prompt_contract='distill_extract.v1',
        policy=primary_policy,
        policy_version=primary_policy.version,
        policy_role='primary',
        status=DistillationStageStatus.REQUIRED,
        attempt_count=1,
        last_failure_class='provider_timeout',
        last_failure_at=timezone.now(),
    )
    call = ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        policy=fallback_policy,
        secret=fallback_policy.secret,
        provider=fallback_policy.provider,
        model=fallback_policy.model,
        task_type=fallback_policy.task_type,
        policy_version=fallback_policy.version,
        request_id=f'distill-stage:{uuid.uuid4()}',
        redaction_state='redacted',
    )
    fallback_stage = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        window=window,
        chunk=chunk,
        stage_kind=DistillationStageKind.EXTRACT,
        level=0,
        ordinal=chunk.ordinal,
        target_key=target_key,
        stage_key=provider_stage_key(
            target_key=target_key,
            policy_id=str(fallback_policy.id),
            policy_version=fallback_policy.version,
            policy_role='fallback',
        ),
        input_hash=chunk.input_hash,
        input_manifest=chunk.input_manifest,
        prompt_contract='distill_extract.v1',
        policy=fallback_policy,
        policy_version=fallback_policy.version,
        policy_role='fallback',
        status=DistillationStageStatus.COMPLETE,
        attempt_count=1,
        accepted_provider_call=call,
        response_hash='a' * 64,
        response_size=1,
        output_snapshot={'memories': [], 'no_signal_observation_ids': []},
        output_hash='b' * 64,
        completed_at=timezone.now(),
    )
    return fallback_stage, primary_stage


def provenanced_candidate(suffix: str = 'promotion') -> tuple[MemoryCandidate, Any, Any]:
    organization, project, session = _create_scope(suffix)
    work = _ended_session_work((organization, project, session))
    window = materialize_distillation_window(work)
    stage, _primary = _stage_history((organization, project, session), window)
    observation = Observation.objects.get(session=session, session_sequence=1)
    title = f'Promotion candidate {suffix}'
    body = f'Promotion body {suffix}'
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        source_observation=observation,
        title=title,
        body=body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=VisibilityScope.PROJECT,
        evidence=[{'observation_id': str(observation.id)}],
        content_hash=session_candidate_content_hash(session.id, title, body),
        confidence=Decimal('0.900'),
        decision_work_contract_version=1,
    )
    anchors = candidate_source_anchors(
        observation,
        observation_id=str(observation.id),
        session_sequence=observation.session_sequence,
        observation_digest=observation_content_digest(observation),
    )
    source = MemoryCandidateSource.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        candidate=candidate,
        window=window,
        observation=observation,
        stage=stage,
        anchors=anchors,
        anchors_hash=canonical_source_manifest(anchors),
    )
    return candidate, source, (organization, project, session)


def convert_candidate_to_import(
    candidate: MemoryCandidate,
    source: MemoryCandidateSource,
    *,
    suffix: str = 'import',
) -> tuple[MemoryCandidate, MemoryCandidateSource, ObservationSource]:
    observation = source.observation
    import_source = ObservationSource.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        observation=observation,
        source_type='claude_mem',
        source_id=f'claude-mem:{suffix}:{uuid.uuid4()}',
        metadata={
            'source_store_id': f'{suffix}-store',
            'event_type': 'claude_mem.observation',
        },
    )
    anchors = import_candidate_source_anchors(
        observation=observation,
        import_source=import_source,
        source_store_id=f'{suffix}-store',
        event_type='claude_mem.observation',
    )
    source.delete()
    source = MemoryCandidateSource.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        candidate=candidate,
        observation=observation,
        source_kind='import',
        import_source=import_source,
        anchors=anchors,
        anchors_hash=hashlib.sha256(canonical_json_bytes(anchors)).hexdigest(),
    )
    candidate.title = observation.title
    candidate.body = observation.body or observation.title
    candidate.content_hash = import_candidate_content_hash(import_source.source_id, observation.content_hash)
    candidate.decision_work_contract_version = 1
    candidate.save(update_fields=['title', 'body', 'content_hash', 'decision_work_contract_version', 'updated_at'])
    return candidate, source, import_source


def import_provenanced_candidate(suffix: str = 'import') -> tuple[MemoryCandidate, Any, Any]:
    candidate, source, scope = provenanced_candidate(suffix)
    candidate, source, _import_source = convert_candidate_to_import(candidate, source, suffix=suffix)
    return candidate, source, scope


def transition_request(candidate: MemoryCandidate, *, key: str | None = None, reason: str = 'test promotion') -> Any:
    transitions = transitions_module()
    scope = transitions.TransitionScope(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=candidate.team_id,
    )
    request = transitions.TransitionRequest(
        scope=scope,
        idempotency_key=key or f'candidate:{candidate.id}:settle:v1',
        actor_type='test',
        actor_id='promotion-tests',
        capability='memories:write',
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        reason=reason,
        origin='promotion-tests',
    )
    from engram.memory.import_provenance import candidate_evidence_manifest

    _entries, manifest_hash = candidate_evidence_manifest(candidate)
    fence = transitions.CandidateFence(
        candidate_id=candidate.id,
        candidate_content_hash=candidate.content_hash,
        evidence_manifest_hash=manifest_hash,
    )
    return transitions.PromoteMemoryCandidateInput(request=request, candidate_fence=fence)


def candidate_in_scope(
    base: MemoryCandidate,
    source: MemoryCandidateSource,
    *,
    title: str,
    body: str,
) -> tuple[MemoryCandidate, MemoryCandidateSource]:
    candidate = MemoryCandidate.objects.create(
        organization_id=base.organization_id,
        project_id=base.project_id,
        team_id=base.team_id,
        source_observation=base.source_observation,
        title=title,
        body=body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=base.visibility_scope,
        evidence=base.evidence,
        content_hash=session_candidate_content_hash(base.source_observation.session_id, title, body),
        confidence=base.confidence,
        decision_work_contract_version=1,
    )
    candidate_source = MemoryCandidateSource.objects.create(
        organization_id=base.organization_id,
        project_id=base.project_id,
        team_id=base.team_id,
        candidate=candidate,
        window=source.window,
        observation=source.observation,
        stage=source.stage,
        anchors=source.anchors,
        anchors_hash=source.anchors_hash,
    )
    return candidate, candidate_source


def candidate_fence_for(candidate: MemoryCandidate) -> Any:
    from engram.memory.import_provenance import candidate_evidence_manifest

    _entries, manifest_hash = candidate_evidence_manifest(candidate)
    return transitions_module().CandidateFence(
        candidate_id=candidate.id,
        candidate_content_hash=candidate.content_hash,
        evidence_manifest_hash=manifest_hash,
    )


def transition_request_for(candidate: MemoryCandidate, *, key: str, reason: str = 'lineage test') -> Any:
    request = transition_request(candidate, key=key, reason=reason)
    return request.request


def promoted_pair(suffix: str) -> tuple[MemoryCandidate, MemoryCandidate, Any, Any]:
    first, source, _scope = provenanced_candidate(suffix)
    first_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(first))
    second, _second_source = candidate_in_scope(
        first,
        source,
        title=f'Second memory {suffix}',
        body=f'Second body {suffix}',
    )
    second_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(second))
    return first, second, first_result, second_result


def open_single_conflict(suffix: str) -> tuple[MemoryCandidate, MemoryConflict]:
    base_candidate, source, _scope = provenanced_candidate(suffix)
    memory_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(base_candidate))
    candidate, _candidate_source = candidate_in_scope(
        base_candidate,
        source,
        title=f'Resolution candidate {suffix}',
        body=f'Resolution candidate body {suffix}',
    )
    conflict = (
        transitions_module()
        .OpenMemoryConflict()
        .execute(
            transitions_module().OpenMemoryConflictInput(
                request=transition_request_for(
                    candidate, key=f'request:{uuid.uuid4()}:conflict-open:{candidate.id}:v1'
                ),
                candidate_fence=candidate_fence_for(candidate),
                memory_fence=transitions_module().build_memory_fence(memory_result.memory),
                evidence_hash='e' * 64,
                redacted_reason='resolution outcome contract',
            )
        )
    )
    return candidate, MemoryConflict.objects.get(id=conflict.id)
