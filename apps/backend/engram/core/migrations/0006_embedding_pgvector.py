from django.db import migrations

try:
    from pgvector.django import VectorField

    _HAS_PGVECTOR = True
except ImportError:
    _HAS_PGVECTOR = False


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0005_memorylink'),
    ]

    operations = (
        [
            migrations.CreateExtension(name='vector'),
            migrations.AddField(
                model_name='retrievaldocument',
                name='embedding_pgvector',
                field=VectorField(dimensions=64, null=True, blank=True) if _HAS_PGVECTOR else migrations.RunSQL.noop,
            ),
        ]
        if _HAS_PGVECTOR
        else []
    )
