from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from engram.core.models import (
    Agent,
    AgentSession,
    DistillationChunk,
    DistillationObservationCoverage,
    DistillationStage,
    DistillationWindow,
    MemoryCandidate,
    MemoryCandidateSource,
    Observation,
    Organization,
    Project,
    Runtime,
    Team,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.workflow_work import CreateWorkflowWorkInput, create_work
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope

_HEX_A = 'a' * 64
_HEX_B = 'b' * 64
_HEX_C = 'c' * 64
_HEX_D = 'd' * 64
_HEX_E = 'e' * 64
_HEX_F = 'f' * 64


class _Deps:
    organization: Organization
    team: Team
    project: Project
    agent: Agent
    session: AgentSession
    observation: Observation
    work: WorkflowWork
    policy: ModelPolicy
    candidate: MemoryCandidate
    call: ProviderCallRecord


def _deps(suffix: str, *, policy: bool = True, candidate: bool = True) -> _Deps:
    deps = _Deps()
    deps.organization = Organization.objects.create(name=f'Org {suffix}', slug=f'org-{suffix}')
    deps.team = Team.objects.create(organization=deps.organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    deps.project = Project.objects.create(
        organization=deps.organization,
        name=f'Project {suffix}',
        slug=f'project-{suffix}',
    )
    deps.agent = Agent.objects.create(
        organization=deps.organization,
        runtime=Runtime.CODEX,
        external_id=f'agent-{suffix}',
    )
    deps.session = AgentSession.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        agent=deps.agent,
        external_session_id=f'session-{suffix}',
        runtime=Runtime.CODEX,
    )
    deps.observation = Observation.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        agent=deps.agent,
        session=deps.session,
        observation_type='tool_use',
        title='observation',
        content_hash=f'content-{suffix}',
        session_sequence=1,
        source_metadata={'event_type': 'post_tool_use'},
    )
    with transaction.atomic():
        deps.work, _created = create_work(
            CreateWorkflowWorkInput(
                organization_id=deps.organization.id,
                project_id=deps.project.id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                subject_type=WorkflowSubjectType.AGENT_SESSION,
                subject_id=deps.session.id,
                input_snapshot={
                    'schema': 'session_distillation_input/v1',
                    'session_id': str(deps.session.id),
                    'lower_sequence_exclusive': 0,
                    'upper_sequence_inclusive': 1,
                },
            )
        )
    if policy:
        secret = ProviderSecret.objects.create(
            organization=deps.organization,
            team=deps.team,
            name='secret',
            provider='openai',
            scope='team',
            current_version=1,
        )
        ProviderSecretEnvelope.objects.create(
            organization=deps.organization,
            team=deps.team,
            secret=secret,
            version=1,
            key_version='v1',
            ciphertext='cipher',
            hmac_digest='hmac',
            active=True,
        )
        deps.policy = ModelPolicy.objects.create(
            organization=deps.organization,
            team=deps.team,
            project=deps.project,
            name='Curation policy',
            scope='project',
            task_type='curation',
            provider='openai',
            model='gpt-4.1-mini',
            secret=secret,
            version=2,
        )
        deps.call = ProviderCallRecord.objects.create(
            organization=deps.organization,
            project=deps.project,
            team=deps.team,
            policy=deps.policy,
            secret=secret,
            provider='openai',
            model='gpt-4.1-mini',
            task_type='curation',
            policy_version=2,
            request_id=f'distill-stage:{suffix}',
            redaction_state='redacted',
        )
    if candidate:
        deps.candidate = MemoryCandidate.objects.create(
            organization=deps.organization,
            project=deps.project,
            team=deps.team,
            title='candidate',
            body='candidate body',
            content_hash=f'candidate-{suffix}',
        )

    return deps


def _window(deps: _Deps, **overrides: object) -> object:
    window_model = DistillationWindow
    fields: dict[str, object] = {
        'organization': deps.organization,
        'project': deps.project,
        'team': deps.team,
        'work': deps.work,
        'session': deps.session,
        'contract_version': 1,
        'lower_sequence_exclusive': 0,
        'upper_sequence_inclusive': 1,
        'observation_count': 1,
        'input_hash': _HEX_A,
        'chunk_char_budget': 8000,
        'reduction_target': 12,
        'chunk_contract_version': 1,
    }
    fields.update(overrides)

    return window_model.objects.create(**fields)


def _chunk(deps: _Deps, window: object, **overrides: object) -> object:
    chunk_model = DistillationChunk
    fields: dict[str, object] = {
        'organization': deps.organization,
        'project': deps.project,
        'team': deps.team,
        'window': window,
        'ordinal': 0,
        'first_sequence': 1,
        'last_sequence': 1,
        'observation_count': 1,
        'input_manifest': {
            'schema': 'distillation_chunk_manifest.v1',
            'window_input_hash': window.input_hash,
            'ordinal': 0,
            'observations': [
                {'observation_id': str(deps.observation.id), 'session_sequence': 1, 'content_digest': _HEX_A},
            ],
        },
        'input_hash': _HEX_B,
    }
    fields.update(overrides)

    return chunk_model.objects.create(**fields)


def _stage(deps: _Deps, window: object, chunk: object | None, **overrides: object) -> object:
    stage_model = DistillationStage
    fields: dict[str, object] = {
        'organization': deps.organization,
        'project': deps.project,
        'team': deps.team,
        'window': window,
        'chunk': chunk,
        'stage_kind': 'extract',
        'level': 0,
        'ordinal': 0,
        'target_key': _HEX_C,
        'stage_key': _HEX_D,
        'input_hash': _HEX_E,
        'input_manifest': {'chunk_ordinal': 0},
        'prompt_contract': 'distill_extract.v1',
        'policy': deps.policy,
        'policy_version': 2,
        'policy_role': 'primary',
        'status': 'required',
        'attempt_count': 0,
    }
    fields.update(overrides)

    return stage_model.objects.create(**fields)


def _complete_overrides(deps: _Deps, now: datetime) -> dict[str, object]:
    return {
        'status': 'complete',
        'attempt_count': 1,
        'accepted_provider_call': deps.call,
        'response_hash': _HEX_A,
        'response_size': 16,
        'output_snapshot': {'memories': []},
        'output_hash': _HEX_F,
        'completed_at': now,
    }


@pytest.mark.django_db
def test_window_rejects_project_from_another_organization() -> None:
    deps = _deps('win-scope', policy=False, candidate=False)
    other = Organization.objects.create(name='Other', slug='other-win-scope')
    other_project = Project.objects.create(organization=other, name='Other project', slug='other-project-win-scope')

    with pytest.raises(ValidationError):
        _window(deps, project=other_project)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('label', 'overrides'),
    (
        ('out_of_order_bounds', {'lower_sequence_exclusive': 1, 'upper_sequence_inclusive': 1}),
        ('non_positive_count', {'observation_count': 0}),
        ('non_positive_budget', {'chunk_char_budget': 0}),
        ('non_positive_reduction_target', {'reduction_target': 0}),
        ('uppercase_input_hash', {'input_hash': _HEX_A.upper()}),
        ('contract_version', {'contract_version': 2}),
        ('chunk_contract_version', {'chunk_contract_version': 2}),
    ),
)
def test_window_constraints_reject_invalid_columns(label: str, overrides: dict[str, object]) -> None:
    deps = _deps(f'win-ck-{label}', policy=False, candidate=False)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _window(deps, **overrides)


@pytest.mark.django_db
def test_window_is_unique_per_work() -> None:
    deps = _deps('win-per-work', policy=False, candidate=False)
    _window(deps)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _window(deps, input_hash=_HEX_B)


@pytest.mark.django_db
def test_window_is_unique_per_scope_and_input_hash() -> None:
    deps = _deps('win-scope-hash', policy=False, candidate=False)
    _window(deps)
    with transaction.atomic():
        second_work, _created = _duplicate_session_work(deps)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _window(deps, work=second_work, input_hash=_HEX_A)


@pytest.mark.django_db
def test_window_requires_session_and_team_to_match_work() -> None:
    deps = _deps('win-crossrow', policy=False, candidate=False)
    other_session = AgentSession.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        agent=deps.agent,
        external_session_id='session-win-crossrow-other',
        runtime=Runtime.CODEX,
    )

    with pytest.raises(ValidationError):
        _window(deps, session=other_session)


def _duplicate_session_work(deps: _Deps) -> tuple[WorkflowWork, bool]:
    return create_work(
        CreateWorkflowWorkInput(
            organization_id=deps.organization.id,
            project_id=deps.project.id,
            work_type=WorkflowWorkType.SESSION_DISTILLATION,
            subject_type=WorkflowSubjectType.AGENT_SESSION,
            subject_id=deps.session.id,
            input_snapshot={
                'schema': 'session_distillation_input/v1',
                'session_id': str(deps.session.id),
                'lower_sequence_exclusive': 0,
                'upper_sequence_inclusive': 2,
            },
        )
    )


@pytest.mark.django_db
def test_chunk_is_unique_per_window_ordinal() -> None:
    deps = _deps('chunk-ordinal', policy=False, candidate=False)
    window = _window(deps)
    _chunk(deps, window)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _chunk(deps, window, input_hash=_HEX_C)


@pytest.mark.django_db
def test_chunk_is_unique_per_window_input_hash() -> None:
    deps = _deps('chunk-hash', policy=False, candidate=False)
    window = _window(deps)
    _chunk(deps, window)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _chunk(deps, window, ordinal=1)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('label', 'overrides'),
    (
        ('out_of_order_bounds', {'first_sequence': 3, 'last_sequence': 1}),
        ('non_positive_count', {'observation_count': 0}),
        ('uppercase_input_hash', {'input_hash': _HEX_B.upper()}),
    ),
)
def test_chunk_constraints_reject_invalid_columns(label: str, overrides: dict[str, object]) -> None:
    deps = _deps(f'chunk-ck-{label}', policy=False, candidate=False)
    window = _window(deps)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _chunk(deps, window, **overrides)


@pytest.mark.django_db
def test_chunk_scope_must_match_window() -> None:
    deps = _deps('chunk-crossrow', policy=False, candidate=False)
    window = _window(deps)

    with pytest.raises(ValidationError):
        _chunk(deps, window, team=None)


@pytest.mark.django_db
def test_stage_key_is_unique_per_scope() -> None:
    deps = _deps('stage-key')
    window = _window(deps)
    chunk = _chunk(deps, window)
    _stage(deps, window, chunk)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(deps, window, chunk, ordinal=1, target_key=_HEX_E, input_hash=_HEX_F)


@pytest.mark.django_db
def test_stage_is_unique_per_coordinate_and_policy_version() -> None:
    deps = _deps('stage-coord')
    window = _window(deps)
    chunk = _chunk(deps, window)
    _stage(deps, window, chunk)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(deps, window, chunk, stage_key=_HEX_F, target_key=_HEX_E, input_hash=_HEX_C)

    fallback = _stage(
        deps,
        window,
        chunk,
        stage_key=_HEX_F,
        target_key=_HEX_E,
        input_hash=_HEX_C,
        policy_role='fallback',
    )
    assert fallback.policy_role == 'fallback'

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(
            deps,
            window,
            chunk,
            stage_key='c' * 64,
            target_key='b' * 64,
            input_hash='a' * 64,
            policy_role='fallback',
        )


def test_stage_coordinate_constraint_includes_policy_role() -> None:
    constraint = next(
        constraint
        for constraint in DistillationStage._meta.constraints
        if constraint.name == 'core_distill_stage_coord_uniq'
    )

    assert tuple(constraint.fields) == (
        'window',
        'stage_kind',
        'level',
        'ordinal',
        'prompt_contract',
        'policy',
        'policy_version',
        'policy_role',
    )
    assert constraint.condition is None


@pytest.mark.django_db
def test_stage_allows_one_completed_target_per_window() -> None:
    deps = _deps('stage-target')
    window = _window(deps)
    chunk = _chunk(deps, window)
    now = timezone.now()
    _stage(deps, window, chunk, **_complete_overrides(deps, now))
    second_call = ProviderCallRecord.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        policy=deps.policy,
        secret=deps.policy.secret,
        provider='openai',
        model='gpt-4.1-mini',
        task_type='curation',
        policy_version=3,
        request_id=f'distill-stage:second:{uuid.uuid4()}',
        redaction_state='redacted',
    )
    complete = _complete_overrides(deps, now)
    complete['accepted_provider_call'] = second_call

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(
            deps,
            window,
            chunk,
            ordinal=1,
            stage_key=_HEX_F,
            input_hash=_HEX_C,
            policy_version=3,
            **complete,
        )


@pytest.mark.django_db
def test_extraction_stage_requires_a_chunk() -> None:
    deps = _deps('stage-extract-shape')
    window = _window(deps)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(deps, window, None)


@pytest.mark.django_db
def test_reduction_stage_forbids_chunk_and_requires_positive_level() -> None:
    deps = _deps('stage-reduce-shape')
    window = _window(deps)
    chunk = _chunk(deps, window)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(deps, window, chunk, stage_kind='reduce', level=1)


@pytest.mark.django_db
def test_required_stage_cannot_carry_completion_fields() -> None:
    deps = _deps('stage-status-shape')
    window = _window(deps)
    chunk = _chunk(deps, window)
    now = timezone.now()

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(deps, window, chunk, status='required', completed_at=now, output_hash=_HEX_F)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('label', 'overrides'),
    (
        ('non_positive_policy_version', {'policy_version': 0}),
        ('uppercase_target_key', {'target_key': _HEX_C.upper()}),
        ('uppercase_stage_key', {'stage_key': _HEX_D.upper()}),
        ('uppercase_input_hash', {'input_hash': _HEX_E.upper()}),
    ),
)
def test_stage_constraints_reject_invalid_columns(label: str, overrides: dict[str, object]) -> None:
    deps = _deps(f'stage-ck-{label}')
    window = _window(deps)
    chunk = _chunk(deps, window)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(deps, window, chunk, **overrides)


@pytest.mark.django_db
def test_complete_stage_requires_positive_response_size() -> None:
    deps = _deps('stage-response-size')
    window = _window(deps)
    chunk = _chunk(deps, window)
    now = timezone.now()
    overrides = _complete_overrides(deps, now)
    overrides['response_size'] = 0

    with transaction.atomic(), pytest.raises(IntegrityError):
        _stage(deps, window, chunk, **overrides)


@pytest.mark.django_db
def test_stage_chunk_must_belong_to_stage_window() -> None:
    deps = _deps('stage-crossrow')
    window = _window(deps)
    with transaction.atomic():
        second_work, _created = _duplicate_session_work(deps)
    other_window = _window(deps, work=second_work, input_hash=_HEX_B)
    foreign_chunk = _chunk(deps, other_window)

    with pytest.raises(ValidationError):
        _stage(deps, window, foreign_chunk)


@pytest.mark.django_db
@pytest.mark.parametrize(
    'field',
    ('output_snapshot', 'output_hash', 'response_hash', 'response_size', 'accepted_provider_call', 'completed_at'),
)
def test_completed_stage_output_is_immutable(field: str) -> None:
    deps = _deps(f'stage-complete-immutable-{field.replace("_", "-")}')
    window = _window(deps)
    chunk = _chunk(deps, window)
    now = timezone.now()
    stage = _stage(deps, window, chunk, **_complete_overrides(deps, now))
    mutations: dict[str, object] = {
        'output_snapshot': {'memories': [{'title': 'changed'}]},
        'output_hash': _HEX_C,
        'response_hash': _HEX_C,
        'response_size': 99,
        'accepted_provider_call': None,
        'completed_at': now + timedelta(seconds=5),
    }
    setattr(stage, field, mutations[field])

    with pytest.raises(ValidationError):
        stage.save()


@pytest.mark.django_db
def test_coverage_is_unique_per_window_observation() -> None:
    deps = _deps('coverage-obs')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)
    _coverage(deps, window, stage)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _coverage(deps, window, stage, session_sequence=2)


@pytest.mark.django_db
def test_coverage_is_unique_per_window_sequence() -> None:
    deps = _deps('coverage-seq')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)
    _coverage(deps, window, stage)
    other = Observation.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        agent=deps.agent,
        session=deps.session,
        observation_type='tool_use',
        title='other',
        content_hash='content-coverage-seq-other',
        session_sequence=2,
        source_metadata={'event_type': 'post_tool_use'},
    )

    with transaction.atomic(), pytest.raises(IntegrityError):
        _coverage(deps, window, stage, observation=other, session_sequence=1)


@pytest.mark.django_db
def test_coverage_digest_must_be_lowercase_sha256() -> None:
    deps = _deps('coverage-hex')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _coverage(deps, window, stage, observation_digest=_HEX_A.upper())


@pytest.mark.django_db
def test_coverage_requires_positive_sequence() -> None:
    deps = _deps('coverage-pos')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _coverage(deps, window, stage, session_sequence=0)


@pytest.mark.django_db
def test_coverage_observation_session_must_match_window() -> None:
    deps = _deps('coverage-crossrow')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)
    other_session = AgentSession.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        agent=deps.agent,
        external_session_id='session-coverage-crossrow-other',
        runtime=Runtime.CODEX,
    )
    foreign_observation = Observation.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        agent=deps.agent,
        session=other_session,
        observation_type='tool_use',
        title='foreign',
        content_hash='content-coverage-crossrow-foreign',
        session_sequence=1,
        source_metadata={'event_type': 'post_tool_use'},
    )

    with pytest.raises(ValidationError):
        _coverage(deps, window, stage, observation=foreign_observation)


def _coverage(deps: _Deps, window: object, stage: object, **overrides: object) -> object:
    coverage_model = DistillationObservationCoverage
    fields: dict[str, object] = {
        'organization': deps.organization,
        'project': deps.project,
        'team': deps.team,
        'window': window,
        'observation': deps.observation,
        'session_sequence': 1,
        'observation_digest': _HEX_A,
        'outcome': 'no_signal',
        'deciding_stage': stage,
    }
    fields.update(overrides)

    return coverage_model.objects.create(**fields)


def _candidate_source(deps: _Deps, window: object, stage: object, **overrides: object) -> object:
    source_model = MemoryCandidateSource
    fields: dict[str, object] = {
        'organization': deps.organization,
        'project': deps.project,
        'team': deps.team,
        'candidate': deps.candidate,
        'window': window,
        'observation': deps.observation,
        'stage': stage,
        'anchors': {'schema': 'candidate_source_anchors.v1', 'observation_id': str(deps.observation.id)},
        'anchors_hash': _HEX_A,
    }
    fields.update(overrides)

    return source_model.objects.create(**fields)


@pytest.mark.django_db
def test_candidate_source_is_unique_per_candidate_window_observation() -> None:
    deps = _deps('source-uniq')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)
    _candidate_source(deps, window, stage)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _candidate_source(deps, window, stage, anchors_hash=_HEX_B)


@pytest.mark.django_db
def test_candidate_source_anchors_hash_must_be_lowercase_sha256() -> None:
    deps = _deps('source-hex')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)

    with transaction.atomic(), pytest.raises(IntegrityError):
        _candidate_source(deps, window, stage, anchors_hash=_HEX_A.upper())


@pytest.mark.django_db
def test_candidate_source_scope_must_match_window() -> None:
    deps = _deps('source-crossrow')
    window = _window(deps)
    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)

    with pytest.raises(ValidationError):
        _candidate_source(deps, window, stage, team=None)


def _build_row(deps: _Deps, window: object, kind: str) -> tuple[object, str, object]:
    if kind == 'window':
        return window, 'input_hash', _HEX_F

    if kind == 'chunk':
        return _chunk(deps, window), 'input_hash', _HEX_F

    chunk = _chunk(deps, window)
    stage = _stage(deps, window, chunk)
    if kind == 'stage':
        return stage, 'stage_key', _HEX_F

    return _candidate_source(deps, window, stage), 'anchors_hash', _HEX_F


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ('window', 'chunk', 'stage', 'source'))
def test_distillation_row_identity_is_immutable_after_insert(kind: str) -> None:
    deps = _deps(f'immutable-{kind}')
    window = _window(deps)
    row, field, value = _build_row(deps, window, kind)
    setattr(row, field, value)

    with pytest.raises(ValidationError):
        row.save()


@pytest.mark.django_db
def test_candidate_decision_work_contract_version_defaults_to_zero() -> None:
    deps = _deps('candidate-decision-default')

    assert deps.candidate.decision_work_contract_version == 0


@pytest.mark.django_db
def test_candidate_decision_work_contract_version_rejects_out_of_range() -> None:
    deps = _deps('candidate-decision-range')

    with transaction.atomic(), pytest.raises(IntegrityError):
        MemoryCandidate.objects.filter(id=deps.candidate.id).update(decision_work_contract_version=2)


@pytest.mark.django_db
def test_workflow_work_accepts_candidate_decision_subject_pair() -> None:
    deps = _deps('candidate-decision-pair')

    work = WorkflowWork.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=deps.candidate.id,
        contract_version=1,
        occurrence_key='',
        input_fingerprint=_HEX_A,
        input_snapshot={'schema': 'candidate_decision_input/v1'},
    )

    assert work.work_type == WorkflowWorkType.CANDIDATE_DECISION
    assert work.subject_type == WorkflowSubjectType.MEMORY_CANDIDATE


def _candidate_decision_work(deps: _Deps) -> WorkflowWork:
    return WorkflowWork.objects.create(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=deps.candidate.id,
        contract_version=1,
        occurrence_key='',
        input_fingerprint=_HEX_A,
        input_snapshot={'schema': 'candidate_decision_input/v1'},
    )


@pytest.mark.django_db
def test_workflow_run_accepts_candidate_decision_run_type() -> None:
    deps = _deps('run-candidate-decision')
    work = _candidate_decision_work(deps)
    now = timezone.now()

    run = WorkflowRun(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        work=work,
        run_type=WorkflowRunType.CANDIDATE_DECISION,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
        origin=WorkflowRunOrigin.AUTOMATIC,
        dispatched_at=now,
        input_snapshot=work.input_snapshot,
    )
    run.full_clean()
    run.save()

    assert WorkflowRun.objects.get(id=run.id).run_type == WorkflowRunType.CANDIDATE_DECISION


@pytest.mark.django_db
def test_workflow_run_rejects_unknown_run_type() -> None:
    deps = _deps('run-unknown-type')
    work = _candidate_decision_work(deps)
    now = timezone.now()

    run = WorkflowRun(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        work=work,
        run_type='not_a_real_run_type',
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
        origin=WorkflowRunOrigin.AUTOMATIC,
        dispatched_at=now,
        input_snapshot=work.input_snapshot,
    )

    with pytest.raises(ValidationError):
        run.full_clean()


@pytest.mark.django_db
def test_complete_reused_stage_shares_provider_call_and_satisfies_status_shape() -> None:
    deps = _deps('stage-reuse-shape')
    window = _window(deps)
    chunk = _chunk(deps, window)
    now = timezone.now()
    source = _stage(deps, window, chunk, **_complete_overrides(deps, now))

    reused = DistillationStage(
        organization=deps.organization,
        project=deps.project,
        team=deps.team,
        window=window,
        chunk=chunk,
        stage_kind='extract',
        level=0,
        ordinal=1,
        target_key=_HEX_E,
        stage_key=_HEX_F,
        input_hash=_HEX_C,
        input_manifest={'chunk_ordinal': 0},
        prompt_contract='distill_extract.v1',
        policy=deps.policy,
        policy_version=2,
        policy_role='primary',
        reuse_key=_HEX_A,
        reused_from=source,
        **_complete_overrides(deps, now),
    )
    reused.full_clean()
    reused.save()

    persisted = DistillationStage.objects.get(id=reused.id)
    assert persisted.reused_from_id == source.id
    assert persisted.accepted_provider_call_id == source.accepted_provider_call_id
    assert persisted.status == 'complete'


@pytest.mark.django_db
def test_reused_from_protects_source_stage_from_deletion() -> None:
    deps = _deps('stage-reuse-protect')
    window = _window(deps)
    chunk = _chunk(deps, window)
    now = timezone.now()
    source = _stage(deps, window, chunk, **_complete_overrides(deps, now))
    _stage(
        deps,
        window,
        chunk,
        ordinal=1,
        target_key=_HEX_E,
        stage_key=_HEX_F,
        input_hash=_HEX_C,
        reuse_key=_HEX_A,
        reused_from=source,
        **_complete_overrides(deps, now),
    )

    with pytest.raises(ProtectedError):
        source.delete()
