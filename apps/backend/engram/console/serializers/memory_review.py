from __future__ import annotations

import hashlib
from typing import Any

from rest_framework import serializers

from engram.core.models import Memory, MemoryCandidate, MemoryConflict, MemoryVersion
from engram.core.redaction import redact_value

MEMORY_TITLE_MAX_LENGTH = Memory._meta.get_field('title').max_length


class MemoryReviewActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=(
            'approve',
            'edit',
            'narrow',
            'supersede',
            'reject',
            'archive',
            'restore',
        ),
    )

    reason = serializers.CharField(min_length=1, max_length=1024)

    body = serializers.CharField(required=False, allow_blank=False, max_length=32768)

    target_memory_id = serializers.UUIDField(required=False)


class BulkArchiveSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=False,
        max_length=500,
    )

    confidence__lte = serializers.DecimalField(
        max_digits=4,
        decimal_places=3,
        required=False,
    )

    project_id = serializers.UUIDField(required=False)

    team_id = serializers.UUIDField(required=False)

    reason = serializers.CharField(min_length=1, max_length=1024)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if 'ids' not in attrs and 'confidence__lte' not in attrs:
            raise serializers.ValidationError(
                'either ids or confidence__lte must be provided',
            )

        return attrs


class BulkArchiveResultSerializer(serializers.Serializer):
    archived_count = serializers.IntegerField(read_only=True)

    archived_ids = serializers.ListField(child=serializers.UUIDField(), read_only=True)


class BulkReviewActionSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=200,
    )

    action = serializers.ChoiceField(choices=('approve', 'reject'))

    reason = serializers.CharField(min_length=1, max_length=1024)


class BulkReviewActionItemResultSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)

    outcome = serializers.ChoiceField(
        choices=('done', 'invalid_state', 'not_found'),
        read_only=True,
    )


class BulkReviewActionResultSerializer(serializers.Serializer):
    results = BulkReviewActionItemResultSerializer(many=True, read_only=True)

    done_count = serializers.IntegerField(read_only=True)

    skipped_count = serializers.IntegerField(read_only=True)


def queue_item_payload(item: MemoryCandidate | Memory) -> dict[str, Any]:
    if isinstance(item, MemoryCandidate):
        return _candidate_payload(item)

    return _memory_payload(item)


def _candidate_payload(candidate: MemoryCandidate) -> dict[str, Any]:
    observation = candidate.source_observation

    return {
        'id': str(candidate.id),
        'type': 'candidate',
        'title': candidate.title,
        'body': candidate.body,
        'status': candidate.status,
        'confidence': str(candidate.confidence) if candidate.confidence is not None else None,
        'visibility_scope': candidate.visibility_scope,
        'team_id': str(candidate.team_id) if candidate.team_id else None,
        'project_id': str(candidate.project_id),
        'evidence': candidate.evidence,
        'source_observation': _observation_payload(observation) if observation is not None else None,
        'citations': [],
        'created_at': candidate.created_at,
    }


def _memory_payload(memory: Memory) -> dict[str, Any]:
    observation = _latest_observation(memory)

    return {
        'id': str(memory.id),
        'type': 'memory',
        'title': memory.title,
        'body': memory.body,
        'status': memory.status,
        'current_version': memory.current_version,
        'refuted': memory.refuted,
        'stale': memory.stale,
        'confidence': str(memory.confidence) if memory.confidence is not None else None,
        'visibility_scope': memory.visibility_scope,
        'team_id': str(memory.team_id) if memory.team_id else None,
        'project_id': str(memory.project_id),
        'evidence': memory.metadata.get('evidence', []) if isinstance(memory.metadata, dict) else [],
        'source_observation': _observation_payload(observation) if observation is not None else None,
        'citations': [
            {
                'id': str(link.id),
                'link_type': link.link_type,
                'target': link.target,
                'label': link.label,
            }
            for link in memory.links.all()
        ],
        'created_at': memory.created_at,
    }


def _latest_observation(memory: Memory) -> Any:
    version = memory.versions.order_by('-version').first()

    if version is None or version.source_observation_id is None:
        return None

    return version.source_observation


def _observation_payload(observation: Any) -> dict[str, Any]:
    return {
        'id': str(observation.id),
        'title': observation.title,
        'files_read': observation.files_read,
        'files_modified': observation.files_modified,
    }


def version_slice(version: MemoryVersion) -> dict[str, Any]:
    return {
        'version': version.version,
        'body': version.body,
        'created_at': version.created_at,
    }


class ConflictResolveSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=(
            'publish_candidate',
            'merge_candidate',
            'supersede_memory',
            'reject_candidate',
        ),
    )

    reason = serializers.CharField(min_length=1, max_length=1024)

    target_memory_id = serializers.UUIDField(required=False)

    merged_title = serializers.CharField(required=False, allow_blank=False, max_length=MEMORY_TITLE_MAX_LENGTH)

    merged_body = serializers.CharField(required=False, allow_blank=False, max_length=32768)


def _redacted(value: str) -> str:
    return str(redact_value(value).value)


def _body_hash(value: str) -> str:
    return hashlib.sha256((value or '').encode()).hexdigest()


def _scope_of(instance: Any) -> dict[str, Any]:
    return {
        'project_id': str(instance.project_id),
        'visibility_scope': instance.visibility_scope,
        'team_id': str(instance.team_id) if instance.team_id else None,
    }


def _evidence_entries(entries: Any, observations: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in entries or []:
        observation = observations.get(entry.get('observation_id'))
        summary = _redacted(observation.title) if observation is not None else ''
        result.append(
            {
                'reference_id': entry.get('reference_id'),
                'source_kind': entry.get('source_kind'),
                'observation_id': entry.get('observation_id'),
                'summary': summary or (entry.get('observation_id') or ''),
            }
        )

    return result


def _candidate_claim(
    candidate: MemoryCandidate,
    *,
    include_body: bool,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    claim = {
        'title': _redacted(candidate.title),
        'kind': candidate.kind,
        'body_hash': _body_hash(candidate.body),
    }

    if include_body:
        claim['body'] = _redacted(candidate.body)

    if evidence is not None:
        claim['evidence'] = evidence

    return claim


def _version_title(version: MemoryVersion, memory: Memory) -> str:
    metadata = version.source_metadata if isinstance(version.source_metadata, dict) else {}
    full_text = metadata.get('full_text')

    if isinstance(full_text, str) and full_text:
        title_part, separator, _body = full_text.partition('\n\n')

        if separator:
            return title_part

    return memory.title


def _version_kind(version: MemoryVersion, memory: Memory) -> str:
    metadata = version.source_metadata if isinstance(version.source_metadata, dict) else {}
    kind = metadata.get('kind')

    if isinstance(kind, str) and kind:
        return kind

    return memory.kind


def _existing_claim(
    conflict: MemoryConflict,
    *,
    include_body: bool,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    version = conflict.memory_version

    claim = {
        'memory_id': str(conflict.memory_id),
        'version_id': str(conflict.memory_version_id),
        'title': _redacted(_version_title(version, conflict.memory)),
        'kind': _version_kind(version, conflict.memory),
        'body_hash': _body_hash(version.body),
    }

    if include_body:
        claim['body'] = _redacted(version.body)

    if evidence is not None:
        claim['evidence'] = evidence

    return claim


def _target_evidence(decision: Any, version_id: Any, observations: dict[str, Any]) -> list[dict[str, Any]]:
    if decision is None:
        return []
    for target in (decision.evidence_membership or {}).get('targets', []):
        if str(target.get('memory_version_id')) == str(version_id):
            return _evidence_entries(target.get('sources', []), observations)

    return []


def _decision_payload(decision: Any) -> dict[str, Any] | None:
    if decision is None:
        return None
    provider_call = decision.provider_call_record
    judge_status = 'succeeded' if decision.provider_call_record_id else 'not_required'

    return {
        'id': str(decision.id),
        'work_id': str(decision.work_id),
        'outcome': decision.outcome,
        'reason_code': decision.reason_code,
        'target_memory_version_id': (
            str(decision.target_memory_version_id) if decision.target_memory_version_id else None
        ),
        'transition_id': str(decision.transition_id) if decision.transition_id else None,
        'conflict_id': str(decision.conflict_id) if decision.conflict_id else None,
        'evidence_tier': decision.evidence_tier,
        'evidence_manifest_hash': decision.evidence_manifest_hash,
        'comparison_manifest_hash': decision.comparison_manifest_hash,
        'effective_scope': {
            'project_id': str(decision.project_id),
            'visibility_scope': decision.effective_visibility_scope,
            'team_id': str(decision.effective_team_id) if decision.effective_team_id else None,
        },
        'judge': {
            'status': judge_status,
            'reason': decision.redacted_reason,
            'provider_call_record_id': (
                str(decision.provider_call_record_id) if decision.provider_call_record_id else None
            ),
            'policy_id': str(decision.policy_id) if decision.policy_id else None,
            'policy_version': decision.policy_version,
            'provider': provider_call.provider if provider_call is not None else None,
            'model': provider_call.model if provider_call is not None else None,
        },
    }


def conflict_list_item(
    candidate: MemoryCandidate,
    conflicts: list[MemoryConflict],
) -> dict[str, Any]:
    ordered = sorted(conflicts, key=lambda conflict: str(conflict.id))

    return {
        'id': str(candidate.id),
        'type': 'conflict',
        'state': 'open',
        'conflict_ids': [str(conflict.id) for conflict in ordered],
        'project_id': str(candidate.project_id),
        'team_id': str(candidate.team_id) if candidate.team_id else None,
        'visibility_scope': candidate.visibility_scope,
        'reason_code': 'same_scope_contradiction',
        'opened_at': min(conflict.created_at for conflict in ordered),
        'candidate_claim': _candidate_claim(candidate, include_body=False),
        'existing_claims': [_existing_claim(conflict, include_body=False) for conflict in ordered],
    }


def conflict_detail_payload(
    candidate: MemoryCandidate,
    conflicts: list[MemoryConflict],
    etag: str,
    decisions_by_conflict: dict[Any, Any] | None = None,
    primary_decision: Any | None = None,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(conflicts, key=lambda conflict: str(conflict.id))
    decisions_by_conflict = decisions_by_conflict or {}
    observations = observations or {}

    candidate_evidence = _evidence_entries(
        (primary_decision.evidence_membership or {}).get('candidate', []) if primary_decision is not None else [],
        observations,
    )

    payload = conflict_list_item(candidate, ordered)
    payload['candidate_claim'] = _candidate_claim(candidate, include_body=True, evidence=candidate_evidence)
    payload['existing_claims'] = [
        _existing_claim(
            conflict,
            include_body=True,
            evidence=_target_evidence(
                decisions_by_conflict.get(conflict.id),
                conflict.memory_version_id,
                observations,
            ),
        )
        for conflict in ordered
    ]
    payload['candidate_id'] = str(candidate.id)
    payload['etag'] = etag
    payload['resolution_actions'] = [
        'publish_candidate',
        'merge_candidate',
        'supersede_memory',
        'reject_candidate',
    ]
    payload['conflicts'] = [
        {
            'id': str(conflict.id),
            'opened_transition_id': (str(conflict.opened_transition_id) if conflict.opened_transition_id else None),
            'decision_id': (
                str(decisions_by_conflict[conflict.id].id) if conflict.id in decisions_by_conflict else None
            ),
            'evidence_hash': conflict.evidence_hash,
        }
        for conflict in ordered
    ]
    payload['decision'] = _decision_payload(primary_decision)
    payload['effective_applicability'] = {
        'verdict': primary_decision.applicability if primary_decision is not None else '',
        'candidate': _scope_of(candidate),
        'targets': [
            {
                'memory_id': str(conflict.memory_id),
                'version_id': str(conflict.memory_version_id),
                **_scope_of(conflict.memory),
            }
            for conflict in ordered
        ],
    }

    return payload
