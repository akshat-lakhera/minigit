"""minigit.revparse -- owner: M1 core. See SPEC.md section 8 for the public API.

Revision syntax (SPEC section 5): ``HEAD``, ``@``, ``ORIG_HEAD``, ``MERGE_HEAD``,
a full sha, a ref name (``refs/x``, then ``refs/tags/x``, then ``refs/heads/x``,
then ``refs/remotes/x``), a hex prefix of >= 4 characters; suffixes ``~N``,
``^``, ``^N`` and the peel suffixes ``^{}``, ``^{commit}``, ``^{tree}``, ``^{blob}``,
``^{tag}``, ``^{object}`` applied left to right; and ``rev:path`` for a blob or tree.
An annotated tag is peeled to the commit it names wherever a commit is required.
"""

from __future__ import annotations

import re

from .errors import ObjectError, RevisionError
from .objects import parse_commit, parse_tree, peel
from .refs import is_hex_sha
from .repo import Repository

_SUFFIX_RE = re.compile(r"(~|\^)(\d*)")
_PEEL_RE = re.compile(r"\^\{([a-z]*)\}")
_PEEL_TYPES = ("", "commit", "tree", "blob", "tag", "object")
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
    candidates.append("refs/tags/" + name)  # git's order: tags shadow branches of the same name
    candidates.append("refs/heads/" + name)
    candidates.append("refs/remotes/" + name)
    for ref in candidates:
        if repo.refs.read_raw(ref) is not None:
            return ref
    return None


def _unknown(spec: str, hint: str | None = None) -> RevisionError:
    return RevisionError("unknown revision '%s'" % spec, hint=hint)


def _resolve_base(repo: Repository, base: str, spec: str) -> str:
    if base in ("HEAD", "@"):
        sha = repo.refs.resolve("HEAD")
        if sha is None:
            branch = repo.refs.current_branch()
            raise _unknown(spec, None if branch is None else
                           "the current branch '%s' has no commits yet" % branch)
        return sha
    if base in ("ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
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


def peel_to_commit(repo: Repository, sha: str, label: str) -> str:
    """``sha`` itself, or the commit an annotated tag (chain) names; ``RevisionError`` otherwise."""
    try:
        obj_type, peeled = peel(repo.objects, sha)
    except ObjectError as e:
        raise RevisionError(e.message) from None
    if obj_type != "commit":
        raise RevisionError("'%s' is not a commit" % label)
    return peeled


def _commit_of(repo: Repository, sha: str, label: str):
    try:
        obj_type, data = repo.objects.read(sha)
    except ObjectError:
        raise _unknown(label) from None
    if obj_type == "tag":
        sha = peel_to_commit(repo, sha, label)
        obj_type, data = repo.objects.read(sha)
    if obj_type != "commit":
        raise RevisionError("'%s' is not a commit" % label)
    return parse_commit(data)


def _apply_suffixes(repo: Repository, sha: str, base: str, suffix: str, spec: str) -> str:
    pos = 0
    label = base
    while pos < len(suffix):
        here = base + suffix[:pos]   # the revision named so far, for the hints
        peel_m = _PEEL_RE.match(suffix, pos)
        if peel_m is not None or suffix.startswith("^{", pos):
            # git's peel syntax: X^{} (tags followed), X^{commit}, X^{tree}, X^{blob}, X^{tag}, X^{object}
            want = peel_m.group(1) if peel_m is not None else None
            if want not in _PEEL_TYPES:
                raise _unknown(spec, "minigit understands the peel suffixes ^{}, ^{commit}, ^{tree}, "
                                     "^{blob}, ^{tag} and ^{object}")
            pos = peel_m.end()
            sha = _peel_to(repo, sha, want, spec)
            label = here + peel_m.group(0)
            continue
        m = _SUFFIX_RE.match(suffix, pos)
        if not m:
            raise _unknown(spec)
        op, num = m.group(1), m.group(2)
        pos = m.end()
        # as in git, "v1~0" and "v1^0" name the tagged commit
        sha = peel_to_commit(repo, sha, label)
        if op == "~":
            n = int(num) if num else 1
            for i in range(n):
                c = _commit_of(repo, sha, label)
                if not c.parents:
                    at = here + ("~%d" % i if i else "")
                    raise _unknown(spec, "%s (%s) is a root commit: the history has no commit before it"
                                   % (at, sha[:7]))
                sha = c.parents[0]
                label = sha
        else:
            n = int(num) if num else 1
            c = _commit_of(repo, sha, label)
            if n == 0:
                continue
            if n > len(c.parents):
                count = len(c.parents)
                raise _unknown(spec, "%s (%s) has %s" % (
                    here, sha[:7], "no parent" if not count else "%d parent%s" % (count, "s" * (count > 1))))
            sha = c.parents[n - 1]
            label = sha
    return sha


def _peel_to(repo: Repository, sha: str, want: str, spec: str) -> str:
    """``X^{want}``: ``""`` follows tags to the first non-tag object, ``object`` is ``X`` itself,
    ``tag`` requires a tag object; ``commit``/``tree``/``blob`` follow tags (and a commit to its
    tree for ``tree``) and require that type."""
    if want == "object":
        return sha
    try:
        if want == "tag":
            obj_type = repo.objects.read(sha)[0]
            peeled = sha
        else:
            obj_type, peeled = peel(repo.objects, sha)
            if want == "tree" and obj_type == "commit":
                obj_type, peeled = "tree", parse_commit(repo.objects.read_typed(peeled, "commit")).tree
    except ObjectError as e:
        raise RevisionError(e.message) from None
    if want and obj_type != want:
        # git peels a commit on to its tree before giving up, and names that type
        got = "tree" if obj_type == "commit" and want != "commit" else obj_type
        raise RevisionError("%s: expected %s type, but the object dereferences to %s type"
                            % (spec, want, got))
    return peeled


def _resolve_path(repo: Repository, commitish: str, rev_label: str, path: str) -> str:
    obj_type, data = repo.objects.read(commitish)
    if obj_type == "tag":
        commitish = peel_to_commit(repo, commitish, rev_label)
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
    """Resolve ``spec`` to any object sha (blob, tree, commit or tag), including ``rev:path``.

    An annotated tag resolves to the tag object itself (as ``git rev-parse v1`` does);
    ``resolve_rev`` peels it.
    """
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
    """Resolve ``spec`` and require a commit (an annotated tag is peeled to its commit)."""
    return peel_to_commit(repo, resolve_object(repo, spec), spec)
