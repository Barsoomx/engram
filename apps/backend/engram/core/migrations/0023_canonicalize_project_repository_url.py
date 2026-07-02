from django.db import migrations

from engram.core.repository import canonicalize_repository_url


def canonicalize_existing_urls(apps, schema_editor) -> None:
    Project = apps.get_model('core', 'Project')

    for project in Project.objects.exclude(repository_url=''):
        canonical = canonicalize_repository_url(project.repository_url)
        if canonical and canonical != project.repository_url:
            project.repository_url = canonical
            project.save(update_fields=['repository_url'])


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0022_contextbundle_retrieval_latency_ms'),
    ]

    operations = [
        migrations.RunPython(canonicalize_existing_urls, migrations.RunPython.noop),
    ]
