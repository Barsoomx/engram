from __future__ import annotations

from rest_framework import serializers

IMPORT_TABLES = ('sdk_sessions', 'user_prompts', 'observations', 'session_summaries')
MAX_BATCH_ROWS = 200
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class ImportManifestTablesSerializer(serializers.Serializer):
    sdk_sessions = serializers.IntegerField(min_value=0)
    user_prompts = serializers.IntegerField(min_value=0)
    observations = serializers.IntegerField(min_value=0)
    session_summaries = serializers.IntegerField(min_value=0)


class ImportManifestSerializer(serializers.Serializer):
    schema_version_head = serializers.IntegerField(min_value=0)
    tables = ImportManifestTablesSerializer()


class CreateImportJobSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    source_store_id = serializers.CharField(max_length=255)
    manifest = ImportManifestSerializer()


class ImportBatchSerializer(serializers.Serializer):
    seq = serializers.IntegerField(min_value=0)
    table = serializers.ChoiceField(choices=IMPORT_TABLES)
    rows = serializers.ListField(
        child=serializers.DictField(),
        max_length=MAX_BATCH_ROWS,
        allow_empty=True,
    )


class FinalizeImportSerializer(serializers.Serializer):
    client_row_counts = serializers.DictField(child=serializers.IntegerField(min_value=0))
