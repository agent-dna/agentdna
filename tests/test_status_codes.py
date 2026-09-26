import json

from agentdna.error import (
    ADMIN_WHITELIST_CHECK_FAILED,
    ADMIN_WHITELIST_CHECK_SERVER_ERROR,
    COCA_VERIFICATION_FAILED_BOUNDARY,
    COCA_VERIFICATION_FAILED_HEAVY,
    COCA_VERIFICATION_FAILED_LIGHT,
    COCA_VERIFICATION_FAILED_UNKNOWN,
    MIDDLEWARE_EXECUTION_FAILED,
    RESULT_OK,
    TOOL_EXECUTION_FAILED,
    StatusCode,
    describe_status,
)


def test_status_code_values_match_existing_constants():
    assert StatusCode.RESULT_OK == RESULT_OK
    assert StatusCode.ADMIN_WHITELIST_CHECK_FAILED == ADMIN_WHITELIST_CHECK_FAILED
    assert StatusCode.ADMIN_WHITELIST_CHECK_SERVER_ERROR == ADMIN_WHITELIST_CHECK_SERVER_ERROR
    assert StatusCode.COCA_VERIFICATION_FAILED_LIGHT == COCA_VERIFICATION_FAILED_LIGHT
    assert StatusCode.COCA_VERIFICATION_FAILED_HEAVY == COCA_VERIFICATION_FAILED_HEAVY
    assert StatusCode.COCA_VERIFICATION_FAILED_BOUNDARY == COCA_VERIFICATION_FAILED_BOUNDARY
    assert StatusCode.COCA_VERIFICATION_FAILED_UNKNOWN == COCA_VERIFICATION_FAILED_UNKNOWN
    assert StatusCode.TOOL_EXECUTION_FAILED == TOOL_EXECUTION_FAILED
    assert StatusCode.MIDDLEWARE_EXECUTION_FAILED == MIDDLEWARE_EXECUTION_FAILED


def test_status_codes_remain_json_serializable_as_integers():
    assert json.dumps({"status_code": StatusCode.COCA_VERIFICATION_FAILED_HEAVY}) == (
        '{"status_code": 2002}'
    )


def test_describe_known_status():
    assert describe_status(COCA_VERIFICATION_FAILED_HEAVY) == (
        "2002 (COCA_VERIFICATION_FAILED_HEAVY): Envelope verification failed under heavy mode"
    )


def test_describe_unknown_status():
    assert describe_status(9999) == ("9999 (UNKNOWN_STATUS): Unknown AgentDNA status code")
