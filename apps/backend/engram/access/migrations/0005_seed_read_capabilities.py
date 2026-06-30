from django.db import migrations


NEW_CAPABILITIES = (
    ('secrets:read', 'Read provider secret inventory.'),
    ('model_policy:read', 'Read model policies.'),
    ('context:read', 'Read context bundles.'),
)

ROLE_CODES = (
    'organization_owner',
    'organization_admin',
    'auditor',
    'developer',
)


def seed_read_capabilities(apps, schema_editor) -> None:
    Capability = apps.get_model('access', 'Capability')
    Role = apps.get_model('access', 'Role')
    RoleCapability = apps.get_model('access', 'RoleCapability')

    cap_map: dict = {}
    for code, description in NEW_CAPABILITIES:
        capability, _created = Capability.objects.get_or_create(
            code=code,
            defaults={'description': description},
        )
        cap_map[code] = capability

    for role_code in ROLE_CODES:
        try:
            role = Role.objects.get(code=role_code)
        except Role.DoesNotExist:
            continue

        for cap in cap_map.values():
            RoleCapability.objects.get_or_create(role=role, capability=cap)


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0004_organizationmembership_status'),
    ]

    operations = [
        migrations.RunPython(seed_read_capabilities, migrations.RunPython.noop),
    ]
