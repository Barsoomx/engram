from django.db import migrations


CAPABILITIES = (
    'organizations:read',
    'organizations:admin',
    'teams:read',
    'teams:admin',
    'projects:read',
    'projects:admin',
    'members:read',
    'members:admin',
    'roles:read',
    'api_keys:read',
    'api_keys:issue',
    'api_keys:revoke',
)

ROLE_CAPABILITIES = {
    'organization_owner': CAPABILITIES,
    'organization_admin': (
        'organizations:read',
        'teams:read',
        'teams:admin',
        'projects:read',
        'projects:admin',
        'members:read',
        'members:admin',
        'roles:read',
        'api_keys:read',
        'api_keys:issue',
        'api_keys:revoke',
    ),
    'developer': (
        'projects:read',
        'teams:read',
        'api_keys:read',
    ),
    'auditor': (
        'organizations:read',
        'teams:read',
        'projects:read',
        'members:read',
        'roles:read',
        'api_keys:read',
        'audit:read',
    ),
}


def ensure_admin_capabilities(apps, schema_editor) -> None:
    Capability = apps.get_model('access', 'Capability')
    Role = apps.get_model('access', 'Role')
    RoleCapability = apps.get_model('access', 'RoleCapability')

    cap_map: dict = {}
    for code in CAPABILITIES:
        capability, _created = Capability.objects.get_or_create(code=code)
        cap_map[code] = capability

    audit_read, _created = Capability.objects.get_or_create(code='audit:read')
    cap_map['audit:read'] = audit_read

    for role_code, codes in ROLE_CAPABILITIES.items():
        try:
            role = Role.objects.get(code=role_code)
        except Role.DoesNotExist:

            continue

        for code in codes:
            RoleCapability.objects.get_or_create(role=role, capability=cap_map[code])


class Migration(migrations.Migration):

    dependencies = [
        ('access', '0002_seed_default_roles'),
    ]

    operations = [
        migrations.RunPython(ensure_admin_capabilities, migrations.RunPython.noop),
    ]
