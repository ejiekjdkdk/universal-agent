class Fault(Exception):
    """可返回给 API 调用方的错误，不包含内部异常详情。"""

    def __init__(self, code, message, status=400, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after: float = 0.0

    def as_dict(self):
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "details": {}}
