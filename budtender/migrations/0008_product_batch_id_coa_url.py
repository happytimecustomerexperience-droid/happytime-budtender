from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('budtender', '0007_phonecartdraft_customer_name_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='batch_id',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name='product',
            name='coa_url',
            field=models.URLField(blank=True, max_length=500),
        ),
    ]
