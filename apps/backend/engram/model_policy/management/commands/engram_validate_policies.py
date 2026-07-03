from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from engram.core.models import Project, ProjectTeam
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import ModelPolicy, TaskType
from engram.model_policy.services import ProviderCallInput, get_provider_gateway

VALIDATION_PROMPT = 'engram_validate_policies health check: respond with a minimal completion.'
NO_PROJECT_AVAILABLE_ERROR_CODE = 'no_project_available'


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

        results = [_validate_policy(policy) for policy in policies]

        for result in results:
            self.stdout.write(_format_result(result))

        passed = sum(1 for result in results if result['passed'])
        failed = len(results) - passed
        self.stdout.write(
            f'engram_validate_policies summary: passed={passed} failed={failed} total={len(results)}',
        )


def _validate_policy(policy: ModelPolicy) -> dict[str, Any]:
    base_result = {
        'policy_id': str(policy.id),
        'task_type': policy.task_type,
        'provider': policy.provider,
        'model': policy.model,
    }

    project = _resolve_validation_project(policy)
    if project is None:
        return {**base_result, 'passed': False, 'error_code': NO_PROJECT_AVAILABLE_ERROR_CODE}

    response_kind = 'candidates' if policy.task_type == TaskType.CURATION else 'single'
    request_id = f'engram_validate_policies:{policy.id}:{uuid.uuid4()}'
    try:
        get_provider_gateway(policy).call(
            ProviderCallInput(
                organization_id=policy.organization_id,
                project_id=project.id,
                team_id=policy.team_id,
                policy=policy,
                request_id=request_id,
                trace_id=request_id,
                prompt=VALIDATION_PROMPT,
                response_kind=response_kind,
            ),
        )
    except (ModelPolicyError, ProviderSecretError) as error:
        error_code = error.error_code or str(error)

        return {**base_result, 'passed': False, 'error_code': error_code}

    return {**base_result, 'passed': True, 'error_code': None}


def _resolve_validation_project(policy: ModelPolicy) -> Project | None:
    if policy.project_id:
        return policy.project

    if policy.team_id:
        project_team = (
            ProjectTeam.objects.filter(organization_id=policy.organization_id, team_id=policy.team_id)
            .select_related('project')
            .first()
        )
        if project_team is not None:
            return project_team.project

    return Project.objects.filter(organization_id=policy.organization_id).first()


def _format_result(result: dict[str, Any]) -> str:
    status_label = 'PASS' if result['passed'] else 'FAIL'
    error_suffix = f' error={result["error_code"]}' if result['error_code'] else ''

    return (
        f'policy={result["policy_id"]} task_type={result["task_type"]} '
        f'provider={result["provider"]} model={result["model"]} status={status_label}{error_suffix}'
    )
