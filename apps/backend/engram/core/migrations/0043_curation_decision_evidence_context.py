from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0042_memory_confidence_decayed_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='curationdecision',
            name='applicability',
            field=models.CharField(blank=True, db_default='', default='', max_length=20),
        ),
        migrations.AddField(
            model_name='curationdecision',
            name='evidence_membership',
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
    ]
