import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0010_notestudyset_language_code"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoteChatSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("summary", models.TextField(blank=True)),
                ("summary_token_count", models.PositiveIntegerField(blank=True, null=True)),
                ("messages_since_summary", models.PositiveIntegerField(default=0)),
                ("total_messages", models.PositiveIntegerField(default=0)),
                ("note", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="chat_sessions", to="notes.generatednote")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="note_chat_sessions", to="accounts.user")),
            ],
            options={"ordering": ("-updated_at",)},
        ),
        migrations.CreateModel(
            name="NoteChatChunk",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("section_title", models.CharField(blank=True, max_length=255)),
                ("page_number", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("chunk_text", models.TextField()),
                ("normalized_text", models.TextField(blank=True)),
                ("chunk_hash", models.CharField(max_length=64)),
                ("source_fingerprint", models.CharField(db_index=True, max_length=64)),
                ("token_count", models.PositiveIntegerField(blank=True, null=True)),
                ("order_index", models.PositiveIntegerField()),
                ("note", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="chat_chunks", to="notes.generatednote")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="note_chat_chunks", to="accounts.user")),
            ],
            options={"ordering": ("order_index", "created_at")},
        ),
        migrations.CreateModel(
            name="NoteChatMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("role", models.CharField(choices=[("user", "User"), ("assistant", "Assistant")], max_length=16)),
                ("content", models.TextField()),
                ("source_chunk_ids", models.JSONField(blank=True, default=list)),
                ("model", models.CharField(blank=True, max_length=120)),
                ("provider", models.CharField(blank=True, max_length=64)),
                ("input_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("output_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("estimated_cost_usd", models.DecimalField(blank=True, decimal_places=8, max_digits=12, null=True)),
                ("note", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="chat_messages", to="notes.generatednote")),
                ("session", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="messages", to="notes.notechatsession")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="note_chat_messages", to="accounts.user")),
            ],
            options={"ordering": ("created_at",)},
        ),
        migrations.CreateModel(
            name="NoteChatUsageLedger",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("event_type", models.CharField(choices=[("user_message", "User message"), ("assistant_answer", "Assistant answer"), ("summary_update", "Summary update"), ("retrieval", "Retrieval"), ("embedding", "Embedding")], db_index=True, max_length=32)),
                ("provider", models.CharField(blank=True, max_length=64)),
                ("model", models.CharField(blank=True, max_length=120)),
                ("input_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("output_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("estimated_cost_usd", models.DecimalField(blank=True, decimal_places=8, max_digits=12, null=True)),
                ("note", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="chat_usage_events", to="notes.generatednote")),
                ("session", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="usage_events", to="notes.notechatsession")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="note_chat_usage_events", to="accounts.user")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(
            model_name="notechatsession",
            constraint=models.UniqueConstraint(fields=("note", "user"), name="unique_note_chat_session_per_user"),
        ),
        migrations.AddIndex(
            model_name="notechatsession",
            index=models.Index(fields=["note", "user"], name="note_chat_session_lookup_idx"),
        ),
        migrations.AddConstraint(
            model_name="notechatchunk",
            constraint=models.UniqueConstraint(fields=("note", "chunk_hash"), name="unique_note_chat_chunk_hash"),
        ),
        migrations.AddConstraint(
            model_name="notechatchunk",
            constraint=models.UniqueConstraint(fields=("note", "order_index"), name="unique_note_chat_chunk_position"),
        ),
        migrations.AddIndex(
            model_name="notechatchunk",
            index=models.Index(fields=["note", "user", "chunk_hash"], name="note_chat_chunk_lookup_idx"),
        ),
        migrations.AddIndex(
            model_name="notechatchunk",
            index=models.Index(fields=["note", "user", "order_index"], name="note_chat_chunk_order_idx"),
        ),
        migrations.AddIndex(
            model_name="notechatmessage",
            index=models.Index(fields=["session", "created_at"], name="note_chat_message_session_idx"),
        ),
        migrations.AddIndex(
            model_name="notechatmessage",
            index=models.Index(fields=["note", "user", "created_at"], name="note_chat_msg_note_user_idx"),
        ),
        migrations.AddIndex(
            model_name="notechatusageledger",
            index=models.Index(fields=["user", "created_at"], name="note_chat_usage_user_time_idx"),
        ),
        migrations.AddIndex(
            model_name="notechatusageledger",
            index=models.Index(fields=["user", "event_type", "created_at"], name="note_chat_usage_event_time_idx"),
        ),
    ]
