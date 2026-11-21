from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0002_generatednote_generation_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="generatednote",
            name="model_name",
            field=models.CharField(db_index=True, default="gemini-2.5-flash-lite", max_length=120),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="generatednote",
            name="variant",
            field=models.CharField(db_index=True, default="gemini-2-5-flash-lite", max_length=100),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="generatednote",
            name="lecture",
            field=models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="generated_notes", to="lectures.lecture"),
        ),
        migrations.AddConstraint(
            model_name="generatednote",
            constraint=models.UniqueConstraint(fields=("lecture", "variant"), name="unique_generated_note_variant_per_lecture"),
        ),
    ]
