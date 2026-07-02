from django.db import migrations

try:
    from pgvector.django import HnswIndex, VectorField

    _HAS_PGVECTOR = True
except ImportError:
    _HAS_PGVECTOR = False


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0023_canonicalize_project_repository_url'),
    ]

    operations = (
        [
            migrations.RemoveIndex(
                model_name='retrievaldocument',
                name='core_retdoc_emb_hnsw',
            ),
            migrations.RemoveField(
                model_name='retrievaldocument',
                name='embedding_pgvector',
            ),
            migrations.AddField(
                model_name='retrievaldocument',
                name='embedding_pgvector',
                field=VectorField(dimensions=1536, null=True, blank=True),
            ),
            migrations.AddIndex(
                model_name='retrievaldocument',
                index=HnswIndex(
                    ef_construction=64,
                    fields=['embedding_pgvector'],
                    m=16,
                    name='core_retdoc_emb_hnsw',
                    opclasses=['vector_cosine_ops'],
                ),
            ),
        ]
        if _HAS_PGVECTOR
        else []
    )
