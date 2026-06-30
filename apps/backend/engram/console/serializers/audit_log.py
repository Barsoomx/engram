from __future__ import annotations

from rest_framework import serializers

from engram.core.models import AuditEvent
from engram.core.redaction import redact_value


def _redacted(value: object) -> object:
    return redact_value(value).value


class AuditEventSerializer(serializers.ModelSerializer):
    actor_display = serializers.SerializerMethodField()
    target_display = serializers.SerializerMethodField()
    metadata = serializers.SerializerMethodField()

    class Meta:
        model = AuditEvent
        fields = [
            'id',
            'event_type',
            'actor_type',
            'actor_id',
            'actor_display',
            'target_type',
            'target_id',
            'target_display',
            'capability',
            'result',
            'request_id',
            'metadata',
            'project_id',
            'team_id',
            'created_at',
        ]
        read_only_fields = fields

    def get_actor_display(self, obj: AuditEvent) -> str | None:
        name_map: dict[str, str | None] = self.context.get('actor_name_map', {})

        return name_map.get(obj.actor_id)

    def get_target_display(self, obj: AuditEvent) -> str | None:
        name_map: dict[tuple[str, str], str | None] = self.context.get('target_name_map', {})

        return name_map.get((obj.target_type, obj.target_id))

    def get_metadata(self, obj: AuditEvent) -> object:
        return _redacted(obj.metadata)
