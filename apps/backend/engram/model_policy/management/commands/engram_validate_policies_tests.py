from __future__ import annotations

import io
import uuid

import pytest
from django.core.management import call_command

from engram.context.context_api_tests import create_project_scope
from engram.core.models import Organization, Project, ProjectTeam, Team
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.management.commands.engram_validate_policies import NO_PROJECT_AVAILABLE_ERROR_CODE
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import ProviderCallInput, ProviderCallResult


class _PassingGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        return ProviderCallResult(
            provider=data.policy.provider,
            model=data.policy.model,
            call_record_id=uuid.uuid4(),
            redaction_state='clean',
            generated_title='ok',
            generated_body='ok',
        )


class _FailingGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise ModelPolicyError('provider_http_error', 'provider returned 400')


class _DisabledSecretGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise ProviderSecretError('provider secret is disabled')


class _RecordingGateway:
    def __init__(self, sink: list[ProviderCallInput]) -> None:
        self._sink = sink

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self._sink.append(data)

        return ProviderCallResult(
            provider=data.policy.provider,
            model=data.policy.model,
            call_record_id=uuid.uuid4(),
            redaction_state='clean',
            generated_title='ok',
            generated_body='ok',
        )


def _make_secret(organization: Organization, team: Team) -> ProviderSecret:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'secret-{uuid.uuid4()}',
        provider='openai',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )

    return secret


def _make_policy(
    organization: Organization,
    team: Team,
    project: Project | None,
    secret: ProviderSecret,
    *,
    task_type: str,
    name: str,
    active: bool = True,
) -> ModelPolicy:
    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name=name,
        scope='project' if project is not None else 'team',
        task_type=task_type,
        provider='openai',
        model='gpt-4o-mini',
        secret=secret,
        version=1,
        active=active,
    )


@pytest.mark.django_db
def test_validate_policies_reports_pass_and_fail_per_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    secret = _make_secret(organization, team)
    passing_policy = _make_policy(organization, team, project, secret, task_type='generation', name='passing')
    failing_policy = _make_policy(organization, team, project, secret, task_type='curation', name='failing')

    def m_get_provider_gateway(policy: ModelPolicy, **_: object) -> object:
        if policy.id == failing_policy.id:
            return _FailingGateway()

        return _PassingGateway()

    monkeypatch.setattr(
        'engram.model_policy.management.commands.engram_validate_policies.get_provider_gateway',
        m_get_provider_gateway,
    )

    stdout = io.StringIO()
    call_command('engram_validate_policies', stdout=stdout)

    output = stdout.getvalue()
    assert f'policy={passing_policy.id}' in output
    assert f'policy={failing_policy.id}' in output

    passing_line = next(line for line in output.splitlines() if f'policy={passing_policy.id}' in line)
    failing_line = next(line for line in output.splitlines() if f'policy={failing_policy.id}' in line)

    assert 'status=PASS' in passing_line
    assert 'status=FAIL' in failing_line
    assert 'error=provider_http_error' in failing_line
    assert 'engram_validate_policies summary: passed=1 failed=1 total=2' in output


@pytest.mark.django_db
def test_validate_policies_uses_candidates_response_kind_for_curation_and_single_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    secret = _make_secret(organization, team)
    generation_policy = _make_policy(organization, team, project, secret, task_type='generation', name='gen')
    curation_policy = _make_policy(organization, team, project, secret, task_type='curation', name='cur')

    captured: list[ProviderCallInput] = []
    monkeypatch.setattr(
        'engram.model_policy.management.commands.engram_validate_policies.get_provider_gateway',
        lambda _policy, **_: _RecordingGateway(captured),
    )

    call_command('engram_validate_policies', stdout=io.StringIO())

    kinds_by_policy_id = {str(data.policy.id): data.response_kind for data in captured}
    assert kinds_by_policy_id[str(generation_policy.id)] == 'single'
    assert kinds_by_policy_id[str(curation_policy.id)] == 'candidates'


@pytest.mark.django_db
def test_validate_policies_skips_inactive_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    secret = _make_secret(organization, team)
    _make_policy(organization, team, project, secret, task_type='generation', name='inactive', active=False)

    monkeypatch.setattr(
        'engram.model_policy.management.commands.engram_validate_policies.get_provider_gateway',
        lambda _policy, **_: _PassingGateway(),
    )

    stdout = io.StringIO()
    call_command('engram_validate_policies', stdout=stdout)

    assert 'engram_validate_policies summary: passed=0 failed=0 total=0' in stdout.getvalue()


@pytest.mark.django_db
def test_validate_policies_filters_by_organization_option(monkeypatch: pytest.MonkeyPatch) -> None:
    organization_a, team_a, project_a, _owner_a, _api_key_a = create_project_scope()
    secret_a = _make_secret(organization_a, team_a)
    policy_a = _make_policy(organization_a, team_a, project_a, secret_a, task_type='generation', name='org-a')

    organization_b = Organization.objects.create(name='Other Org', slug='validate-org-b')
    team_b = Team.objects.create(organization=organization_b, name='Team B', slug='team-b')
    project_b = Project.objects.create(organization=organization_b, name='Project B', slug='project-b')
    ProjectTeam.objects.create(organization=organization_b, team=team_b, project=project_b)
    secret_b = _make_secret(organization_b, team_b)
    _make_policy(organization_b, team_b, project_b, secret_b, task_type='generation', name='org-b')

    monkeypatch.setattr(
        'engram.model_policy.management.commands.engram_validate_policies.get_provider_gateway',
        lambda _policy, **_: _PassingGateway(),
    )

    stdout = io.StringIO()
    call_command('engram_validate_policies', stdout=stdout, organization=str(organization_a.id))

    output = stdout.getvalue()
    assert f'policy={policy_a.id}' in output
    assert 'total=1' in output


@pytest.mark.django_db
def test_validate_policies_reports_sanitized_code_for_provider_secret_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    secret = _make_secret(organization, team)
    policy = _make_policy(organization, team, project, secret, task_type='generation', name='disabled-secret')

    monkeypatch.setattr(
        'engram.model_policy.management.commands.engram_validate_policies.get_provider_gateway',
        lambda _policy, **_: _DisabledSecretGateway(),
    )

    stdout = io.StringIO()
    call_command('engram_validate_policies', stdout=stdout)

    policy_line = next(line for line in stdout.getvalue().splitlines() if f'policy={policy.id}' in line)
    assert 'status=FAIL' in policy_line
    assert 'error=provider_secret_unavailable' in policy_line


@pytest.mark.django_db
def test_validate_policies_marks_policy_without_available_project_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = Organization.objects.create(name='Projectless Org', slug='validate-projectless')
    secret = ProviderSecret.objects.create(
        organization=organization,
        name='org-secret',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )
    policy = ModelPolicy.objects.create(
        organization=organization,
        name='org-scoped-no-project',
        scope='organization',
        task_type='generation',
        provider='openai',
        model='gpt-4o-mini',
        secret=secret,
        version=1,
        active=True,
    )

    monkeypatch.setattr(
        'engram.model_policy.management.commands.engram_validate_policies.get_provider_gateway',
        lambda _policy, **_: _PassingGateway(),
    )

    stdout = io.StringIO()
    call_command('engram_validate_policies', stdout=stdout)

    output = stdout.getvalue()
    policy_line = next(line for line in output.splitlines() if f'policy={policy.id}' in line)
    assert 'status=FAIL' in policy_line
    assert f'error={NO_PROJECT_AVAILABLE_ERROR_CODE}' in policy_line
