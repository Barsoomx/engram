from __future__ import annotations

import uuid
from typing import Any

from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.request_scope import resolve_request_scope
from engram.imports.batch_services import (
    ApplyImportBatch,
    ApplyImportBatchInput,
    ImportPayloadTooLargeError,
    audit_batch_rejected,
    get_import_job,
)
from engram.imports.serializers import ImportBatchSerializer
from engram.imports.views.support import (
    authorize_job_project,
    request_too_large,
    resolve_import_organization,
)


class ImportBatchView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request, import_id: uuid.UUID) -> Response:
        scope = resolve_request_scope(request, required_capability='memories:admin', project_id=None)
        organization = resolve_import_organization(scope)
        job = get_import_job(organization, import_id)
        authorize_job_project(scope, job)

        if request_too_large(request):
            audit_batch_rejected(
                job,
                actor_id=scope.api_key_id,
                reason='payload_too_large',
                seq=self._raw_int(request.data, 'seq'),
                table=self._raw_str(request.data, 'table'),
                rows=self._raw_len(request.data, 'rows'),
            )
            raise ImportPayloadTooLargeError('import batch request exceeds the maximum size')

        serializer = ImportBatchSerializer(data=request.data)
        if not serializer.is_valid():
            audit_batch_rejected(
                job,
                actor_id=scope.api_key_id,
                reason='invalid_batch',
                seq=self._raw_int(request.data, 'seq'),
                table=self._raw_str(request.data, 'table'),
                rows=self._raw_len(request.data, 'rows'),
            )
            raise ValidationError(serializer.errors)

        data = serializer.validated_data
        result = ApplyImportBatch().execute(
            ApplyImportBatchInput(
                organization=organization,
                import_id=job.id,
                seq=data['seq'],
                table=data['table'],
                rows=[dict(row) for row in data['rows']],
                api_key_id=scope.api_key_id,
            ),
        )

        return Response(
            {
                'accepted': True,
                'seq': result.seq,
                'created': result.created,
                'duplicates': result.duplicates,
                'skipped': result.skipped,
            },
        )

    def _raw_int(self, payload: Any, key: str) -> int:
        value = payload.get(key) if isinstance(payload, dict) else None

        return value if isinstance(value, int) else -1

    def _raw_str(self, payload: Any, key: str) -> str:
        value = payload.get(key) if isinstance(payload, dict) else None

        return value if isinstance(value, str) else ''

    def _raw_len(self, payload: Any, key: str) -> int:
        value = payload.get(key) if isinstance(payload, dict) else None

        return len(value) if isinstance(value, list) else 0
