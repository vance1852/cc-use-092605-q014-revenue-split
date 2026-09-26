"""收益归集服务向 API 和 CLI 暴露的稳定错误。"""


class RevenueError(RuntimeError):
    code = "revenue_error"
    status = 400


class NotFound(RevenueError):
    code = "not_found"
    status = 404


class Conflict(RevenueError):
    code = "conflict"
    status = 409


class Forbidden(RevenueError):
    code = "forbidden"
    status = 403


class InvalidState(RevenueError):
    code = "invalid_state"
    status = 409


class ValidationFailed(RevenueError):
    code = "validation_failed"
    status = 422
