from __future__ import annotations

from rest_framework import serializers

from engram.imports.models import ImportJob


class ImportJobSerializer(serializers.ModelSerializer):
    project_name = serializers.SerializerMethodField()

    class Meta:
        model = ImportJob
        fields = (
            'id',
            'source_store_id',
            'status',
            'project',
            'project_name',
            'team',
            'manifest',
            'batches_applied',
            'rows_created',
            'rows_duplicate',
            'report',
            'failure_reason',
            'created_at',
            'updated_at',
        )

    def get_project_name(self, obj: ImportJob) -> str:
        return obj.project.name if obj.project_id else ''
