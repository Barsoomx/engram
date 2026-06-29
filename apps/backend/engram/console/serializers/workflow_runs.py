from __future__ import annotations

from typing import Any

from rest_framework import serializers

from engram.core.models import WorkflowRun


class WorkflowRunListSerializer(serializers.ModelSerializer):
    project_id = serializers.PrimaryKeyRelatedField(source='project', read_only=True)

    team_id = serializers.PrimaryKeyRelatedField(source='team', read_only=True)

    result_memory_id = serializers.PrimaryKeyRelatedField(source='result_memory', read_only=True)

    class Meta:
        model = WorkflowRun

        fields = [
            'id',
            'organization_id',
            'project_id',
            'team_id',
            'run_type',
            'status',
            'escalation',
            'request_id',
            'correlation_id',
            'result_memory_id',
            'started_at',
            'finished_at',
            'created_at',
        ]

        read_only_fields = fields


class WorkflowRunDetailSerializer(serializers.ModelSerializer):
    project_id = serializers.PrimaryKeyRelatedField(source='project', read_only=True)

    team_id = serializers.PrimaryKeyRelatedField(source='team', read_only=True)

    input_snapshot = serializers.JSONField(read_only=True)

    provider_call_ids = serializers.JSONField(read_only=True)

    result_memory = serializers.SerializerMethodField()

    curator_actions = serializers.SerializerMethodField()

    provider_calls = serializers.SerializerMethodField()

    rerun_of_id = serializers.PrimaryKeyRelatedField(source='rerun_of', read_only=True)

    class Meta:
        model = WorkflowRun

        fields = [
            'id',
            'organization_id',
            'project_id',
            'team_id',
            'run_type',
            'status',
            'input_snapshot',
            'provider_call_ids',
            'result_memory',
            'curator_actions',
            'provider_calls',
            'escalation',
            'failure_reason',
            'request_id',
            'correlation_id',
            'started_at',
            'finished_at',
            'created_at',
            'rerun_of_id',
        ]

        read_only_fields = fields

    def get_result_memory(self, obj: WorkflowRun) -> dict[str, Any] | None:
        memory = obj.result_memory

        if memory is None:
            return None

        return {
            'id': str(memory.id),
            'title': memory.title,
            'status': memory.status,
        }

    def get_curator_actions(self, obj: WorkflowRun) -> list[dict[str, Any]]:
        from engram.core.models import AuditEvent

        join_values = [value for value in (obj.request_id, obj.correlation_id) if value]

        if not join_values:
            return []

        events = AuditEvent.objects.filter(
            organization_id=obj.organization_id,
            request_id__in=join_values,
        )

        return [
            {
                'id': str(event.id),
                'event_type': event.event_type,
                'actor_type': event.actor_type,
                'target_type': event.target_type,
                'target_id': event.target_id,
                'result': event.result,
                'created_at': event.created_at.isoformat(),
            }
            for event in events.order_by('created_at')
        ]

    def get_provider_calls(self, obj: WorkflowRun) -> list[dict[str, Any]]:
        from engram.model_policy.models import ProviderCallRecord

        call_ids = obj.provider_call_ids or []

        if not call_ids:
            return []

        records = ProviderCallRecord.objects.filter(
            organization_id=obj.organization_id,
            id__in=call_ids,
        )

        return [
            {
                'id': str(record.id),
                'provider': record.provider,
                'model': record.model,
                'task_type': record.task_type,
                'result': record.result,
                'latency_ms': record.latency_ms,
            }
            for record in records.order_by('created_at')
        ]
