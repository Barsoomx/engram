from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import IntegrityError, transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from engram.core.models import (
    AgentSession,
    Observation,
    Project,
    ProjectTeam,
    Team,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)

_MIN_SIGNED_64 = -(2**63)
_MAX_SIGNED_64 = 2**63 - 1
_MAX_POSITIVE_SMALL_INTEGER = 32767
_LIFECYCLE_EVENT_TYPES = frozenset({'session_start', 'session_end'})
_DIGEST_WORK_TYPES = frozenset({WorkflowWorkType.DAILY_DIGEST, WorkflowWorkType.WEEKLY_DIGEST})
_WORK_SUBJECT_PAIRS = frozenset(
    {
        (WorkflowWorkType.OBSERVATION_PROCESSING, WorkflowSubjectType.OBSERVATION),
        (WorkflowWorkType.SESSION_DISTILLATION, WorkflowSubjectType.AGENT_SESSION),
        (WorkflowWorkType.DAILY_DIGEST, WorkflowSubjectType.PROJECT),
        (WorkflowWorkType.WEEKLY_DIGEST, WorkflowSubjectType.PROJECT),
        (WorkflowWorkType.WEEKLY_DIGEST, WorkflowSubjectType.TEAM),
    }
)
_OBSERVATION_SNAPSHOT_KEYS = frozenset({'schema', 'observation_id', 'observation_digest', 'policy'})
_OBSERVATION_POLICY_KEYS = frozenset({'schema', 'realtime_candidates_enabled', 'legacy_policy_fallback'})
_SESSION_SNAPSHOT_KEYS = frozenset({'schema', 'session_id', 'lower_sequence_exclusive', 'upper_sequence_inclusive'})
_DAILY_SNAPSHOT_KEYS = frozenset(
    {
        'schema',
        'project_id',
        'schedule_key',
        'window_start',
        'window_end',
        'visibility_policy',
        'allowed_team_ids',
        'output_visibility_scope',
        'output_team_id',
        'eligible_source_count',
        'max_sources',
        'sources_truncated',
        'sources',
        'input_digest',
    }
)
_WEEKLY_SNAPSHOT_KEYS = frozenset(
    {
        'schema',
        'project_id',
        'team_id',
        'schedule_key',
        'window_start',
        'window_end',
        'visibility_policy',
        'allowed_team_ids',
        'output_visibility_scope',
        'output_team_id',
        'changes',
        'input_digest',
    }
)


class WorkflowWorkScopeError(ValueError):
    pass


class WorkflowWorkCollisionError(ValueError):
    pass


class WorkflowWorkStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CreateWorkflowWorkInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    work_type: str
    subject_type: str
    subject_id: uuid.UUID
    input_snapshot: dict[str, object]
    contract_version: int = 1
    occurrence_key: str = ''


def _require_exact_keys(value: dict[str, object], expected: frozenset[str], label: str) -> None:
    if value.keys() != expected:
        raise ValueError(f'{label} has unexpected or missing fields')


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise ValueError(f'{label} must be lowercase SHA-256')

    return value


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (bool, str)):
        return value

    if isinstance(value, int):
        if not _MIN_SIGNED_64 <= value <= _MAX_SIGNED_64:
            raise ValueError('integer is outside the signed 64-bit range')

        return value

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('timestamp must be timezone-aware')
        normalized = value.astimezone(UTC)
        timespec = 'microseconds' if normalized.microsecond else 'seconds'

        return normalized.isoformat(timespec=timespec).replace('+00:00', 'Z')

    if isinstance(value, list):
        return [_canonical_value(item) for item in value]

    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('canonical dictionaries require string keys')

        return {key: _canonical_value(item) for key, item in value.items()}

    raise ValueError(f'unsupported canonical value: {type(value).__name__}')


def canonical_json_bytes(value: object) -> bytes:
    try:
        normalized = _canonical_value(value)
    except RecursionError as error:
        raise ValueError('canonical value is recursive') from error

    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode()


def observation_content_digest(observation: Observation) -> str:
    values = (
        observation.id,
        observation.observation_type,
        observation.title,
        observation.subtitle,
        observation.body,
        observation.facts,
        observation.narrative,
        observation.concepts,
        observation.files_read,
        observation.files_modified,
        observation.source_metadata,
    )
    digest = hashlib.sha256()
    for value in values:
        encoded = canonical_json_bytes(value)
        digest.update(len(encoded).to_bytes(8, 'big', signed=False))
        digest.update(encoded)

    return digest.hexdigest()


def _validate_work_identity(
    *,
    work_type: str,
    subject_type: str,
    contract_version: int,
    occurrence_key: str,
) -> None:
    if (work_type, subject_type) not in _WORK_SUBJECT_PAIRS:
        raise ValueError('unsupported workflow work and subject pair')
    if type(contract_version) is not int or not 0 < contract_version <= _MAX_POSITIVE_SMALL_INTEGER:
        raise ValueError('contract version must be a positive small integer')
    if not isinstance(occurrence_key, str):
        raise ValueError('occurrence key must be a string')
    if work_type in _DIGEST_WORK_TYPES and not occurrence_key:
        raise ValueError('digest work requires an occurrence key')
    if work_type not in _DIGEST_WORK_TYPES and occurrence_key:
        raise ValueError('non-digest work cannot have an occurrence key')


def _observation_identity_input(
    *,
    subject_id: uuid.UUID,
    input_snapshot: dict[str, object],
) -> dict[str, object]:
    _require_exact_keys(input_snapshot, _OBSERVATION_SNAPSHOT_KEYS, 'observation snapshot')
    if input_snapshot.get('schema') != 'observation_processing_input/v1':
        raise ValueError('unsupported observation work snapshot schema')
    if input_snapshot.get('observation_id') != str(subject_id):
        raise ValueError('observation snapshot subject does not match work subject')

    observation_digest = _require_sha256(
        input_snapshot.get('observation_digest'),
        'observation snapshot digest',
    )

    policy = input_snapshot.get('policy')
    if not isinstance(policy, dict) or policy.get('schema') != 'hook_work_policy/v1':
        raise ValueError('unsupported observation work policy schema')
    _require_exact_keys(policy, _OBSERVATION_POLICY_KEYS, 'observation policy')
    realtime_enabled = policy.get('realtime_candidates_enabled')
    fallback = policy.get('legacy_policy_fallback')
    if type(realtime_enabled) is not bool or type(fallback) is not bool:
        raise ValueError('observation work policy decisions must be boolean')

    return {
        'observation_id': str(subject_id),
        'observation_digest': observation_digest,
        'realtime_candidates_enabled': realtime_enabled,
    }


def _validate_session_snapshot(subject_id: uuid.UUID, input_snapshot: dict[str, object]) -> None:
    _require_exact_keys(input_snapshot, _SESSION_SNAPSHOT_KEYS, 'session snapshot')
    if input_snapshot['schema'] != 'session_distillation_input/v1':
        raise ValueError('unsupported session work snapshot schema')
    if input_snapshot['session_id'] != str(subject_id):
        raise ValueError('session snapshot subject does not match work subject')
    lower = input_snapshot['lower_sequence_exclusive']
    upper = input_snapshot['upper_sequence_inclusive']
    if type(lower) is not int or lower != 0:
        raise ValueError('session lower sequence must be zero')
    if type(upper) is not int or not 0 <= upper <= _MAX_SIGNED_64:
        raise ValueError('session upper sequence must be a non-negative signed 64-bit integer')


def _validate_digest_common(
    *,
    input_snapshot: dict[str, object],
    schema: str,
    occurrence_key: str,
) -> None:
    if input_snapshot['schema'] != schema:
        raise ValueError('unsupported digest work snapshot schema')
    if input_snapshot['schedule_key'] != occurrence_key:
        raise ValueError('digest schedule key does not match work occurrence')
    if input_snapshot['visibility_policy'] != 'digest_visibility/v1':
        raise ValueError('unsupported digest visibility policy')
    if not isinstance(input_snapshot['window_start'], str) or not isinstance(input_snapshot['window_end'], str):
        raise ValueError('digest window boundaries must be canonical timestamp strings')
    if not isinstance(input_snapshot['allowed_team_ids'], list):
        raise ValueError('digest allowed team ids must be a list')
    _require_sha256(input_snapshot['input_digest'], 'digest input digest')


def _validate_daily_snapshot(
    *,
    subject_id: uuid.UUID,
    occurrence_key: str,
    input_snapshot: dict[str, object],
) -> None:
    _require_exact_keys(input_snapshot, _DAILY_SNAPSHOT_KEYS, 'daily digest snapshot')
    _validate_digest_common(
        input_snapshot=input_snapshot,
        schema='daily_digest_input/v1',
        occurrence_key=occurrence_key,
    )
    if input_snapshot['project_id'] != str(subject_id):
        raise ValueError('daily digest project does not match work subject')
    if input_snapshot['allowed_team_ids'] != []:
        raise ValueError('project digest cannot admit team-private sources')
    if input_snapshot['output_visibility_scope'] != 'project' or input_snapshot['output_team_id'] is not None:
        raise ValueError('daily digest output must remain project-visible')
    if type(input_snapshot['eligible_source_count']) is not int or input_snapshot['eligible_source_count'] < 0:
        raise ValueError('daily digest eligible source count must be non-negative')
    if type(input_snapshot['max_sources']) is not int or input_snapshot['max_sources'] < 0:
        raise ValueError('daily digest max sources must be non-negative')
    if type(input_snapshot['sources_truncated']) is not bool or not isinstance(input_snapshot['sources'], list):
        raise ValueError('daily digest source metadata is malformed')


def _validate_weekly_snapshot(
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    occurrence_key: str,
    input_snapshot: dict[str, object],
) -> None:
    _require_exact_keys(input_snapshot, _WEEKLY_SNAPSHOT_KEYS, 'weekly digest snapshot')
    _validate_digest_common(
        input_snapshot=input_snapshot,
        schema='weekly_digest_input/v1',
        occurrence_key=occurrence_key,
    )
    if not isinstance(input_snapshot['project_id'], str) or not isinstance(input_snapshot['changes'], list):
        raise ValueError('weekly digest project or changes are malformed')

    if subject_type == WorkflowSubjectType.PROJECT:
        if input_snapshot['project_id'] != str(subject_id):
            raise ValueError('weekly digest project does not match work subject')
        if input_snapshot['team_id'] is not None or input_snapshot['allowed_team_ids'] != []:
            raise ValueError('project weekly digest cannot admit team-private sources')
        if input_snapshot['output_visibility_scope'] != 'project' or input_snapshot['output_team_id'] is not None:
            raise ValueError('project weekly digest output must remain project-visible')
    else:
        expected_team = str(subject_id)
        if input_snapshot['team_id'] != expected_team or input_snapshot['allowed_team_ids'] != [expected_team]:
            raise ValueError('team weekly digest must bind exactly one selected team')
        if input_snapshot['output_visibility_scope'] != 'team' or input_snapshot['output_team_id'] != expected_team:
            raise ValueError('team weekly digest output must remain team-visible')


def _identity_projection(
    *,
    work_type: str,
    subject_type: str,
    subject_id: uuid.UUID,
    contract_version: int,
    occurrence_key: str,
    input_snapshot: dict[str, object],
) -> dict[str, object]:
    if not isinstance(subject_id, uuid.UUID):
        raise ValueError('subject id must be a UUID')
    if not isinstance(input_snapshot, dict):
        raise ValueError('input snapshot must be a dictionary')

    _validate_work_identity(
        work_type=work_type,
        subject_type=subject_type,
        contract_version=contract_version,
        occurrence_key=occurrence_key,
    )
    if work_type == WorkflowWorkType.OBSERVATION_PROCESSING:
        identity_input: object = _observation_identity_input(
            subject_id=subject_id,
            input_snapshot=input_snapshot,
        )
    elif work_type == WorkflowWorkType.SESSION_DISTILLATION:
        _validate_session_snapshot(subject_id, input_snapshot)
        identity_input = input_snapshot
    elif work_type == WorkflowWorkType.DAILY_DIGEST:
        _validate_daily_snapshot(
            subject_id=subject_id,
            occurrence_key=occurrence_key,
            input_snapshot=input_snapshot,
        )
        identity_input = input_snapshot
    else:
        _validate_weekly_snapshot(
            subject_type=subject_type,
            subject_id=subject_id,
            occurrence_key=occurrence_key,
            input_snapshot=input_snapshot,
        )
        identity_input = input_snapshot

    projection = {
        'contract_version': contract_version,
        'identity_input': identity_input,
        'occurrence_key': occurrence_key,
        'subject_id': subject_id,
        'subject_type': subject_type,
        'work_type': work_type,
    }
    return projection


def work_input_fingerprint(
    *,
    work_type: str,
    subject_type: str,
    subject_id: uuid.UUID,
    contract_version: int,
    occurrence_key: str,
    input_snapshot: dict[str, object],
) -> str:
    projection = _identity_projection(
        work_type=work_type,
        subject_type=subject_type,
        subject_id=subject_id,
        contract_version=contract_version,
        occurrence_key=occurrence_key,
        input_snapshot=input_snapshot,
    )

    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _get_project(data: CreateWorkflowWorkInput) -> Project:
    try:
        return Project.objects.get(id=data.project_id, organization_id=data.organization_id)
    except Project.DoesNotExist as error:
        raise WorkflowWorkScopeError('project is outside the declared organization scope') from error


def _validate_derived_team(team_id: uuid.UUID | None, organization_id: uuid.UUID) -> None:
    if team_id is not None and not Team.objects.filter(id=team_id, organization_id=organization_id).exists():
        raise WorkflowWorkScopeError('derived team is outside the declared organization scope')


def _resolve_subject_team(data: CreateWorkflowWorkInput, project: Project) -> tuple[object, uuid.UUID | None]:
    if data.subject_type == WorkflowSubjectType.OBSERVATION:
        try:
            subject = Observation.objects.get(
                id=data.subject_id,
                organization_id=data.organization_id,
                project_id=project.id,
            )
        except Observation.DoesNotExist as error:
            raise WorkflowWorkScopeError('observation is outside the declared work scope') from error

        _validate_derived_team(subject.team_id, data.organization_id)

        return subject, subject.team_id

    if data.subject_type == WorkflowSubjectType.AGENT_SESSION:
        try:
            subject = AgentSession.objects.get(
                id=data.subject_id,
                organization_id=data.organization_id,
                project_id=project.id,
            )
        except AgentSession.DoesNotExist as error:
            raise WorkflowWorkScopeError('session is outside the declared work scope') from error

        _validate_derived_team(subject.team_id, data.organization_id)

        return subject, subject.team_id

    if data.subject_type == WorkflowSubjectType.PROJECT:
        if data.subject_id != project.id:
            raise WorkflowWorkScopeError('project subject does not match work project')

        return project, None

    if data.subject_type == WorkflowSubjectType.TEAM:
        try:
            subject = Team.objects.get(id=data.subject_id, organization_id=data.organization_id)
        except Team.DoesNotExist as error:
            raise WorkflowWorkScopeError('team is outside the declared organization scope') from error
        if not ProjectTeam.objects.filter(
            organization_id=data.organization_id,
            project_id=project.id,
            team_id=subject.id,
        ).exists():
            raise WorkflowWorkScopeError('team is not linked to the declared project')

        return subject, subject.id

    raise ValueError('unsupported workflow subject type')


def _normalize_snapshot(input_snapshot: dict[str, object]) -> dict[str, object]:
    normalized = _canonical_value(input_snapshot)
    if not isinstance(normalized, dict):
        raise ValueError('input snapshot must be a dictionary')

    return normalized


def _validate_observation_snapshot(
    subject: Observation,
    input_snapshot: dict[str, object],
) -> None:
    if input_snapshot['observation_digest'] != observation_content_digest(subject):
        raise ValueError('observation snapshot digest does not match persisted content')
    policy = input_snapshot['policy']
    if not isinstance(policy, dict) or policy['realtime_candidates_enabled'] is not True:
        raise ValueError('observation work requires the captured realtime policy')
    event_type = subject.source_metadata.get('event_type')
    if not isinstance(event_type, str):
        raise ValueError('observation is missing its trusted event type')
    if event_type in _LIFECYCLE_EVENT_TYPES:
        raise ValueError('lifecycle observations do not create observation work')


def _validate_subject_snapshot(
    data: CreateWorkflowWorkInput,
    subject: object,
    input_snapshot: dict[str, object],
) -> None:
    if isinstance(subject, Observation):
        _validate_observation_snapshot(subject, input_snapshot)
    elif data.subject_type == WorkflowSubjectType.TEAM:
        if input_snapshot['project_id'] != str(data.project_id):
            raise ValueError('digest snapshot project does not match work project')


def _verify_existing_work(
    *,
    work: WorkflowWork,
    data: CreateWorkflowWorkInput,
    team_id: uuid.UUID | None,
    proposed_projection: dict[str, object],
    allow_frozen_digest_input: bool,
) -> None:
    expected_fields = {
        'organization_id': data.organization_id,
        'project_id': data.project_id,
        'team_id': team_id,
        'work_type': data.work_type,
        'subject_type': data.subject_type,
        'subject_id': data.subject_id,
        'contract_version': data.contract_version,
        'occurrence_key': data.occurrence_key,
    }
    if any(getattr(work, field) != value for field, value in expected_fields.items()):
        raise WorkflowWorkCollisionError('existing work scope or identity does not match')

    try:
        stored_projection = _identity_projection(
            work_type=work.work_type,
            subject_type=work.subject_type,
            subject_id=work.subject_id,
            contract_version=work.contract_version,
            occurrence_key=work.occurrence_key,
            input_snapshot=work.input_snapshot,
        )
    except ValueError as error:
        raise WorkflowWorkCollisionError('existing work snapshot is not canonical') from error

    stored_fingerprint = hashlib.sha256(canonical_json_bytes(stored_projection)).hexdigest()
    if stored_fingerprint != work.input_fingerprint:
        raise WorkflowWorkCollisionError('existing work fingerprint does not match its snapshot')
    if not allow_frozen_digest_input and canonical_json_bytes(stored_projection) != canonical_json_bytes(
        proposed_projection
    ):
        raise WorkflowWorkCollisionError('existing work semantic input does not match')


def _create_non_digest_work(
    *,
    data: CreateWorkflowWorkInput,
    team_id: uuid.UUID | None,
    input_snapshot: dict[str, object],
    projection: dict[str, object],
    fingerprint: str,
) -> tuple[WorkflowWork, bool]:
    lookup = {
        'organization_id': data.organization_id,
        'project_id': data.project_id,
        'work_type': data.work_type,
        'subject_type': data.subject_type,
        'subject_id': data.subject_id,
        'contract_version': data.contract_version,
        'input_fingerprint': fingerprint,
    }
    with transaction.atomic():
        work, created = WorkflowWork.objects.get_or_create(
            **lookup,
            defaults={
                'team_id': team_id,
                'occurrence_key': data.occurrence_key,
                'input_snapshot': input_snapshot,
            },
        )
    _verify_existing_work(
        work=work,
        data=data,
        team_id=team_id,
        proposed_projection=projection,
        allow_frozen_digest_input=False,
    )

    return work, created


def _create_digest_work(
    *,
    data: CreateWorkflowWorkInput,
    team_id: uuid.UUID | None,
    input_snapshot: dict[str, object],
    projection: dict[str, object],
    fingerprint: str,
) -> tuple[WorkflowWork, bool]:
    occurrence_lookup = {
        'organization_id': data.organization_id,
        'project_id': data.project_id,
        'work_type': data.work_type,
        'subject_type': data.subject_type,
        'subject_id': data.subject_id,
        'contract_version': data.contract_version,
        'occurrence_key': data.occurrence_key,
    }
    try:
        with transaction.atomic():
            work, created = WorkflowWork.objects.get_or_create(
                **occurrence_lookup,
                defaults={
                    'team_id': team_id,
                    'input_fingerprint': fingerprint,
                    'input_snapshot': input_snapshot,
                },
            )
    except IntegrityError as error:
        work = WorkflowWork.objects.filter(**occurrence_lookup).first()
        if work is None:
            raise WorkflowWorkCollisionError('digest identity collides outside its occurrence') from error

        created = False

    _verify_existing_work(
        work=work,
        data=data,
        team_id=team_id,
        proposed_projection=projection,
        allow_frozen_digest_input=True,
    )

    return work, created


def create_work(data: CreateWorkflowWorkInput) -> tuple[WorkflowWork, bool]:
    if not transaction.get_connection().in_atomic_block:
        raise TransactionManagementError('workflow work creation requires an active transaction')
    if data.contract_version != 1:
        raise ValueError('unsupported workflow work contract version')

    project = _get_project(data)
    subject, team_id = _resolve_subject_team(data, project)
    input_snapshot = _normalize_snapshot(data.input_snapshot)
    projection = _identity_projection(
        work_type=data.work_type,
        subject_type=data.subject_type,
        subject_id=data.subject_id,
        contract_version=data.contract_version,
        occurrence_key=data.occurrence_key,
        input_snapshot=input_snapshot,
    )
    _validate_subject_snapshot(data, subject, input_snapshot)
    fingerprint = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()

    if data.work_type in _DIGEST_WORK_TYPES:
        return _create_digest_work(
            data=data,
            team_id=team_id,
            input_snapshot=input_snapshot,
            projection=projection,
            fingerprint=fingerprint,
        )

    return _create_non_digest_work(
        data=data,
        team_id=team_id,
        input_snapshot=input_snapshot,
        projection=projection,
        fingerprint=fingerprint,
    )


def _resolve_work(
    work_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    disposition: str,
    reason: str,
) -> WorkflowWork:
    with transaction.atomic():
        try:
            work = WorkflowWork.objects.select_for_update().get(
                id=work_id,
                organization_id=organization_id,
                project_id=project_id,
            )
        except WorkflowWork.DoesNotExist as error:
            raise WorkflowWorkScopeError('workflow work is outside the declared scope') from error
        if work.disposition != WorkflowWorkDisposition.REQUIRED:
            if work.disposition == disposition and work.resolution_reason == reason:
                return work

            raise WorkflowWorkStateError('workflow work already has a different terminal resolution')
        if (
            reason == WorkflowWorkResolutionReason.NO_INPUT
            and work.work_type == WorkflowWorkType.OBSERVATION_PROCESSING
        ):
            raise WorkflowWorkStateError('observation work cannot resolve as no input')

        work.disposition = disposition
        work.resolution_reason = reason
        work.resolved_at = timezone.now()
        work.save(update_fields=['disposition', 'resolution_reason', 'resolved_at', 'updated_at'])

        return work


def resolve_work_succeeded(
    work_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> WorkflowWork:
    return _resolve_work(
        work_id,
        organization_id=organization_id,
        project_id=project_id,
        disposition=WorkflowWorkDisposition.COMPLETE,
        reason=WorkflowWorkResolutionReason.SUCCEEDED,
    )


def resolve_work_no_signal(
    work_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> WorkflowWork:
    return _resolve_work(
        work_id,
        organization_id=organization_id,
        project_id=project_id,
        disposition=WorkflowWorkDisposition.COMPLETE,
        reason=WorkflowWorkResolutionReason.NO_SIGNAL,
    )


def resolve_work_no_input(
    work_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> WorkflowWork:
    return _resolve_work(
        work_id,
        organization_id=organization_id,
        project_id=project_id,
        disposition=WorkflowWorkDisposition.NO_OP,
        reason=WorkflowWorkResolutionReason.NO_INPUT,
    )
