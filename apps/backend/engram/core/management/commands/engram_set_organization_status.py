from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from engram.core.models import AuditEvent, AuditResult, Organization, OrganizationStatus


class Command(BaseCommand):
    help = 'Set the lifecycle status of an organization (operator tool).'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('slug')
        parser.add_argument('status', choices=OrganizationStatus.values)
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(slug=options['slug']).first()
        if organization is None:
            raise CommandError(f'organization not found: {options["slug"]}')

        previous = organization.status
        new_status = options['status']
        organization.status = new_status
        organization.save(update_fields=['status', 'updated_at'])

        AuditEvent.objects.create(
            organization=organization,
            event_type='OrganizationStatusChanged',
            actor_type='system',
            target_type='organization',
            target_id=str(organization.id),
            result=AuditResult.RECORDED,
            metadata={'previous_status': previous, 'new_status': new_status},
        )

        if options.get('as_json'):
            self.stdout.write(
                json.dumps(
                    {
                        'organization_id': str(organization.id),
                        'slug': organization.slug,
                        'previous_status': previous,
                        'status': new_status,
                    },
                ),
            )

            return

        self.stdout.write(f'{organization.slug}: {previous} -> {new_status}')
