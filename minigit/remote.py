"""minigit.remote -- owner: M6 client.

Remote configuration and the collaboration flows: clone, fetch, pull, push and
login (SPEC sections 7.1 and 7.5).  Library code: never prints; returns message
strings and raises MinigitError subclasses.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import graph
from . import index as index_mod
from . import merge as merge_mod
from . import pack as pack_mod
from . import revparse
from . import trees
from . import worktree
from .errors import MinigitError, PushRejectedError, RemoteError, UsageError
from .refs import ANY, _check_windows_ref_name, check_ref_name
from .repo import Repository
from .transport import (HttpRemote, RemoteRefs, check_url, redact_url, save_credentials, split_url,
                        url_with_user)

if TYPE_CHECKING:  # pragma: no cover
    from .merge import MergeOutcome

HEADS = "refs/heads/"
TAGS = "refs/tags/"
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass
class RefChange:
    ref: str            # the remote-tracking ref ("refs/remotes/origin/main") or a tag ("refs/tags/v1")
    old: str | None
    new: str | None
    kind: str           # "new" | "updated" | "forced" | "deleted" | "up-to-date" | "rejected" (tags only)


def _abbrev(sha: str | None) -> str:
    return (sha or "0" * 40)[:7]


# ------------------------------------------------------------ remotes ----

def _validate_remote_name(name: str) -> None:
    if not _REMOTE_NAME_RE.match(name or "") or ".." in name or name.endswith(".lock"):
        raise UsageError(f"invalid remote name '{name}'")


def remote_add(repo: Repository, name: str, url: str) -> None:
    _validate_remote_name(name)
    cfg = repo.config
    if cfg.get(f"remote.{name}.url") is not None:
        raise MinigitError(f"remote '{name}' already exists")
    clean, creds = split_url(url)
    check_url(clean)
    clean = clean.rstrip("/")
    if creds is not None:
        save_credentials(clean, creds[0], creds[1])
    cfg.set(f"remote.{name}.url", url_with_user(clean, creds[0] if creds else None))


def remote_remove(repo: Repository, name: str) -> None:
    remote_url(repo, name)  # raises if missing
    cfg = repo.config
    cfg.remove_section("remote", name)
    for ref in sorted(repo.refs.list(f"refs/remotes/{name}/"), key=len, reverse=True):
        repo.refs.update(ref, None)
    for branch in cfg.subsections("branch"):
        if repo.config.get(f"branch.{branch}.remote") == name:
            repo.config.unset(f"branch.{branch}.remote")
            repo.config.unset(f"branch.{branch}.merge")


def remote_list(repo: Repository) -> dict[str, str]:
    """Remote name -> URL for display (``remote -v``): a token in a hand-edited URL shows as ``***``."""
    cfg = repo.config
    out: dict[str, str] = {}
    for name in sorted(cfg.subsections("remote")):
        url = cfg.get(f"remote.{name}.url")
        if url:
            out[name] = redact_url(url)
    return out


def remote_url(repo: Repository, name: str) -> str:
    url = repo.config.get(f"remote.{name}.url")
    if not url:
        raise MinigitError(f"no remote '{name}' configured",
                           hint=f"add it with: minigit remote add {name} <url>")
    return url


def _head_branch(repo: Repository) -> str | None:
    """Branch HEAD points at (even when unborn), or None when detached."""
    target = repo.refs.head_target()
    if target and target.startswith(HEADS):
        return target[len(HEADS):]
    return None


def _default_remote(repo: Repository) -> str:
    branch = _head_branch(repo)
    if branch:
        configured = repo.config.get(f"branch.{branch}.remote")
        if configured:
            return configured
    return "origin"


# -------------------------------------------------------------- fetch ----

def _local_tips(repo: Repository) -> list[str]:
    tips = set(repo.refs.list("refs/").values())
    head = repo.head_commit()
    if head:
        tips.add(head)
    return sorted(tips)


def _is_connected(repo: Repository, sha: str, complete: set[str]) -> bool:
    if sha in complete:
        return True
    if not repo.objects.has(sha):
        return False
    try:
        pack_mod.check_connectivity(repo.objects, sha, complete)
    except MinigitError:
        return False
    return True


def _fetch_with(repo: Repository, remote: str, rem: HttpRemote, info: RemoteRefs, *,
                all_tags: bool = False) -> list[RefChange]:
    store = repo.objects
    heads = {ref: sha for ref, sha in info.refs.items() if ref.startswith(HEADS)}
    tags = {ref: sha for ref, sha in info.refs.items() if ref.startswith(TAGS)}
    haves = _local_tips(repo)
    complete = pack_mod.complete_commits(store, haves)
    # A tip is wanted unless it is present *and* fully connected locally.
    wants = {sha for sha in heads.values() if not _is_connected(repo, sha, complete)}
    if all_tags:
        wants |= {sha for sha in tags.values() if not _is_connected(repo, sha, complete)}
    wants = sorted(wants)
    if wants:
        data = rem.fetch_pack(wants, haves)
        pack_mod.unpack(store, data)
        complete = pack_mod.complete_commits(store, haves)
        for sha in wants:
            pack_mod.check_connectivity(store, sha, complete)
    if not all_tags and tags:
        # Auto-follow (as git does): a tag comes along when the commit it names is one we have,
        # on a local ref or on a branch just fetched. Other tags need 'fetch --tags'.
        known = pack_mod.complete_commits(store, haves + list(heads.values()))
        tags = {ref: sha for ref, sha in tags.items() if info.commit_of(ref) in known}
        tag_wants = sorted({sha for sha in tags.values() if not _is_connected(repo, sha, known)})
        if tag_wants:
            data = rem.fetch_pack(tag_wants, haves + list(heads.values()))
            pack_mod.unpack(store, data)
            for sha in tag_wants:
                pack_mod.check_connectivity(store, sha, known)

    changes: list[RefChange] = []
    for ref, new in sorted(tags.items()):
        try:
            check_ref_name(ref)
            _check_windows_ref_name(ref)
        except MinigitError as e:
            print(f"warning: ignoring tag '{ref[len(TAGS):]}' on '{remote}': {e.message}", file=sys.stderr)
            continue
        old = repo.refs.resolve(ref)
        if old == new:
            changes.append(RefChange(ref, old, new, "up-to-date"))
        elif old is None:
            repo.refs.update(ref, new, expected=None)
            changes.append(RefChange(ref, None, new, "new"))
        else:  # as in git, fetch never replaces a local tag that differs
            changes.append(RefChange(ref, old, new, "rejected"))

    prefix = f"refs/remotes/{remote}/"
    for ref in sorted(heads):
        try:
            check_ref_name(prefix + ref[len(HEADS):])
            _check_windows_ref_name(prefix + ref[len(HEADS):])
        except MinigitError as e:
            # git refuses such names too; skipping keeps the rest of the fetch usable
            print(f"warning: ignoring branch '{ref[len(HEADS):]}' on '{remote}': {e.message}", file=sys.stderr)
            del heads[ref]
    existing = repo.refs.list(prefix)
    # deletions first, so a branch "a" can be replaced by "a/b" (and vice versa)
    for tref, old in sorted(existing.items(), key=lambda kv: len(kv[0]), reverse=True):
        if HEADS + tref[len(prefix):] not in heads:
            repo.refs.update(tref, None, expected=ANY)
            changes.append(RefChange(tref, old, None, "deleted"))
    for ref, new in sorted(heads.items()):
        tref = prefix + ref[len(HEADS):]
        old = existing.get(tref)
        if old == new:
            changes.append(RefChange(tref, old, new, "up-to-date"))
            continue
        if old is None:
            kind = "new"
        else:
            try:
                kind = "updated" if graph.is_ancestor(store, old, new) else "forced"
            except MinigitError:
                kind = "forced"
        repo.refs.update(tref, new, expected=ANY)
        changes.append(RefChange(tref, old, new, kind))
    # branches first (tracking refs, deletions last), then tags
    order = {"deleted": 1}
    changes.sort(key=lambda c: (c.ref.startswith(TAGS), order.get(c.kind, 0), c.ref))
    return changes


def fetch(repo: Repository, remote: str | None = "origin", *, all_tags: bool = False) -> list[RefChange]:
    remote = remote or _default_remote(repo)
    url = remote_url(repo, remote)
    rem = HttpRemote(url)
    return _fetch_with(repo, remote, rem, rem.info_refs(), all_tags=all_tags)


def format_fetch(url: str, changes: list[RefChange]) -> str:
    lines: list[str] = []
    for c in changes:
        if c.kind == "up-to-date":
            continue
        if c.ref.startswith(TAGS):
            name = c.ref[len(TAGS):]
            if c.kind == "new":
                lines.append(f" * {'[new tag]':<17} {name} -> {name}")
            elif c.kind == "rejected":
                lines.append(f" ! {'[rejected]':<17} {name} -> {name} (would clobber existing tag)")
            continue
        remote, _, branch = c.ref[len("refs/remotes/"):].partition("/")
        dst = f"{remote}/{branch}"
        if c.kind == "new":
            lines.append(f" * {'[new branch]':<17} {branch} -> {dst}")
        elif c.kind == "deleted":
            lines.append(f" - {'[deleted]':<17} {branch} -> {dst}")
        elif c.kind == "forced":
            lines.append(f" + {_abbrev(c.old) + '...' + _abbrev(c.new):<17} {branch} -> {dst} (forced update)")
        else:
            lines.append(f"   {_abbrev(c.old) + '..' + _abbrev(c.new):<17} {branch} -> {dst}")
    if not lines:
        return ""
    return "\n".join([f"From {redact_url(url)}"] + lines)


# -------------------------------------------------------------- clone ----

def _guess_dir(clean_url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(clean_url).path).rstrip("/")
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not name or name in (".", ".."):
        raise UsageError(f"cannot guess a directory name from '{clean_url}'; please specify one")
    return name


def _force_remove(func, path, _exc_info) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
    except OSError:
        pass


def _cleanup(target: str, created: bool) -> None:
    if created:
        shutil.rmtree(target, onerror=_force_remove)
        return
    try:
        children = os.listdir(target)
    except OSError:
        return
    for child in children:
        p = os.path.join(target, child)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, onerror=_force_remove)
        else:
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                os.unlink(p)
            except OSError:
                pass


def _choose_branch(info: RemoteRefs) -> str | None:
    heads = sorted(r for r in info.refs if r.startswith(HEADS))
    if not heads:
        return None
    if info.head and info.head in info.refs and info.head.startswith(HEADS):
        return info.head[len(HEADS):]
    if HEADS + "main" in info.refs:
        return "main"
    return heads[0][len(HEADS):]


def clone(url: str, directory: str | None, cwd: str) -> tuple[Repository, list[str]]:
    clean, creds = split_url(url)
    check_url(clean)
    clean = clean.rstrip("/")
    if not directory:
        directory = _guess_dir(clean)
    target = os.path.abspath(os.path.join(cwd, directory))
    created = not os.path.exists(target)
    if not created and (not os.path.isdir(target) or os.listdir(target)):
        raise MinigitError(f"destination path '{directory}' already exists and is not an empty directory")

    rem = HttpRemote(url)
    info = rem.info_refs()           # fails fast (auth, 404, ...) before touching the disk
    if creds is not None:
        save_credentials(clean, creds[0], creds[1])

    messages = [f"Cloning into '{directory}'..."]
    branch = _choose_branch(info)
    if branch is None:
        head = info.head if info.head and info.head.startswith(HEADS) else HEADS + "main"
        initial = head[len(HEADS):]
    else:
        initial = branch
    try:
        os.makedirs(target, exist_ok=True)
        repo, _ = Repository.init(target, initial_branch=initial)
        repo.config.set("remote.origin.url", url_with_user(clean, creds[0] if creds else None))
        _fetch_with(repo, "origin", rem, info)
        if branch is None:
            repo.refs.set_symbolic_head(HEADS + initial)
            repo.config.set(f"branch.{initial}.remote", "origin")
            repo.config.set(f"branch.{initial}.merge", HEADS + initial)
            messages.append("warning: You appear to have cloned an empty repository.")
        else:
            sha = info.refs[HEADS + branch]
            repo.refs.update(HEADS + branch, sha, expected=None)
            repo.config.set(f"branch.{branch}.remote", "origin")
            repo.config.set(f"branch.{branch}.merge", HEADS + branch)
            repo.refs.set_symbolic_head(HEADS + branch)
            target_flat = trees.commit_flat(repo.objects, sha)
            with index_mod.locked_index(repo) as idx:
                worktree.checkout_tree(repo, idx, {}, target_flat, operation="clone")
    except BaseException:
        _cleanup(target, created)
        raise
    return repo, messages


# --------------------------------------------------------------- pull ----

def pull(repo: Repository, remote: str | None = None, branch: str | None = None, *,
         allow_unrelated: bool = False, rebase: bool = False) -> tuple[list[RefChange], "MergeOutcome"]:
    """``fetch`` then merge the tracking branch into HEAD; with ``rebase`` the current branch is
    rebased onto it instead (``rebase.start``), so the outcome's ``kind`` is ``"rebased"``, or
    ``"conflict"`` when the rebase stopped (``rebase --continue`` / ``--abort`` finish it)."""
    repo.require_worktree()
    if rebase:
        from . import rebase as rebase_mod

        # Open operation (HEAD is detached during a stopped rebase), unborn HEAD, dirty tree: fail
        # with rebase's own messages before anything is resolved or fetched.
        rebase_mod.check_preconditions(repo)
    branch_arg = branch
    configured = False
    remote = remote or _default_remote(repo)
    url = remote_url(repo, remote)
    current = _head_branch(repo)
    if branch is None:
        if current is None:
            raise MinigitError("you are not on a branch; say which branch to merge: "
                               f"minigit pull {remote} <branch>")
        merge_ref = repo.config.get(f"branch.{current}.merge")
        if merge_ref and repo.config.get(f"branch.{current}.remote", remote) == remote:
            branch = merge_ref[len(HEADS):] if merge_ref.startswith(HEADS) else merge_ref
            configured = True
        else:
            branch = current
    elif branch.startswith(HEADS):
        branch = branch[len(HEADS):]
    changes = fetch(repo, remote)
    try:
        tracking = f"refs/remotes/{remote}/{branch}"
        if repo.refs.resolve(tracking) is None:
            if branch_arg is not None or configured:
                raise MinigitError(f"couldn't find remote ref '{branch}' on '{remote}'",
                                   hint="check the branch name with: minigit fetch")
            raise MinigitError(f"no upstream '{remote}/{branch}' (push with -u first)",
                               hint=f"publish it with 'minigit push -u {remote} {branch}', or say what to merge: "
                                    f"'minigit pull {remote} <branch>'")
        if rebase:
            return changes, _pull_rebase(repo, tracking, f"{remote}/{branch}")
        shown_url = split_url(url)[0]  # the user name of the URL does not belong in history
        outcome = merge_mod.merge(repo, tracking, label=f"{remote}/{branch}",
                                  message=f"Merge branch '{branch}' of {shown_url}",
                                  allow_unrelated=allow_unrelated)
    except MinigitError as err:
        # The fetch already moved the tracking refs: report it (as git does) although the merge failed.
        err.fetch_changes = changes
        raise
    return changes, outcome


def _pull_rebase(repo: Repository, tracking: str, label: str) -> "MergeOutcome":
    """``pull --rebase`` after the fetch: rebase HEAD's branch onto ``tracking`` (``origin/main``)."""
    from . import rebase as rebase_mod

    head = repo.head_commit()
    target = repo.refs.resolve(tracking)
    if target == head or graph.is_ancestor(repo.objects, target, head):
        return merge_mod.MergeOutcome("up-to-date", head, head, [], ["Already up to date."])
    res = rebase_mod.start(repo, label)
    conflicts: list[str] = []
    if res.stopped:
        kind = "conflict"
        conflicts = sorted(index_mod.read_index(repo).unmerged())
    elif any(line.startswith("Fast-forwarded ") for line in res.lines):
        kind = "fast-forward"
    else:
        kind = "rebased"
    return merge_mod.MergeOutcome(kind, repo.head_commit(), head, conflicts, res.lines)


# --------------------------------------------------------------- push ----

_REASON_TEXT = {
    "non-fast-forward": "non-fast-forward",
    "fetch-first": "fetch first",
    "stale": "fetch first",  # the ref moved between our info/refs and the push (a lost race)
    "force-disabled": "forced update disabled",
    "delete-default-branch": "deletion of the default branch prohibited",
    "ref-clash": "ref name clash",
    "invalid-ref-name": "invalid ref name",
    "tag-exists": "already exists",
}

_REJECT_MESSAGES = {
    "non-fast-forward": "the remote branch has commits you do not have; run 'minigit pull' first, then push again",
    "fetch-first": "the remote branch has commits you do not have; run 'minigit pull' first, then push again",
    "tag-exists": "tag '{name}' already exists on the remote (use --force to replace it)",
}


def _local_tag(repo: Repository, name: str) -> str | None:
    """``refs/tags/<name>`` when that tag exists locally."""
    ref = TAGS + name
    return ref if repo.refs.read_raw(ref) is not None else None


def _resolve_src(repo: Repository, src: str) -> tuple[str, str | None, bool]:
    """Return (sha, local branch name or None, is_tag) for a push source.

    A tag source yields the ref's value (the tag object for an annotated tag) and ``is_tag``.
    """
    if src in ("HEAD", "@"):
        sha = repo.head_commit()
        if sha is None:
            raise MinigitError("src refspec 'HEAD' does not match any commit (make a commit first)")
        return sha, _head_branch(repo), False
    if src.startswith(TAGS):
        sha = repo.refs.resolve(src)
        if sha is None:
            raise MinigitError(f"src refspec '{src}' does not match any tag")
        return sha, None, True
    name = src[len(HEADS):] if src.startswith(HEADS) else src
    local_ref = revparse.resolve_local_branch(repo, name)
    tag_ref = None if src.startswith(HEADS) else _local_tag(repo, name)
    if local_ref is not None and tag_ref is not None:
        raise MinigitError(f"src refspec '{src}' matches more than one (a branch and a tag)",
                           hint=f"say which one: refs/heads/{name} or refs/tags/{name}")
    if local_ref is not None:
        sha = repo.refs.resolve(local_ref)
        if sha:
            return sha, local_ref[len(HEADS):], False
    if tag_ref is not None:
        sha = repo.refs.resolve(tag_ref)
        if sha:
            return sha, None, True
    if _head_branch(repo) == name and repo.head_commit() is None:
        raise MinigitError(f"src refspec '{src}' does not match any commit (make a commit first)")
    try:
        return revparse.resolve_rev(repo, src), None, False
    except MinigitError:
        raise MinigitError(f"src refspec '{src}' does not match any commit") from None


def _dst_ref(dst: str, delete: bool = False, *, tag: bool = False) -> str:
    """The full destination ref: ``refs/tags/<dst>`` for a tag, else ``refs/heads/<dst>``."""
    if dst.startswith(TAGS):
        tag, name = True, dst[len(TAGS):]
    elif dst.startswith(HEADS):
        tag, name = False, dst[len(HEADS):]
    else:
        name = dst
    prefix = TAGS if tag else HEADS
    what = "tag" if tag else "branch"
    ref = prefix + name
    try:
        check_ref_name(ref)
        if os.name == "nt" and not delete:  # the checks refs.update makes, before contacting the server
            _check_windows_ref_name(ref)
    except MinigitError as e:
        prefix_text = f"invalid ref name '{ref}': "
        reason = e.message[len(prefix_text):] if e.message.startswith(prefix_text) else ""
        raise UsageError(f"invalid destination {what} name '{dst}'" + (f": {reason}" if reason else "")) from None
    if not name:
        raise UsageError(f"empty destination {what} name")
    if name == "HEAD" and not tag and not delete:  # git refuses a branch named HEAD too
        raise UsageError(f"invalid destination branch name '{dst}': 'HEAD' is not a valid branch name")
    return ref


def _tag_exists_error(url: str, ref: str, remote_old: str) -> PushRejectedError:
    name = ref[len(TAGS):]
    return PushRejectedError("push rejected", url=url, code="rejected", results=[{
        "ref": ref, "status": "rejected", "reason": "tag-exists", "current": remote_old,
        "message": _REJECT_MESSAGES["tag-exists"].format(name=name)}])


def _set_upstream(repo: Repository, branch: str, remote: str, merge_ref: str) -> None:
    cfg = repo.config
    cfg.set(f"branch.{branch}.remote", remote)
    cfg.set(f"branch.{branch}.merge", merge_ref)


def push(repo: Repository, remote: str | None = None, refspec: str | None = None, *,
         set_upstream: bool = False, force: bool = False, delete: bool = False) -> list[str]:
    remote = remote or _default_remote(repo)
    url = remote_url(repo, remote)
    shown = redact_url(url)
    store = repo.objects

    # -- parse the refspec ------------------------------------------------
    src: str | None
    if refspec is None:
        if delete:
            raise UsageError("--delete needs the name of the branch to delete")
        current = _head_branch(repo)
        if current is None:
            raise MinigitError("you are not on a branch (detached HEAD); "
                               f"use: minigit push {remote} HEAD:<branch>")
        src, dst = current, current
    elif delete:
        src, dst = None, refspec.split(":", 1)[-1]
    elif ":" in refspec:
        src, dst = refspec.split(":", 1)
        if src == "":
            src, delete = None, True
        if not dst:
            raise UsageError(f"invalid refspec '{refspec}': missing destination")
    elif refspec in ("HEAD", "@"):
        # as in git: the current branch, to the branch of the same name (never a branch named "HEAD")
        current = _head_branch(repo)
        if current is None:
            raise MinigitError("you are not on a branch (detached HEAD); "
                               f"use: minigit push {remote} HEAD:<branch>")
        src, dst = "HEAD", current
    else:
        src, dst = refspec, refspec

    local: str | None = None
    src_branch: str | None = None
    src_is_tag = False
    if src is not None:
        local, src_branch, src_is_tag = _resolve_src(repo, src)
    src_label = src_branch or (src if src is not None else "")
    if src_label.startswith(TAGS):
        src_label = src_label[len(TAGS):]
    if not delete:  # validated before the server is contacted
        dst_ref = _dst_ref(dst, False, tag=src_is_tag)

    rem = HttpRemote(url)
    info = rem.info_refs()

    # -- delete -------------------------------------------------------------
    if delete:
        if dst.startswith(HEADS) or dst.startswith(TAGS):
            dst_ref = _dst_ref(dst, True)
        else:  # an unqualified name: whichever the remote has (as in git, both is ambiguous)
            candidates = [r for r in (HEADS + dst, TAGS + dst) if r in info.refs]
            if len(candidates) > 1:
                raise MinigitError(f"dst refspec '{dst}' matches more than one (a branch and a tag)",
                                   hint=f"say which one: refs/heads/{dst} or refs/tags/{dst}")
            dst_ref = _dst_ref(candidates[0] if candidates else dst, True)
        is_tag = dst_ref.startswith(TAGS)
        dst_name = dst_ref[len(TAGS) if is_tag else len(HEADS):]
        remote_old = info.refs.get(dst_ref)
        if remote_old is None:
            raise MinigitError(f"unable to delete '{dst_name}': remote ref does not exist")
        try:
            rem.push([{"ref": dst_ref, "old": remote_old, "new": None}],
                     pack_mod.build_pack(store, []), force=force)
        except PushRejectedError as err:
            err.src, err.dst, err.url = "", dst_name, shown
            raise
        if not is_tag:
            tracking = f"refs/remotes/{remote}/{dst_name}"
            if repo.refs.resolve(tracking) is not None:
                repo.refs.update(tracking, None)
        return [f"To {shown}", f" - {'[deleted]':<17} {dst_name}"]

    assert local is not None
    is_tag = dst_ref.startswith(TAGS)
    dst_name = dst_ref[len(TAGS) if is_tag else len(HEADS):]
    tracking = f"refs/remotes/{remote}/{dst_name}"
    remote_old = info.refs.get(dst_ref)
    upstream_lines: list[str] = []

    def finish_upstream() -> None:
        if set_upstream and src_branch and not is_tag:
            _set_upstream(repo, src_branch, remote, dst_ref)
            upstream_lines.append(f"branch '{src_branch}' set up to track '{remote}/{dst_name}'.")

    if remote_old == local:
        if not is_tag and repo.refs.resolve(tracking) != local:
            repo.refs.update(tracking, local, expected=ANY)
        finish_upstream()
        return upstream_lines + ["Everything up-to-date"]  # git prints the upstream line first

    forced_update = False
    if remote_old is not None and is_tag:
        if not force:  # a tag on the remote is never moved silently
            err = _tag_exists_error(shown, dst_ref, remote_old)
            err.src, err.dst = src_label, dst_name
            raise err
        forced_update = True
    elif remote_old is not None:
        known = store.has(remote_old)
        fast_forward = False
        if known:
            try:
                fast_forward = graph.is_ancestor(store, remote_old, local)
            except MinigitError:
                fast_forward = False
        if not fast_forward:
            if not force:
                reason = "non-fast-forward" if known else "fetch-first"
                err = PushRejectedError("push rejected", url=shown, code="rejected", results=[{
                    "ref": dst_ref, "status": "rejected", "reason": reason,
                    "current": remote_old, "message": _REJECT_MESSAGES[reason]}])
                err.src, err.dst = src_label, dst_name
                raise err
            forced_update = True

    exclude = [sha for sha in info.refs.values() if store.has(sha)]
    shas = pack_mod.objects_to_send(store, [local], exclude)
    data = pack_mod.build_pack(store, shas)
    try:
        rem.push([{"ref": dst_ref, "old": remote_old, "new": local}], data, force=force)
    except PushRejectedError as err:
        err.src, err.dst, err.url = src_label, dst_name, shown
        raise

    if not is_tag:
        repo.refs.update(tracking, local, expected=ANY)
    finish_upstream()
    return [f"To {shown}", _push_line(src_label, dst_name, remote_old, local, forced_update, is_tag)] + upstream_lines


def _push_line(src_label: str, dst_name: str, remote_old: str | None, local: str, forced: bool,
               is_tag: bool) -> str:
    if remote_old is None:
        return f" * {'[new tag]' if is_tag else '[new branch]':<17} {src_label} -> {dst_name}"
    if forced:
        return f" + {_abbrev(remote_old) + '...' + _abbrev(local):<17} {src_label} -> {dst_name} (forced update)"
    return f"   {_abbrev(remote_old) + '..' + _abbrev(local):<17} {src_label} -> {dst_name}"


def push_tags(repo: Repository, remote: str | None = None, *, force: bool = False) -> list[str]:
    """``push --tags``: upload every local tag the remote lacks (one request, all or nothing).

    A tag that exists on the remote with another value is left alone and reported as rejected
    (``PushRejectedError``, after the others were pushed) unless ``force`` is given.
    """
    remote = remote or _default_remote(repo)
    rem = HttpRemote(remote_url(repo, remote))
    url = redact_url(remote_url(repo, remote))
    store = repo.objects
    local_tags = repo.refs.list(TAGS)
    info = rem.info_refs()
    updates: list[dict] = []
    lines: list[str] = []
    rejected: list[dict] = []
    for ref, local in sorted(local_tags.items()):
        name = ref[len(TAGS):]
        remote_old = info.refs.get(ref)
        if remote_old == local:
            continue
        if remote_old is not None and not force:
            rejected.append(_tag_exists_error(url, ref, remote_old).results[0])
            continue
        updates.append({"ref": ref, "old": remote_old, "new": local})
        lines.append(_push_line(name, name, remote_old, local, remote_old is not None, True))
    if updates:
        exclude = [sha for sha in info.refs.values() if store.has(sha)]
        shas = pack_mod.objects_to_send(store, [u["new"] for u in updates], exclude)
        try:
            rem.push(updates, pack_mod.build_pack(store, shas), force=force)
        except PushRejectedError as err:
            err.url = url
            raise
    if rejected:
        err = PushRejectedError("push rejected", url=url, code="rejected", results=rejected)
        err.src = err.dst = rejected[0]["ref"][len(TAGS):]
        err.accepted = lines
        raise err
    if not lines:
        return ["Everything up-to-date"]
    return [f"To {url}"] + lines


def _ref_display_name(ref: str) -> str:
    for prefix in (HEADS, TAGS):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def format_push_rejection(err: PushRejectedError, src: str | None = None, dst: str | None = None) -> str:
    """Section 7.5 rejection text.  ``src``/``dst`` default to the names push() attached."""
    if src is None:
        src = getattr(err, "src", None) or ""
    if dst is None:
        dst = getattr(err, "dst", None) or src
    results = getattr(err, "results", None) or []
    rejected = [r for r in results if isinstance(r, dict) and r.get("status", "rejected") != "ok"]
    reason = rejected[0].get("reason") if rejected else None
    url = redact_url(getattr(err, "url", "") or "")
    src = _ref_display_name(src)
    dst = _ref_display_name(dst)
    lines = [f"To {url}"] + list(getattr(err, "accepted", None) or [])
    if len(rejected) > 1:  # several refs in one push (push --tags): one line each
        for r in rejected:
            name = _ref_display_name(str(r.get("ref", "")))
            text = _REASON_TEXT.get(r.get("reason") or "", r.get("reason") or "rejected")
            lines.append(f" ! {'[rejected]':<17} {name} -> {name} ({text})")
    else:
        text = _REASON_TEXT.get(reason or "", reason or "rejected")
        lines.append(f" ! {'[rejected]':<17} {src} -> {dst} ({text})")
    lines.append(f"error: failed to push some refs to '{url}'")
    if reason == "delete-default-branch":
        lines.append("hint: The remote's default branch cannot be deleted.")
    elif reason == "tag-exists":
        lines.append("hint: Updates were rejected because the tag already exists in the remote.")
        lines.append("hint: Delete it there first ('minigit push --delete <remote> <tag>') or push with --force.")
    elif reason in ("ref-clash", "invalid-ref-name"):
        detail = next((r.get("message") for r in results if isinstance(r, dict) and r.get("message")), None)
        if detail:
            lines.insert(2, f"error: {detail}")
        lines.append("hint: Choose a different branch name; pulling will not resolve this.")
    elif reason == "force-disabled":  # pulling would not help: the policy refuses any rewrite
        lines.append("hint: this repository does not allow forced pushes "
                     "(ask the admin for --allow-force or push a new branch)")
    else:
        if reason == "non-fast-forward":
            lines.append("hint: Updates were rejected because the tip of your branch is behind its remote "
                         "counterpart.")
        else:
            lines.append("hint: Updates were rejected because the remote contains work that you do not have "
                         "locally.")
        lines.append("hint: Integrate the remote changes first ('minigit pull'), then push again.")
    return "\n".join(lines)


# -------------------------------------------------------------- login ----

def login(url: str, user: str, token: str) -> None:
    clean, _ = split_url(url)
    check_url(clean)
    if not user:
        raise UsageError("login needs --user")
    if not token:
        raise UsageError("login needs --token")
    save_credentials(clean, user, token, host_default=True)
