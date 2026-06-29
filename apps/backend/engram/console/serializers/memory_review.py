from __future__ import annotations

from typing import Any

from rest_framework import serializers

from engram.core.models import Memory, MemoryCandidate, MemoryVersion


class SourceObservationSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)

    title = serializers.CharField(read_only=True)

    files_read = serializers.JSONField(read_only=True)

    files_modified = serializers.JSONField(read_only=True)


class CitationSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)

    link_type = serializers.CharField(read_only=True)

    target = serializers.CharField(read_only=True)

    label = serializers.CharField(read_only=True)


class ReviewQueueItemSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)

    type = serializers.CharField(read_only=True)

    title = serializers.CharField(read_only=True)

    body = serializers.CharField(read_only=True)

    status = serializers.CharField(read_only=True)

    confidence = serializers.SerializerMethodField()

    visibility_scope = serializers.CharField(read_only=True)

    team_id = serializers.UUIDField(read_only=True, allow_null=True)

    project_id = serializers.UUIDField(read_only=True)

    evidence = serializers.JSONField(read_only=True)

    source_observation = SourceObservationSummarySerializer(read_only=True)

    citations = CitationSerializer(many=True, read_only=True)

    created_at = serializers.DateTimeField(read_only=True)

    def get_confidence(self, obj: Any) -> str | None:
        value = obj.confidence

        if value is None:
            return None

        return str(value)


class MemoryVersionSliceSerializer(serializers.Serializer):
    version = serializers.IntegerField(read_only=True)

    body = serializers.CharField(read_only=True)

    created_at = serializers.DateTimeField(read_only=True)


class MemoryReviewActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=(
            'approve',
            'edit',
            'narrow',
            'supersede',
            'reject',
            'archive',
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
