from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.accounts.models import get_system_user_pk
from apps.common.models import BaseModel
from apps.notes.themes import DEFAULT_NOTE_THEME, NOTE_THEMES


class NoteGenerationStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not Started"
    QUEUED = "QUEUED", "Queued"
    SUMMARIZING = "SUMMARIZING", "Summarizing"
    STRUCTURING = "STRUCTURING", "Structuring"
    RENDERING = "RENDERING", "Rendering"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class NoteStudySetStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class NoteStudyDifficulty(models.TextChoices):
    EASY = "easy", "Easy"
    MEDIUM = "medium", "Medium"
    HARD = "hard", "Hard"
    MIXED = "mixed", "Mixed"


class NoteFlashcardStatus(models.TextChoices):
    NEW = "new", "New"
    KNOWN = "known", "Known"
    REVIEW = "review", "Review"


class NoteChatMessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"


class NoteChatUsageEventType(models.TextChoices):
    USER_MESSAGE = "user_message", "User message"
    ASSISTANT_ANSWER = "assistant_answer", "Assistant answer"
    SUMMARY_UPDATE = "summary_update", "Summary update"
    RETRIEVAL = "retrieval", "Retrieval"
    EMBEDDING = "embedding", "Embedding"


class NoteFolder(BaseModel):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_folders")
    parent = models.ForeignKey(
        "notes.NoteFolder",
        on_delete=models.CASCADE,
        related_name="children",
        blank=True,
        null=True,
    )
    name = models.CharField(max_length=120)

    class Meta:
        ordering = ("name", "created_at")
        constraints = [
            models.UniqueConstraint(fields=("user", "parent", "name"), name="unique_note_folder_name_per_parent"),
        ]
        indexes = [
            models.Index(fields=["user", "parent", "name"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self) -> str:
        return self.name


class ActiveGeneratedNoteManager(models.Manager):
    """Notes the user has not deleted. A deleted note stays in `all_objects` until it is purged."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class GeneratedNote(BaseModel):
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notes",
        default=get_system_user_pk,
    )
    folder = models.ForeignKey(
        "notes.NoteFolder",
        on_delete=models.SET_NULL,
        related_name="notes",
        blank=True,
        null=True,
    )
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="generated_notes")
    variant = models.CharField(max_length=100, db_index=True)
    model_name = models.CharField(max_length=120, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=NoteGenerationStatus.choices,
        default=NoteGenerationStatus.NOT_STARTED,
        db_index=True,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    title = models.CharField(max_length=255, blank=True)
    theme = models.CharField(max_length=32, choices=[(theme, theme) for theme in NOTE_THEMES], default=DEFAULT_NOTE_THEME)
    summary_text = models.TextField(blank=True)
    semantic_payload = models.JSONField(default=dict, blank=True)
    body_html = models.TextField(blank=True)
    html_path = models.CharField(max_length=500, blank=True)
    pdf_path = models.CharField(max_length=500, blank=True)
    generation_metadata = models.JSONField(default=dict, blank=True)
    prompt_metadata = models.JSONField(default=dict, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    editor_revision = models.PositiveIntegerField(default=0)
    edited_at = models.DateTimeField(null=True, blank=True)
    first_opened_at = models.DateTimeField(null=True, blank=True)
    feedback_dismissed_at = models.DateTimeField(null=True, blank=True)
    # Set when the user deletes the note from the app. The note is hidden at once and removed from the
    # database and storage for good one day later (apps.notes.lifecycle). Nothing else deletes notes.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ActiveGeneratedNoteManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("lecture", "variant"),
                condition=Q(deleted_at__isnull=True),
                name="unique_generated_note_variant_per_lecture",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "folder", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["lecture", "created_at"]),
            models.Index(fields=["theme", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Generated note for {self.lecture_id} ({self.variant})"


class NoteFeedbackReason(models.TextChoices):
    WRONG_TERMS = "wrong_terms", "اصطلاحات غلط"
    MISSING_SECTION = "missing_section", "بخشی جا افتاده"
    WRONG_FORMULA = "wrong_formula", "فرمول اشتباه"
    BAD_STRUCTURE = "bad_structure", "ساختار بد"
    OTHER = "other", "مورد دیگر"


class NoteFeedback(BaseModel):
    """One end-of-note rating per (user, note)."""

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_feedback")
    note = models.ForeignKey(GeneratedNote, on_delete=models.CASCADE, related_name="feedback")
    rating = models.PositiveSmallIntegerField()
    reasons = models.JSONField(default=list, blank=True)
    comment = models.TextField(blank=True)
    allow_public = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("user", "note"), name="unique_note_feedback_per_user_note"),
            models.CheckConstraint(condition=Q(rating__gte=1, rating__lte=5), name="note_feedback_rating_1_to_5"),
        ]
        indexes = [models.Index(fields=["rating", "created_at"])]

    def __str__(self) -> str:
        return f"Feedback {self.rating}/5 for note {self.note_id}"


class GeneratedNoteSummary(BaseModel):
    """A concise, independently-rendered study summary for one completed note."""

    source_note = models.OneToOneField(
        "notes.GeneratedNote",
        on_delete=models.CASCADE,
        related_name="study_summary",
    )
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_summaries")
    status = models.CharField(
        max_length=32,
        choices=NoteGenerationStatus.choices,
        default=NoteGenerationStatus.NOT_STARTED,
        db_index=True,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    title = models.CharField(max_length=255, blank=True)
    overview = models.TextField(blank=True)
    semantic_payload = models.JSONField(default=dict, blank=True)
    body_html = models.TextField(blank=True)
    html_path = models.CharField(max_length=500, blank=True)
    pdf_path = models.CharField(max_length=500, blank=True)
    generation_metadata = models.JSONField(default=dict, blank=True)
    source_fingerprint = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    download_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "status", "created_at"]),
            models.Index(fields=["source_note", "status"]),
        ]

    def __str__(self) -> str:
        return f"Study summary for {self.source_note_id}"


class NoteImage(BaseModel):
    """A user-uploaded photo embedded inside a note's editable body."""

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="note_images",
        default=get_system_user_pk,
    )
    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="images")
    storage_path = models.CharField(max_length=500, unique=True)
    content_type = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["note", "created_at"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Image for note {self.note_id}"


class NotePdf(BaseModel):
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="pdfs",
        default=get_system_user_pk,
    )
    note = models.OneToOneField("notes.GeneratedNote", on_delete=models.CASCADE, related_name="pdf_record")
    storage_path = models.CharField(max_length=500, unique=True)
    download_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["note", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"PDF for note {self.note_id}"


class NoteStudySet(BaseModel):
    """One saved Gemini response that powers both flashcards and practice questions."""

    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="study_sets")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_study_sets")
    requested_count = models.PositiveSmallIntegerField()
    difficulty = models.CharField(max_length=16, choices=NoteStudyDifficulty.choices)
    # A set is valid only for the output language of the note that produced it.
    language_code = models.CharField(max_length=8, default="fa", db_index=True)
    status = models.CharField(
        max_length=16,
        choices=NoteStudySetStatus.choices,
        default=NoteStudySetStatus.PENDING,
        db_index=True,
    )
    generated_count = models.PositiveSmallIntegerField(default=0)
    model_name = models.CharField(max_length=120, blank=True)
    provider_name = models.CharField(max_length=64, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    total_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)
    error_message = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("note", "requested_count", "difficulty"),
                condition=Q(status=NoteStudySetStatus.PENDING),
                name="unique_pending_note_study_set_request",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "note", "status", "created_at"], name="notes_notes_user_id_92ab3c_idx"),
            models.Index(fields=["note", "language_code", "requested_count", "difficulty", "status"], name="note_set_language_lookup_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.requested_count < 1:
            raise ValidationError({"requested_count": "تعداد آیتم‌های مطالعه باید مثبت باشد."})

    def __str__(self) -> str:
        return f"Study set for {self.note_id} ({self.generated_count} items)"


class NoteStudyItem(BaseModel):
    study_set = models.ForeignKey("notes.NoteStudySet", on_delete=models.CASCADE, related_name="items")
    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="study_items")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_study_items")
    order_index = models.PositiveSmallIntegerField()
    source_section_title = models.CharField(max_length=255, blank=True)
    concept = models.CharField(max_length=255)
    flashcard_front = models.TextField()
    flashcard_back = models.TextField()
    mcq_question = models.TextField()
    options = models.JSONField(default=list)
    correct_option_index = models.PositiveSmallIntegerField()
    explanation = models.TextField(blank=True)
    difficulty = models.CharField(max_length=16, choices=NoteStudyDifficulty.choices)
    source_quote = models.TextField(blank=True)
    flashcard_status = models.CharField(
        max_length=16,
        choices=NoteFlashcardStatus.choices,
        default=NoteFlashcardStatus.NEW,
        db_index=True,
    )
    flashcard_reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("order_index", "created_at")
        constraints = [
            models.UniqueConstraint(fields=("study_set", "order_index"), name="unique_note_study_item_position"),
            models.CheckConstraint(
                condition=Q(correct_option_index__gte=0) & Q(correct_option_index__lte=3),
                name="note_study_item_correct_option_range",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "note", "created_at"], name="notes_notes_user_id_954f36_idx"),
            models.Index(fields=["study_set", "order_index"], name="notes_notes_study_s8a9a4_idx"),
            models.Index(fields=["user", "note", "flashcard_status"], name="note_item_user_status_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if not isinstance(self.options, list) or len(self.options) != 4:
            raise ValidationError({"options": "هر سوال باید دقیقاً چهار گزینه داشته باشد."})
        if any(not isinstance(option, str) or not option.strip() for option in self.options):
            raise ValidationError({"options": "گزینه‌ها باید متن غیرخالی باشند."})
        if self.correct_option_index >= len(self.options):
            raise ValidationError({"correct_option_index": "گزینه صحیح معتبر نیست."})

    def __str__(self) -> str:
        return f"Study item {self.order_index} for {self.note_id}"


class NoteChatChunk(BaseModel):
    """Deterministic chunks of final note HTML used by the future tutor RAG flow."""

    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="chat_chunks")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_chat_chunks")
    section_title = models.CharField(max_length=255, blank=True)
    page_number = models.PositiveSmallIntegerField(null=True, blank=True)
    chunk_text = models.TextField()
    normalized_text = models.TextField(blank=True)
    chunk_hash = models.CharField(max_length=64)
    source_fingerprint = models.CharField(max_length=64, db_index=True)
    token_count = models.PositiveIntegerField(null=True, blank=True)
    order_index = models.PositiveIntegerField()

    class Meta:
        ordering = ("order_index", "created_at")
        constraints = [
            models.UniqueConstraint(fields=("note", "chunk_hash"), name="unique_note_chat_chunk_hash"),
            models.UniqueConstraint(fields=("note", "order_index"), name="unique_note_chat_chunk_position"),
        ]
        indexes = [
            models.Index(fields=["note", "user", "chunk_hash"], name="note_chat_chunk_lookup_idx"),
            models.Index(fields=["note", "user", "order_index"], name="note_chat_chunk_order_idx"),
        ]

    def __str__(self) -> str:
        return f"Chat chunk {self.order_index} for {self.note_id}"


class NoteChatSession(BaseModel):
    """One stable tutor session per user and generated note."""

    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="chat_sessions")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_chat_sessions")
    summary = models.TextField(blank=True)
    summary_token_count = models.PositiveIntegerField(null=True, blank=True)
    messages_since_summary = models.PositiveIntegerField(default=0)
    total_messages = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(fields=("note", "user"), name="unique_note_chat_session_per_user"),
        ]
        indexes = [
            models.Index(fields=["note", "user"], name="note_chat_session_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"Chat session for {self.note_id}"


class NoteChatMessage(BaseModel):
    """Persisted visible chat history; Phase 4A does not create assistant messages."""

    session = models.ForeignKey("notes.NoteChatSession", on_delete=models.CASCADE, related_name="messages")
    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="chat_messages")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_chat_messages")
    role = models.CharField(max_length=16, choices=NoteChatMessageRole.choices)
    content = models.TextField()
    source_chunk_ids = models.JSONField(default=list, blank=True)
    model = models.CharField(max_length=120, blank=True)
    provider = models.CharField(max_length=64, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            models.Index(fields=["session", "created_at"], name="note_chat_message_session_idx"),
            models.Index(fields=["note", "user", "created_at"], name="note_chat_msg_note_user_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.role} message for {self.note_id}"


class NoteChatUsageLedger(BaseModel):
    """Append-only usage accounting for tutor quotas and future model spending."""

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_chat_usage_events")
    note = models.ForeignKey(
        "notes.GeneratedNote",
        on_delete=models.SET_NULL,
        related_name="chat_usage_events",
        null=True,
        blank=True,
    )
    session = models.ForeignKey(
        "notes.NoteChatSession",
        on_delete=models.SET_NULL,
        related_name="usage_events",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=32, choices=NoteChatUsageEventType.choices, db_index=True)
    provider = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=120, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "created_at"], name="note_chat_usage_user_time_idx"),
            models.Index(fields=["user", "event_type", "created_at"], name="note_chat_usage_event_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} usage for {self.user_id}"


class NoteReviewState(models.TextChoices):
    READY = "READY", "Ready"
    UNAVAILABLE = "UNAVAILABLE", "Unavailable"


class NoteReview(BaseModel):
    """Accuracy-check summary for one note (see services/note_review)."""

    note = models.OneToOneField("notes.GeneratedNote", on_delete=models.CASCADE, related_name="review")
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="note_reviews",
        default=get_system_user_pk,
    )
    state = models.CharField(max_length=16, choices=NoteReviewState.choices, db_index=True)
    reason = models.CharField(max_length=64, blank=True)
    flagged_count = models.PositiveSmallIntegerField(default=0)
    # Share of lecture time that was heard clearly. Hidden from users until calibrated.
    clear_ratio = models.FloatField(null=True, blank=True)
    timeline_ok = models.BooleanField(default=False)
    algorithm_version = models.PositiveSmallIntegerField(default=1)
    details = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Review for note {self.note_id} ({self.state})"


class NoteReviewSpot(BaseModel):
    """One passage the speech recognizer was unsure about, with an optional saved audio clip."""

    review = models.ForeignKey("notes.NoteReview", on_delete=models.CASCADE, related_name="spots")
    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="review_spots")
    order = models.PositiveSmallIntegerField()
    section_index = models.PositiveSmallIntegerField()
    heading_text = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=24, default="HEARING")
    start_s = models.FloatField()
    end_s = models.FloatField()
    badness = models.FloatField(default=0)
    alignment_score = models.FloatField(default=0)
    reason_codes = models.JSONField(default=list, blank=True)
    snippet = models.CharField(max_length=300, blank=True)
    clip_path = models.CharField(max_length=500, blank=True)
    clip_bytes = models.PositiveIntegerField(default=0)
    clip_start_s = models.FloatField(default=0)
    clip_duration_s = models.FloatField(default=0)
    # Position inside the section (0 = its first words, 1 = its last), so the editor can place the
    # marker next to the paragraph that was being said; empty on reviews made before this existed.
    section_fraction = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ("order",)
        constraints = [
            models.UniqueConstraint(fields=["review", "order"], name="unique_review_spot_order"),
        ]
        indexes = [models.Index(fields=["note", "order"], name="note_review_spot_note_idx")]

    def __str__(self) -> str:
        return f"Spot {self.order} of note {self.note_id}"


class NoteQAReview(BaseModel):
    """One quality-assurance review of one generated note (see docs/product/note-qa-and-v42-plan.md).

    Holds scores and findings only, never the note or transcript text, so it can be kept after the
    review bundles are deleted. Deleting the note deletes its reviews.
    """

    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="qa_reviews")
    note_ref = models.CharField(max_length=24, db_index=True)
    protocol_version = models.PositiveSmallIntegerField(default=1)
    pipeline_version = models.CharField(max_length=32, blank=True, db_index=True)
    course_family = models.CharField(max_length=32, blank=True, db_index=True)
    reviewer = models.CharField(max_length=16, default="auto")
    lecture_minutes = models.FloatField(null=True, blank=True)
    est_pages = models.FloatField(null=True, blank=True)
    auto_metrics = models.JSONField(default=dict, blank=True)
    reviewer_metrics = models.JSONField(default=dict, blank=True)
    dimension_scores = models.JSONField(default=dict, blank=True)
    overall_score = models.FloatField(null=True, blank=True)
    critical_count = models.PositiveSmallIntegerField(default=0)
    verdict = models.CharField(max_length=16, blank=True)
    summary = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["pipeline_version", "created_at"], name="note_qa_review_ver_idx")]

    def __str__(self) -> str:
        return f"QA review {self.note_ref} ({self.reviewer})"


class NoteQAFinding(BaseModel):
    """One defect found by a QA review. The quote is short evidence from the transcript."""

    review = models.ForeignKey("notes.NoteQAReview", on_delete=models.CASCADE, related_name="findings")
    dimension = models.CharField(max_length=24, db_index=True)
    defect_type = models.CharField(max_length=48, db_index=True)
    severity = models.CharField(max_length=12, db_index=True)
    note_location = models.CharField(max_length=160, blank=True)
    evidence_start_s = models.FloatField(null=True, blank=True)
    evidence_end_s = models.FloatField(null=True, blank=True)
    evidence_quote = models.CharField(max_length=300, blank=True)
    detail = models.TextField(blank=True)
    suggested_cause = models.CharField(max_length=24, blank=True)

    class Meta:
        ordering = ("severity", "defect_type")

    def __str__(self) -> str:
        return f"{self.severity} {self.defect_type}"
