from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pos", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventoryitem",
            name="received_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
