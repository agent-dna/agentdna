from typing import (
    Callable, TypeAlias, Any,
    Awaitable
)

CbacFn: TypeAlias = Callable[
    [
        str, 
        str,
        dict[str, Any],
        str | None,
        str | None,
        str,
        str | None,
    ], 
    Awaitable[tuple[str, int, str]]
]

class CBACVerificationError(Exception):
    """Raised when CBAC verification fails."""
    pass