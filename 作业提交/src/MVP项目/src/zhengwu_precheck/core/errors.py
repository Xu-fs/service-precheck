"""统一业务错误，不在客户端暴露服务响应或堆栈。"""
class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        self.status, self.code, self.message, self.retryable = status, code, message, retryable
        super().__init__(message)
