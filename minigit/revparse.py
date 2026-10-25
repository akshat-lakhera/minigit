"""minigit.revparse -- owner: M1 core. See SPEC.md section 8 for the public API.

Revision syntax (SPEC section 5): ``HEAD``, ``@``, ``ORIG_HEAD``, ``MERGE_HEAD``,
a full sha, a ref name (``refs/x``, then ``refs/heads/x``, then
``refs/remotes/x``), a hex prefix of >= 4 characters; suffixes ``~N``, ``^``,
``^N`` applied left to right; and ``rev:path`` for a blob or tree.
"""

from __future__ import annotations

import re

from .errors import ObjectError, RevisionError
from .objects import parse_commit, parse_tree
from .refs import is_hex_sha
from .repo import Repository

_SUFFIX_RE = re.compile(r"(~|\^)(\d*)")
_HEXPREFIX_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def abbrev(sha: str, n: int = 7) -> str:
    return sha[:n]


def resolve_local_branch(repo: Repository, name: str) -> str | None:
    ref = "refs/heads/" + name
    return ref if repo.refs.read_raw(ref) is not None else None


def resolve_ref(repo: Repository, name: str) -> str | None:
    """Full ref name for a short name, or ``None`` when no such ref exists."""
    if not name or name.endswith("/"):
        return None
    candidates = []
    if name.startswith("refs/"):
        candidates.append(name)
    candidates.append("refs/heads/" + name)
    candidates.append("refs/remotes/" + name)
    for ref in candidates:
        if repo.refs.read_raw(ref) is not None:
            return ref
    return None


def _unknown(spec: str) -> RevisionError:
    return RevisionError("unknown revision '%s'" % spec)


def _resolve_base(repo: Repository, base: str, spec: str) -> str:
    if base in ("HEAD", "@"):
        sha = repo.refs.resolve("HEAD")
        if sha is None:
            raise _unknown(spec)
        return sha
    if base in ("ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        sha = repo.refs.read_special(base)
        if sha is None or not is_hex_sha(sha):
            raise _unknown(spec)
        return sha
    if is_hex_sha(base):
        if not repo.objects.has(base):
            raise _unknown(spec)
        return base
    ref = resolve_ref(repo, base)
    if ref is not None:
        sha = repo.refs.resolve(ref)
        if sha is None:
            raise _unknown(spec)
        return sha
    if _HEXPREFIX_RE.match(base):
        matches = repo.objects.resolve_prefix(base.lower())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RevisionError("ambiguous short id '%s' (matches %d objects)" % (base, len(matches)))
    raise _unknown(spec)


def _commit_of(repo: Repository, sha: str, label: str):
    try:
        obj_type, data = repo.objects.read(sha)
    except ObjectError:
        raise _unknown(label) from None
    if obj_type != "commit":
        raise RevisionError("'%s' is not a commit" % label)
    return parse_commit(data)


def _apply_suffixes(repo: Repository, sha: str, base: str, suffix: str, spec: str) -> str:
    pos = 0
    label = base
    while pos < len(suffix):
        m = _SUFFIX_RE.match(suffix, pos)
        if not m:
            raise _unknown(spec)
        op, num = m.group(1), m.group(2)
        pos = m.end()
        if op == "~":
            n = int(num) if num else 1
            for _ in range(n):
                c = _commit_of(repo, sha, label)
                if not c.parents:
                    raise _unknown(spec)
                sha = c.parents[0]
                label = sha
        else:
            n = int(num) if num else 1
            c = _commit_of(repo, sha, label)
            if n == 0:
                continue
            if n > len(c.parents):
                raise _unknown(spec)
            sha = c.parents[n - 1]
            label = sha
    return sha


def _resolve_path(repo: Repository, commitish: str, rev_label: str, path: str) -> str:
    obj_type, data = repo.objects.read(commitish)
    if obj_type == "commit":
        tree = parse_commit(data).tree
    elif obj_type == "tree":
        tree = commitish
    else:
        raise RevisionError("'%s' is not a commit" % rev_label)
    components = [c for c in path.split("/") if c and c != "."]
    sha = tree
    for i, comp in enumerate(components):
        try:
            entries = parse_tree(repo.objects.read_typed(sha, "tree"))
        except ObjectError:
            raise RevisionError("path '%s' does not exist in '%s'" % (path, rev_label)) from None
        for e in entries:
            if e.name == comp:
                sha = e.sha
                if e.mode != "40000" and i != len(components) - 1:
                    raise RevisionError("path '%s' does not exist in '%s'" % (path, rev_label))
                break
        else:
            raise RevisionError("path '%s' does not exist in '%s'" % (path, rev_label))
    return sha


def resolve_object(repo: Repository, spec: str) -> str:
    """Resolve ``spec`` to any object sha (blob, tree or commit), including ``rev:path``."""
    if not isinstance(spec, str) or spec == "":
        raise _unknown(spec)
    path: str | None = None
    rev = spec
    colon = spec.find(":")
    if colon >= 0:
        rev, path = spec[:colon], spec[colon + 1 :]
        if rev == "":
            raise _unknown(spec)
    m = re.search(r"[~^]", rev)
    if m:
        base, suffix = rev[: m.start()], rev[m.start() :]
    else:
        base, suffix = rev, ""
    if base == "":
        raise _unknown(spec)
    sha = _resolve_base(repo, base, rev)
    if suffix:
        sha = _apply_suffixes(repo, sha, base, suffix, rev)
    if path is not None:
        sha = _resolve_path(repo, sha, rev, path)
    return sha


def resolve_rev(repo: Repository, spec: str) -> str:
    """Resolve ``spec`` and require a commit."""
    sha = resolve_object(repo, spec)
    try:
        obj_type, _ = repo.objects.read(sha)
    except ObjectError as e:
        raise RevisionError(e.message) from None
    if obj_type != "commit":
        raise RevisionError("'%s' is not a commit" % spec)
    return sha
