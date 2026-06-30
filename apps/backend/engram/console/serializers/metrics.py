from __future__ import annotations

from rest_framework import serializers


class OverviewMetricsSerializer(serializers.Serializer):
    memories_indexed = serializers.IntegerField(read_only=True)
    memories_indexed_delta = serializers.IntegerField(read_only=True)
    context_bundles_7d = serializers.IntegerField(read_only=True)
    context_bundles_7d_delta = serializers.IntegerField(read_only=True)
    connected_agents = serializers.IntegerField(read_only=True)
    avg_retrieval_latency_ms = serializers.FloatField(allow_null=True, read_only=True)
    avg_retrieval_latency_measured = serializers.BooleanField(read_only=True)


class MemoryIngestDailyItemSerializer(serializers.Serializer):
    date = serializers.CharField(read_only=True)
    count = serializers.IntegerField(read_only=True)


class SessionItemSerializer(serializers.Serializer):
    session_id = serializers.CharField(read_only=True)
    agent_name = serializers.CharField(read_only=True)
    model_id = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    last_seen = serializers.CharField(read_only=True)


class ActivityItemSerializer(serializers.Serializer):
    event_type = serializers.CharField(read_only=True)
    actor_type = serializers.CharField(read_only=True)
    actor_id = serializers.CharField(read_only=True)
    target_type = serializers.CharField(read_only=True)
    target_id = serializers.CharField(read_only=True)
    result = serializers.CharField(read_only=True)
    created_at = serializers.CharField(read_only=True)
