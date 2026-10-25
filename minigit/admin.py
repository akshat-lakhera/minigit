"""minigit.admin -- server administration beyond add-user/create-repo (features/server-admin.md).

Every function here edits ROOT/server.json through ``auth._modify`` (lock, read, mutate, atomic write), so
concurrent admin commands lose no update, and a function that raises changes nothing. Fields this module
does not know about (in users, repos or at the top level) are never dropped.
"""
from __future__ import annotations

import time

from minigit import auth, errors

LEVELS = ("none", "read", "write")


def _no_user(name: str) -> errors.MinigitError:
    return errors.MinigitError(f"user '{name}' does not exist")


def _no_repo(name: str) -> errors.MinigitError:
    return errors.MinigitError(f"repository '{name}' does not exist")


def _user_record(data: dict, name: str) -> dict:
    record = data["users"].get(name)
    if name not in data["users"]:
        raise _no_user(name)
    if not isinstance(record, dict):
        record = data["users"][name] = {}
    return record


def _repo_record(data: dict, name: str) -> dict:
    record = data["repos"].get(name)
    if not isinstance(record, dict):
        raise _no_repo(name)
    return record


def _acl(record: dict, key: str) -> list:
    value = record.get(key)
    if not isinstance(value, list):
        value = record[key] = []
    return value


# ---------------------------------------------------------------------------
# users and tokens
# ---------------------------------------------------------------------------

def remove_user(root: str, name: str) -> list[str]:
    """Delete a user and every token; drop the name from all repo access lists. Returns the names of the
    repositories (sorted) whose lists contained it."""
    auth.validate_user_name(name)

    def mutate(data: dict) -> list[str]:
        if name not in data["users"]:
            raise _no_user(name)
        del data["users"][name]
        touched = []
        for repo in sorted(data["repos"]):
            record = data["repos"][repo]
            if not isinstance(record, dict):
                continue
            changed = False
            for key in ("readers", "writers"):
                names = record.get(key)
                if isinstance(names, list) and name in names:
                    record[key] = [n for n in names if n != name]
                    changed = True
            if changed:
                touched.append(repo)
        return touched

    return auth._modify(root, mutate)  # type: ignore[return-value]


def add_token(root: str, user: str, name: str) -> str:
    """Issue a new token called ``name`` for ``user`` and return it (it is stored only as a hash)."""
    auth.validate_user_name(user)
    auth.validate_token_name(name)
    token = auth._new_token()
    digest = auth.hash_token(token)
    now = int(time.time())

    def mutate(data: dict) -> None:
        record = _user_record(data, user)
        taken = {n for n, _, _ in auth.user_tokens(record)}
        if name in taken:
            raise errors.MinigitError(f"token '{name}' of user '{user}' already exists",
                                      hint="revoke it first with 'minigit server revoke-token'")
        if name == auth.DEFAULT_TOKEN:
            record["token_sha256"] = digest
            record["token_created"] = now
            return
        named = record.get("tokens")
        if not isinstance(named, dict):
            named = record["tokens"] = {}
        named[name] = {"sha256": digest, "created": now}

    auth._modify(root, mutate)
    return token


def list_tokens(root: str, user: str) -> list[tuple[str, int | None]]:
    """``(name, created)`` of the usable tokens of ``user``: ``default`` first, then by name."""
    auth.validate_user_name(user)
    cfg = auth.ServerConfig(root)
    if user not in cfg.users:
        raise _no_user(user)
    return [(n, created) for n, _, created in auth.user_tokens(cfg.users[user])]


def revoke_token(root: str, user: str, name: str | None = None, *, everything: bool = False) -> int:
    """Revoke one token (``name``) or all of them. Returns how many usable tokens were revoked."""
    auth.validate_user_name(user)
    if name is not None:
        auth.validate_token_name(name)

    def mutate(data: dict) -> int:
        record = _user_record(data, user)
        if everything:
            count = len(auth.user_tokens(record))
            for key in ("token_sha256", "token_created", "tokens"):
                record.pop(key, None)
            return count
        if name == auth.DEFAULT_TOKEN:
            if "token_sha256" not in record:
                raise errors.MinigitError(f"user '{user}' has no token named '{name}'")
            record.pop("token_sha256")
            record.pop("token_created", None)
            return 1
        named = record.get("tokens")
        if not isinstance(named, dict) or name not in named:
            raise errors.MinigitError(f"user '{user}' has no token named '{name}'")
        del named[name]
        if not named:
            del record["tokens"]
        return 1

    return auth._modify(root, mutate)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# repository permissions and options
# ---------------------------------------------------------------------------

def _effective(record: dict, name: str) -> tuple[str, str | None]:
    """The access ``name`` ('*' stands for the lists' wildcard entry) really has to a repo, and why
    it has more than its own entry gives it (``'*'``, ``public`` or None)."""
    readers = record.get("readers") if isinstance(record.get("readers"), list) else []
    writers = record.get("writers") if isinstance(record.get("writers"), list) else []
    if name == "*":
        return ("write" if "*" in writers else "read" if "*" in readers else "none"), None
    if name in writers or "*" in writers:
        return "write", "'*'" if name not in writers else None
    if name in readers or "*" in readers:
        return "read", "'*'" if name not in readers else None
    if record.get("public"):
        return "read", "public"
    return "none", None


def _line(repo: str, name: str, record: dict, requested: str | None) -> tuple[str, str | None]:
    """``("repo: name = level", warning)``; the warning is set when ``requested`` is less than the result."""
    level, via = _effective(record, name)
    warning = None
    if requested is not None and via is not None and LEVELS.index(level) > LEVELS.index(requested):
        if via == "public":
            warning = (f"warning: {name} still has read access to '{repo}' "
                       f"because the repository is public")
        else:
            warning = f"warning: {name} still has {level} access to '{repo}' through '*'"
    return f"{repo}: {name} = {level}", warning


def parse_names(value: str) -> list[str]:
    """Comma separated user names or '*', validated and de-duplicated ([] when the text names nobody)."""
    return auth._acl_list(value)


def get_perm(root: str, repo: str, names: list[str]) -> list[str]:
    """The effective access of each of ``names``; changes nothing."""
    auth.validate_repo_name(repo)
    cfg = auth.ServerConfig(root)
    record = cfg.repos.get(repo)
    if not isinstance(record, dict):
        raise _no_repo(repo)
    for n in names:
        if n != "*" and n not in cfg.users:
            raise _no_user(n)
    return [_line(repo, n, record, None)[0] for n in names]


def set_perm(root: str, repo: str, names: list[str], level: str) -> list[tuple[str, str | None]]:
    """Give each of ``names`` exactly ``level`` ('none', 'read' or 'write') on ``repo``: the name is
    removed from the list it does not belong in and added to the one it does. Returns the
    ``(output line, warning)`` pairs. ``none`` also works for names that are not (or no longer) users."""
    auth.validate_repo_name(repo)

    def mutate(data: dict) -> list[tuple[str, str | None]]:
        record = _repo_record(data, repo)
        if level != "none":
            for n in names:
                if n != "*" and n not in data["users"]:
                    raise _no_user(n)
        readers, writers = _acl(record, "readers"), _acl(record, "writers")
        for n in names:
            if level != "read" and n in readers:
                readers[:] = [x for x in readers if x != n]
            if level != "write" and n in writers:
                writers[:] = [x for x in writers if x != n]
            if level == "read" and n not in readers:
                readers.append(n)
            if level == "write" and n not in writers:
                writers.append(n)
        return [_line(repo, n, record, level) for n in names]

    return auth._modify(root, mutate)  # type: ignore[return-value]


def set_repo(root: str, repo: str, *, public: bool | None = None,
             allow_force: bool | None = None) -> tuple[bool, bool]:
    """Change the ``public`` / ``allow_force`` flags (None leaves one alone); returns both final values."""
    auth.validate_repo_name(repo)

    def mutate(data: dict) -> tuple[bool, bool]:
        record = _repo_record(data, repo)
        if public is not None:
            record["public"] = public
        if allow_force is not None:
            record["allow_force"] = allow_force
        return bool(record.get("public")), bool(record.get("allow_force"))

    return auth._modify(root, mutate)  # type: ignore[return-value]
