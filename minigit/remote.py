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
from .transport import HttpRemote, RemoteRefs, check_url, save_credentials, split_url, url_with_user

if TYPE_CHECKING:  # pragma: no cover
    from .merge import MergeOutcome

HEADS = "refs/heads/"
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass
class RefChange:
    ref: str            # the remote-tracking ref, e.g. "refs/remotes/origin/main"
    old: str | None
    new: str | None
    kind: str           # "new" | "updated" | "forced" | "deleted" | "up-to-date"


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
    cfg = repo.config
    out: dict[str, str] = {}
    for name in sorted(cfg.subsections("remote")):
        url = cfg.get(f"remote.{name}.url")
        if url:
            out[name] = url
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


def _fetch_with(repo: Repository, remote: str, rem: HttpRemote, info: RemoteRefs) -> list[RefChange]:
    store = repo.objects
    heads = {ref: sha for ref, sha in info.refs.items() if ref.startswith(HEADS)}
    haves = _local_tips(repo)
    complete = pack_mod.complete_commits(store, haves)
    # A tip is wanted unless it is present *and* fully connected locally.
    wants = sorted({sha for sha in heads.values() if not _is_connected(repo, sha, complete)})
    if wants:
        data = rem.fetch_pack(wants, haves)
        pack_mod.unpack(store, data)
        complete = pack_mod.complete_commits(store, haves)
        for sha in wants:
            pack_mod.check_connectivity(store, sha, complete)

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
    changes: list[RefChange] = []
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
    order = {"deleted": 1}
    changes.sort(key=lambda c: (order.get(c.kind, 0), c.ref))
    return changes


def fetch(repo: Repository, remote: str | None = "origin") -> list[RefChange]:
    remote = remote or _default_remote(repo)
    url = remote_url(repo, remote)
    rem = HttpRemote(url)
    return _fetch_with(repo, remote, rem, rem.info_refs())


def format_fetch(url: str, changes: list[RefChange]) -> str:
    lines: list[str] = []
    for c in changes:
        if c.kind == "up-to-date":
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
    return "\n".join([f"From {url}"] + lines)


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
        raise MinigitError(f"destination '{directory}' already exists and is not empty")

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
         allow_unrelated: bool = False) -> tuple[list[RefChange], "MergeOutcome"]:
    repo.require_worktree()
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
    tracking = f"refs/remotes/{remote}/{branch}"
    if repo.refs.resolve(tracking) is None:
        if branch_arg is not None or configured:
            raise MinigitError(f"couldn't find remote ref '{branch}' on '{remote}'",
                               hint="check the branch name with: minigit fetch")
        raise MinigitError(f"no upstream '{remote}/{branch}' (push with -u first)")
    shown_url = split_url(url)[0]  # the user name of the URL does not belong in history
    outcome = merge_mod.merge(repo, tracking, label=f"{remote}/{branch}",
                              message=f"Merge branch '{branch}' of {shown_url}", allow_unrelated=allow_unrelated)
    return changes, outcome


# --------------------------------------------------------------- push ----

_REASON_TEXT = {
    "non-fast-forward": "non-fast-forward",
    "fetch-first": "fetch first",
    "stale": "fetch first",  # the ref moved between our info/refs and the push (a lost race)
    "force-disabled": "forced update disabled",
    "delete-default-branch": "deletion of the default branch prohibited",
    "ref-clash": "ref name clash",
    "invalid-ref-name": "invalid ref name",
}

_REJECT_MESSAGES = {
    "non-fast-forward": "the remote branch has commits you do not have; run 'minigit pull' first, then push again",
    "fetch-first": "the remote branch has commits you do not have; run 'minigit pull' first, then push again",
}


def _resolve_src(repo: Repository, src: str) -> tuple[str, str | None]:
    """Return (commit sha, local branch name or None) for a push source."""
    if src in ("HEAD", "@"):
        sha = repo.head_commit()
        if sha is None:
            raise MinigitError("src refspec 'HEAD' does not match any commit (make a commit first)")
        return sha, _head_branch(repo)
    name = src[len(HEADS):] if src.startswith(HEADS) else src
    local_ref = revparse.resolve_local_branch(repo, name)
    if local_ref is not None:
        sha = repo.refs.resolve(local_ref)
        if sha:
            return sha, local_ref[len(HEADS):]
    if _head_branch(repo) == name and repo.head_commit() is None:
        raise MinigitError(f"src refspec '{src}' does not match any commit (make a commit first)")
    try:
        return revparse.resolve_rev(repo, src), None
    except MinigitError:
        raise MinigitError(f"src refspec '{src}' does not match any commit") from None


def _dst_ref(dst: str, delete: bool = False) -> str:
    name = dst[len(HEADS):] if dst.startswith(HEADS) else dst
    ref = HEADS + name
    try:
        check_ref_name(ref)
        if os.name == "nt" and not delete:  # the checks refs.update makes, before contacting the server
            _check_windows_ref_name(ref)
    except MinigitError as e:
        prefix = f"invalid ref name '{ref}': "
        reason = e.message[len(prefix):] if e.message.startswith(prefix) else ""
        raise UsageError(f"invalid destination branch name '{dst}'" + (f": {reason}" if reason else "")) from None
    if not name:
        raise UsageError("empty destination branch name")
    return ref


def _set_upstream(repo: Repository, branch: str, remote: str, merge_ref: str) -> None:
    cfg = repo.config
    cfg.set(f"branch.{branch}.remote", remote)
    cfg.set(f"branch.{branch}.merge", merge_ref)


def push(repo: Repository, remote: str | None = None, refspec: str | None = None, *,
         set_upstream: bool = False, force: bool = False, delete: bool = False) -> list[str]:
    remote = remote or _default_remote(repo)
    url = remote_url(repo, remote)
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
    else:
        src, dst = refspec, refspec
    dst_ref = _dst_ref(dst, delete)
    dst_name = dst_ref[len(HEADS):]
    tracking = f"refs/remotes/{remote}/{dst_name}"

    local: str | None = None
    src_branch: str | None = None
    if src is not None:
        local, src_branch = _resolve_src(repo, src)
    src_label = src_branch or (src if src is not None else "")

    rem = HttpRemote(url)
    info = rem.info_refs()
    remote_old = info.refs.get(dst_ref)

    # -- delete -------------------------------------------------------------
    if delete:
        if remote_old is None:
            raise MinigitError(f"unable to delete '{dst_name}': remote ref does not exist")
        try:
            rem.push([{"ref": dst_ref, "old": remote_old, "new": None}],
                     pack_mod.build_pack(store, []), force=force)
        except PushRejectedError as err:
            err.src, err.dst = "", dst_name
            raise
        if repo.refs.resolve(tracking) is not None:
            repo.refs.update(tracking, None)
        return [f"To {url}", f" - {'[deleted]':<17} {dst_name}"]

    assert local is not None
    upstream_lines: list[str] = []

    def finish_upstream() -> None:
        if set_upstream and src_branch:
            _set_upstream(repo, src_branch, remote, dst_ref)
            upstream_lines.append(f"branch '{src_branch}' set up to track '{remote}/{dst_name}'.")

    if remote_old == local:
        if repo.refs.resolve(tracking) != local:
            repo.refs.update(tracking, local, expected=ANY)
        finish_upstream()
        return ["Everything up-to-date"] + upstream_lines

    forced_update = False
    if remote_old is not None:
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
                err = PushRejectedError("push rejected", url=url, code="rejected", results=[{
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
        err.src, err.dst = src_label, dst_name
        raise

    repo.refs.update(tracking, local, expected=ANY)
    finish_upstream()
    if remote_old is None:
        line = f" * {'[new branch]':<17} {src_label} -> {dst_name}"
    elif forced_update:
        line = f" + {_abbrev(remote_old) + '...' + _abbrev(local):<17} {src_label} -> {dst_name} (forced update)"
    else:
        line = f"   {_abbrev(remote_old) + '..' + _abbrev(local):<17} {src_label} -> {dst_name}"
    return [f"To {url}", line] + upstream_lines


def format_push_rejection(err: PushRejectedError, src: str | None = None, dst: str | None = None) -> str:
    """Section 7.5 rejection text.  ``src``/``dst`` default to the names push() attached."""
    if src is None:
        src = getattr(err, "src", None) or ""
    if dst is None:
        dst = getattr(err, "dst", None) or src
    results = getattr(err, "results", None) or []
    reason = None
    for r in results:
        if isinstance(r, dict) and r.get("status", "rejected") != "ok":
            reason = r.get("reason")
            break
    text = _REASON_TEXT.get(reason or "", reason or "rejected")
    url = getattr(err, "url", "") or ""
    src = src[len(HEADS):] if src.startswith(HEADS) else src
    dst = dst[len(HEADS):] if dst.startswith(HEADS) else dst
    lines = [f"To {url}",
             f" ! {'[rejected]':<17} {src} -> {dst} ({text})",
             f"error: failed to push some refs to '{url}'"]
    if reason == "delete-default-branch":
        lines.append("hint: The remote's default branch cannot be deleted.")
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
