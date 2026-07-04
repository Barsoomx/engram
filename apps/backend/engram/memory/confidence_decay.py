from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from engram.core.models import (
    AuditEvent,
    AuditResult,
    Memory,
    MemoryStatus,
    Organization,
    OrganizationSettings,
    Project,
)
from engram.memory.services import redact_value

_CONFIDENCE_QUANTIZE = Decimal('0.001')
_MAX_AUDIT_MEMORY_IDS = 200


@dataclass
class DecayMemoryConfidenceResult:
    organizations: int
    projects: int
    memories: int


class DecayMemoryConfidence:
    def execute(self) -> DecayMemoryConfidenceResult:
        organizations = 0
        projects = 0
        memories = 0

        for organization in Organization.objects.all():
            if not resolve_confidence_decay_enabled(organization):
                continue

            organizations += 1

            for project in Project.objects.filter(organization=organization):
                decayed_ids = self._decay_project(organization, project)
                if not decayed_ids:
                    continue

                projects += 1
                memories += len(decayed_ids)
                self._audit(organization, project, decayed_ids)

        return DecayMemoryConfidenceResult(
            organizations=organizations,
            projects=projects,
            memories=memories,
        )

    def _decay_project(self, organization: Organization, project: Project) -> list[uuid.UUID]:
        cutoff = timezone.now() - timedelta(days=settings.ENGRAM_CONFIDENCE_DECAY_MIN_AGE_DAYS)
        step = settings.ENGRAM_CONFIDENCE_DECAY_STEP
        floor = settings.ENGRAM_CONFIDENCE_DECAY_FLOOR

        candidates = Memory.objects.filter(
            organization=organization,
            project=project,
            status=MemoryStatus.APPROVED,
            stale=False,
            refuted=False,
            confidence__isnull=False,
            confidence__gt=floor,
            updated_at__lt=cutoff,
        ).exclude(kind='digest')

        decayed_ids: list[uuid.UUID] = []
        for memory in candidates:
            memory.confidence = max(floor, memory.confidence - step).quantize(_CONFIDENCE_QUANTIZE)
            memory.save(update_fields=['confidence', 'updated_at'])
            decayed_ids.append(memory.id)

        return decayed_ids

    def _audit(self, organization: Organization, project: Project, decayed_ids: list[uuid.UUID]) -> None:
        metadata = {
            'memory_ids': [str(value) for value in decayed_ids[:_MAX_AUDIT_MEMORY_IDS]],
            'count': len(decayed_ids),
            'step': str(settings.ENGRAM_CONFIDENCE_DECAY_STEP),
            'floor': str(settings.ENGRAM_CONFIDENCE_DECAY_FLOOR),
        }

        AuditEvent.objects.create(
            organization=organization,
            project=project,
            event_type='MemoryConfidenceDecayed',
            actor_type='system',
            actor_id='curator',
            capability='memories:review',
            result=AuditResult.RECORDED,
            metadata=redact_value(metadata),
        )


def resolve_confidence_decay_enabled(organization: Organization) -> bool:
    enabled = (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list('confidence_decay_enabled', flat=True)
        .first()
    )
    if enabled is None:
        return True

    return enabled
