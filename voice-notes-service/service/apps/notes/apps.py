from django.apps import AppConfig


class NotesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notes"
    verbose_name = "Notes"

    def ready(self):
        # Pillow can't decode HEIC/HEIF on its own -- registering this opener
        # once at startup lets Image.open() handle it transparently, same as
        # any other format, so photos straight off an iPhone camera (which
        # defaults to HEIC) upload into the editor like any other image.
        import pillow_heif

        pillow_heif.register_heif_opener()

        from services.note_review import signals  # noqa: F401  (registers clip cleanup on delete)
