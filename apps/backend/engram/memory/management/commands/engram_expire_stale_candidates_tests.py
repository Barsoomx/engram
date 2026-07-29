from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from engram.core.models import CandidateStatus, MemoryCandidate, Organization, Project


@pytest.fixture
def f_candidate() -> MemoryCandidate:
    organization = Organization.objects.create(name='Ttl command', slug='ttl-command')
    project = Project.objects.create(organization=organization, name='Eng', slug='eng')
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        title='Candidate',
        body='Body',
        status=CandidateStatus.PROPOSED,
        content_hash='hash-ttl-command',
        confidence='0.300',
    )
    MemoryCandidate.objects.filter(id=candidate.id).update(created_at=timezone.now() - timedelta(days=40))

    return candidate


@pytest.mark.django_db
def test_command_previews_by_default(f_candidate: MemoryCandidate) -> None:
    out = StringIO()

    call_command('engram_expire_stale_candidates', '--format', 'json', stdout=out)

    payload = json.loads(out.getvalue())
    f_candidate.refresh_from_db()
    assert payload == {
        'candidate_ids': [str(f_candidate.id)],
        'dry_run': True,
        'rejected': 0,
        'scanned': 1,
    }
    assert f_candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_command_expires_with_apply(f_candidate: MemoryCandidate) -> None:
    out = StringIO()

    call_command('engram_expire_stale_candidates', '--apply', '--format', 'json', stdout=out)

    payload = json.loads(out.getvalue())
    f_candidate.refresh_from_db()
    assert payload['dry_run'] is False
    assert payload['rejected'] == 1
    assert f_candidate.status == CandidateStatus.REJECTED
