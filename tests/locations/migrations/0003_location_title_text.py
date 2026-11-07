from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("locations", "0002_api_views")]

    operations = [
        migrations.AlterField(
            model_name="location",
            name="title",
            field=models.TextField(),
        ),
    ]
