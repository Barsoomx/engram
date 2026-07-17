from __future__ import annotations

import hashlib
from typing import Any

from rest_framework import serializers

from engram.core.models import Memory, MemoryCandidate, MemoryConflict, MemoryVersion
from engram.core.redaction import redact_value


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

    merged_title = serializers.CharField(required=False, allow_blank=False, max_length=32768)

    merged_body = serializers.CharField(required=False, allow_blank=False, max_length=32768)


def _redacted(value: str) -> str:
    return str(redact_value(value).value)


def _body_hash(value: str) -> str:
    return hashlib.sha256((value or '').encode()).hexdigest()


def _candidate_claim(candidate: MemoryCandidate, *, include_body: bool) -> dict[str, Any]:
    claim = {
        'title': _redacted(candidate.title),
        'kind': candidate.kind,
        'body_hash': _body_hash(candidate.body),
    }

    if include_body:
        claim['body'] = _redacted(candidate.body)

    return claim


def _existing_claim(conflict: MemoryConflict, *, include_body: bool) -> dict[str, Any]:
    memory = conflict.memory

    claim = {
        'memory_id': str(conflict.memory_id),
        'version_id': str(conflict.memory_version_id),
        'title': _redacted(memory.title),
        'kind': memory.kind,
        'body_hash': _body_hash(memory.body),
    }

    if include_body:
        claim['body'] = _redacted(memory.body)

    return claim


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
) -> dict[str, Any]:
    ordered = sorted(conflicts, key=lambda conflict: str(conflict.id))

    payload = conflict_list_item(candidate, ordered)
    payload['candidate_claim'] = _candidate_claim(candidate, include_body=True)
    payload['existing_claims'] = [_existing_claim(conflict, include_body=True) for conflict in ordered]
    payload['candidate_id'] = str(candidate.id)
    payload['etag'] = etag
    payload['resolution_actions'] = [
        'publish_candidate',
        'merge_candidate',
        'supersede_memory',
        'reject_candidate',
    ]

    return payload
