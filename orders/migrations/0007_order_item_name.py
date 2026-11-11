# Generated for the Import Tracker shopping list feature.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0006_lookupattempt_stockitem_sizes"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="item_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The main product name used to group shopping-list items.",
                max_length=120,
            ),
        ),
    ]
