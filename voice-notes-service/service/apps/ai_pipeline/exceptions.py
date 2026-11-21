from __future__ import annotations


class PipelineError(Exception):
    def __init__(self, *, stage: str, message: str) -> None:
        self.stage = stage
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        return {"error": True, "stage": self.stage, "message": self.message}
