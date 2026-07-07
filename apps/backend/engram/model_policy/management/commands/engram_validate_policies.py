from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from engram.model_policy.models import ModelPolicy
from engram.model_policy.services import get_provider_gateway
from engram.model_policy.validation import (
    NO_PROJECT_AVAILABLE_ERROR_CODE,
    PolicyValidationResult,
    validate_policy,
)

__all__ = ['Command', 'NO_PROJECT_AVAILABLE_ERROR_CODE']


class Command(BaseCommand):
    help = 'Make one minimal, workflow-shaped provider call per active model policy and report pass/fail.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization', default='')
        parser.add_argument('--project', default='')

    def handle(self, *args: Any, **options: Any) -> None:
        policies = ModelPolicy.objects.filter(active=True).order_by('organization_id', 'task_type', 'name')

        organization_filter = str(options.get('organization') or '')
        if organization_filter:
            policies = policies.filter(organization_id=organization_filter)

        project_filter = str(options.get('project') or '')
        if project_filter:
            policies = policies.filter(project_id=project_filter)

        results = [validate_policy(policy, gateway_factory=get_provider_gateway) for policy in policies]

        for result in results:
            self.stdout.write(_format_result(result))

        passed = sum(1 for result in results if result.ok)
        failed = len(results) - passed
        self.stdout.write(
            f'engram_validate_policies summary: passed={passed} failed={failed} total={len(results)}',
        )


def _format_result(result: PolicyValidationResult) -> str:
    status_label = 'PASS' if result.ok else 'FAIL'
    error_suffix = f' error={result.error_code}' if result.error_code else ''

    return (
        f'policy={result.policy_id} task_type={result.task_type} '
        f'provider={result.provider} model={result.model} status={status_label}{error_suffix}'
    )
