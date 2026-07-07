from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

import structlog
from django.db import connections
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.model_policy_validation import ValidateModelPoliciesSerializer
from engram.console.services import audit_admin_action
from engram.model_policy.models import ModelPolicy
from engram.model_policy.validation import (
    PolicyValidationResult,
    validate_policy,
    validation_timeout_failure,
)

logger = structlog.get_logger(__name__)

VALIDATION_CALL_TIMEOUT_SECONDS = 10
VALIDATION_DEADLINE_SECONDS = 25
_VALIDATION_MAX_WORKERS = 4


class ValidateModelPoliciesView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('model_policy:*'),
        ]

    def post(self, request: Request) -> Response:
        serializer = ValidateModelPoliciesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy_id: uuid.UUID | None = serializer.validated_data.get('policy_id')

        organization = request.active_organization
        policies = (
            ModelPolicy.objects.filter(organization=organization, active=True)
            .select_related('secret')
            .order_by('task_type', 'name')
        )
        if policy_id is not None:
            policies = policies.filter(id=policy_id)

        policies = list(policies)
        if policy_id is not None and not policies:
            return Response(
                {'code': 'model_policy_not_found', 'detail': 'model policy not found'},
                status=HTTP_404_NOT_FOUND,
            )

        results = _run_validations(policies)

        passed = sum(1 for result in results if result.ok)
        audit_admin_action(
            organization=organization,
            actor_identity=request.user_identity,
            event_type='ModelPolicyValidated',
            target_type='model_policy',
            target_id=str(policy_id) if policy_id is not None else 'all',
            metadata={
                'policy_count': len(results),
                'passed': passed,
                'failed': len(results) - passed,
            },
        )

        logger.info(
            'model_policies_validated',
            organization_id=str(organization.id),
            policy_count=len(results),
            passed=passed,
            failed=len(results) - passed,
        )

        return Response({'results': [_result_response(result) for result in results]})


def _run_validations(policies: list[ModelPolicy]) -> list[PolicyValidationResult]:
    if not policies:
        return []

    executor = ThreadPoolExecutor(max_workers=min(_VALIDATION_MAX_WORKERS, len(policies)))
    try:
        futures = [executor.submit(_validate_closing_connections, policy) for policy in policies]
        done, _pending = wait(futures, timeout=VALIDATION_DEADLINE_SECONDS)
        results = []
        for policy, future in zip(policies, futures, strict=True):
            if future in done:
                results.append(future.result())
            else:
                results.append(
                    validation_timeout_failure(
                        policy,
                        latency_ms=int(VALIDATION_DEADLINE_SECONDS * 1000),
                    ),
                )

        return results
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _validate_closing_connections(policy: ModelPolicy) -> PolicyValidationResult:
    try:
        return validate_policy(policy, timeout=VALIDATION_CALL_TIMEOUT_SECONDS)
    finally:
        connections.close_all()


def _result_response(result: PolicyValidationResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'policy_id': result.policy_id,
        'task_type': result.task_type,
        'provider': result.provider,
        'model': result.model,
        'ok': result.ok,
        'latency_ms': result.latency_ms,
    }
    if result.error_code:
        payload['error_code'] = result.error_code
    if result.public_error:
        payload['public_error'] = result.public_error

    return payload
