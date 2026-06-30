from django.db import migrations

try:
    from pgvector.django import HnswIndex

    _HAS_PGVECTOR = True
except ImportError:
    _HAS_PGVECTOR = False


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0016_organizationsettings_curator_llm_judge_enabled'),
    ]

    operations = (
        [
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
