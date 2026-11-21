from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [("admin_ops", "0009_growthcampaign_growthvisit")]

    operations = [
        migrations.CreateModel(
            name="GrowthRecipientLink",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token", models.CharField(max_length=64, unique=True)),
                ("first_clicked_at", models.DateTimeField(blank=True, null=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="recipient_links", to="admin_ops.growthcampaign")),
                ("visit", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="recipient_link", to="admin_ops.growthvisit")),
            ],
        ),
        migrations.AddIndex(
            model_name="growthrecipientlink",
            index=models.Index(fields=["campaign", "first_clicked_at"], name="admin_ops_g_campaig_bd4211_idx"),
        ),
    ]
