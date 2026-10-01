"""Safe application errors, independent of model SDKs and entry points."""


class FrontierError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}
