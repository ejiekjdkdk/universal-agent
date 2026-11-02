class Fault(Exception):
    """An error safe to return to API clients."""

    def __init__(self, code, message, status=400, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable

    def as_dict(self):
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "details": {}}
