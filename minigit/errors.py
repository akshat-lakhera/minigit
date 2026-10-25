"""minigit.errors -- owner: M1 core. See SPEC.md section 8 for the public API.

Every failure the library reports is a ``MinigitError`` subclass. Only ``cli.py``
prints; library code raises these and the CLI maps ``exit_code`` to the process
exit status (1 for any error, 2 for usage errors).
"""

from __future__ import annotations


class MinigitError(Exception):
    """Base class for every error minigit reports to the user."""

    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class UsageError(MinigitError):
    exit_code = 2


class NotARepositoryError(MinigitError):
    pass


class ObjectError(MinigitError):
    def __init__(self, message: str, *, sha: str | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.sha = sha


class LockError(MinigitError):
    pass


class RefUpdateError(MinigitError):
    def __init__(
        self,
        message: str,
        *,
        ref: str,
        expected: object,
        actual: str | None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.ref = ref
        self.expected = expected
        self.actual = actual


class RevisionError(MinigitError):
    pass


class NothingToCommit(MinigitError):
    pass


class CheckoutConflictError(MinigitError):
    def __init__(self, message: str, *, paths: list[str], hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.paths = list(paths)


class RemoteError(MinigitError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        payload: dict | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.code = code
        self.payload = payload


class AuthError(RemoteError):
    pass


class PermissionDeniedError(RemoteError):
    pass


class PushRejectedError(RemoteError):
    """A push the server (or the local pre-check) refused.

    ``results`` has the shape of the section 7.2 409 ``results`` list.
    """

    def __init__(self, message: str, *, results: list[dict], url: str, **kw) -> None:
        super().__init__(message, **kw)
        self.results = list(results)
        self.url = url
