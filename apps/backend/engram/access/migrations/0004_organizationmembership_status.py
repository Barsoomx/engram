from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0003_admin_capabilities'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationmembership',
            name='status',
            field=models.CharField(
                choices=[('active', 'Active'), ('invited', 'Invited'), ('suspended', 'Suspended')],
                default='active',
                max_length=40,
            ),
        ),
    ]
