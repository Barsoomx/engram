from django.db import migrations

CAPABILITY_CODE = 'projects:agent'
CAPABILITY_DESCRIPTION = 'Resolve/auto-create and operate on any project in the organization (org-wide agent key).'
ROLE_CODES = ('organization_owner', 'organization_admin')


def seed_projects_agent_capability(apps, schema_editor) -> None:
    Capability = apps.get_model('access', 'Capability')
    Role = apps.get_model('access', 'Role')
    RoleCapability = apps.get_model('access', 'RoleCapability')

    capability, _created = Capability.objects.get_or_create(
        code=CAPABILITY_CODE,
        defaults={'description': CAPABILITY_DESCRIPTION},
    )

    for role_code in ROLE_CODES:
        try:
            role = Role.objects.get(code=role_code)
        except Role.DoesNotExist:
            continue

        RoleCapability.objects.get_or_create(role=role, capability=capability)


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0006_apikey_access_apik_key_pre_a1ebd1_idx'),
    ]

    operations = [
        migrations.RunPython(seed_projects_agent_capability, migrations.RunPython.noop),
    ]
