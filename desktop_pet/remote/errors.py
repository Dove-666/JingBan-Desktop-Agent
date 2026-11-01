"""Public remote errors never contain provider exceptions or local paths."""


class RemoteError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
