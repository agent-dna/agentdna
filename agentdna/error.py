from enum import IntEnum


class StatusCode(IntEnum):
    """Named AgentDNA status codes with their human-readable descriptions."""

    RESULT_OK = 1000
    ADMIN_WHITELIST_CHECK_FAILED = 1001
    ADMIN_WHITELIST_CHECK_SERVER_ERROR = 1002

    COCA_VERIFICATION_FAILED_LIGHT = 2001
    COCA_VERIFICATION_FAILED_HEAVY = 2002
    COCA_VERIFICATION_FAILED_BOUNDARY = 2003
    COCA_VERIFICATION_FAILED_UNKNOWN = 2999

    TOOL_EXECUTION_FAILED = 4001
    MIDDLEWARE_EXECUTION_FAILED = 4002

    @property
    def description(self) -> str:
        """Return a concise explanation of this status code."""
        return _STATUS_DESCRIPTIONS[self]


_STATUS_DESCRIPTIONS = {
    StatusCode.RESULT_OK: "No issues found",
    StatusCode.ADMIN_WHITELIST_CHECK_FAILED: "Agent is not whitelisted",
    StatusCode.ADMIN_WHITELIST_CHECK_SERVER_ERROR: (
        "Agent whitelist verification could not be completed"
    ),
    StatusCode.COCA_VERIFICATION_FAILED_LIGHT: ("Envelope verification failed under light mode"),
    StatusCode.COCA_VERIFICATION_FAILED_HEAVY: ("Envelope verification failed under heavy mode"),
    StatusCode.COCA_VERIFICATION_FAILED_BOUNDARY: (
        "Envelope verification failed under boundary mode"
    ),
    StatusCode.COCA_VERIFICATION_FAILED_UNKNOWN: ("CoCA verification failed for an unknown reason"),
    StatusCode.TOOL_EXECUTION_FAILED: "MCP tool execution failed",
    StatusCode.MIDDLEWARE_EXECUTION_FAILED: "MCP middleware execution failed",
}


def describe_status(code: int | StatusCode) -> str:
    """Format an AgentDNA status code for logs, errors, and user interfaces."""
    try:
        status = StatusCode(code)
    except (TypeError, ValueError):
        return f"{code} (UNKNOWN_STATUS): Unknown AgentDNA status code"

    return f"{status.value} ({status.name}): {status.description}"


# Preserve the existing integer constants as a backwards-compatible API.
RESULT_OK = StatusCode.RESULT_OK.value
ADMIN_WHITELIST_CHECK_FAILED = StatusCode.ADMIN_WHITELIST_CHECK_FAILED.value
ADMIN_WHITELIST_CHECK_SERVER_ERROR = StatusCode.ADMIN_WHITELIST_CHECK_SERVER_ERROR.value

COCA_VERIFICATION_FAILED_LIGHT = StatusCode.COCA_VERIFICATION_FAILED_LIGHT.value
COCA_VERIFICATION_FAILED_HEAVY = StatusCode.COCA_VERIFICATION_FAILED_HEAVY.value
COCA_VERIFICATION_FAILED_BOUNDARY = StatusCode.COCA_VERIFICATION_FAILED_BOUNDARY.value
COCA_VERIFICATION_FAILED_UNKNOWN = StatusCode.COCA_VERIFICATION_FAILED_UNKNOWN.value

TOOL_EXECUTION_FAILED = StatusCode.TOOL_EXECUTION_FAILED.value
MIDDLEWARE_EXECUTION_FAILED = StatusCode.MIDDLEWARE_EXECUTION_FAILED.value
