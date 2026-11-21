from apps.uploads.models import LectureFile


class UploadSelector:
    @staticmethod
    def list_files(*, user):
        return LectureFile.objects.select_related("lecture").filter(lecture__user=user)

    @staticmethod
    def list_files_for_lecture(*, user, lecture_id):
        return (
            LectureFile.objects.select_related("lecture")
            .filter(lecture_id=lecture_id, lecture__user=user)
            .order_by("created_at")
        )
