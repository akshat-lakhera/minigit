"""minigit.status -- owner: M3 commands. See SPEC.md section 8 for the public API.

Status computation (SPEC section 6, "status").  ``compute_status`` only reads
the index; it never writes anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from minigit.refs import short_ref

Entry = tuple[str, str]

UNMERGED_CODES = {
    frozenset({1, 2, 3}): "UU",
    frozenset({2, 3}): "AA",
    frozenset({1, 2}): "UD",
    frozenset({1, 3}): "DU",
    frozenset({2}): "AU",
    frozenset({3}): "UA",
}

UNMERGED_LABELS = {
    "UU": "both modified:",
    "AA": "both added:",
    "UD": "deleted by them:",
    "DU": "deleted by us:",
    "AU": "added by us:",
    "UA": "added by them:",
}

STAGED_LABELS = {"A": "new file:", "M": "modified:", "D": "deleted:"}
UNSTAGED_LABELS = {"M": "modified:", "D": "deleted:"}


@dataclass
class StatusResult:
    branch: str | None
    head: str | None
    detached: bool
    unborn: bool
    upstream: str | None
    ahead: int
    behind: int
    merging: bool
    entries: list[tuple[str, str]] = field(default_factory=list)
    cherry_picking: str | None = None   # sha from CHERRY_PICK_HEAD
    reverting: str | None = None        # sha from REVERT_HEAD

    def is_clean(self) -> bool:
        return not self.entries


def tracking_ref(remote: str, merge_ref: str) -> str:
    """``("origin", "refs/heads/main") -> "refs/remotes/origin/main"``."""
    if remote == ".":
        return merge_ref
    short = merge_ref[len("refs/heads/"):] if merge_ref.startswith("refs/heads/") else merge_ref
    return f"refs/remotes/{remote}/{short}"


def upstream_of(repo, branch: str) -> tuple[str, str | None] | None:
    """(tracking ref name, its sha or None) for a branch with an upstream, else None."""
    cfg = repo.config
    remote = cfg.get(f"branch.{branch}.remote")
    merge = cfg.get(f"branch.{branch}.merge")
    if not remote or not merge:
        return None
    ref = tracking_ref(remote, merge)
    return ref, repo.refs.resolve(ref)


def compute_status(repo) -> StatusResult:
    from minigit.graph import ahead_behind, ancestors
    from minigit.ignore import IgnoreMatcher
    from minigit.index import read_index
    from minigit.worktree import fs_ignores_case, scan_files, worktree_entry

    repo.require_worktree()
    branch = repo.refs.current_branch()
    head = repo.head_commit()
    detached = branch is None
    unborn = head is None
    upstream = None
    ahead = behind = 0
    if branch is not None:
        up = upstream_of(repo, branch)
        if up is not None and up[1] is not None:
            upstream = short_ref(up[0])
            if head is not None:
                ahead, behind = ahead_behind(repo.objects, head, up[1])
            else:
                behind = len(ancestors(repo.objects, [up[1]]))
    merging = repo.merge_head() is not None

    head_flat = repo.head_flat()
    index = read_index(repo)
    staged = index.flat()
    unmerged = index.unmerged()

    entries: list[tuple[str, str]] = []
    for p, stages in unmerged.items():
        entries.append((UNMERGED_CODES.get(frozenset(stages), "UU"), p))
    for p in sorted(set(head_flat) | set(staged)):
        if p in unmerged:
            continue
        h = head_flat.get(p)
        i = staged.get(p)
        if h == i:
            x = " "
        elif h is None:
            x = "A"
        elif i is None:
            x = "D"
        else:
            x = "M"
        y = " "
        if i is not None:
            w = worktree_entry(repo, index, p, head_mode=h[0] if h else None)
            if w is None:
                y = "D"
            elif w != i:
                y = "M"
        if x + y != "  ":
            entries.append((x + y, p))
    ignore_case = fs_ignores_case(repo)
    for p in scan_files(repo, IgnoreMatcher(repo.worktree)):
        if p not in staged and p not in unmerged:
            if ignore_case and index.case_variant(p) is not None:
                continue  # a tracked file whose on-disk name differs only in case
            entries.append(("??", p))
    entries.sort(key=lambda e: (e[1], e[0]))
    return StatusResult(branch, head, detached, unborn, upstream, ahead, behind, merging, entries,
                        repo.cherry_pick_head(), repo.revert_head())


def _plural(n: int) -> str:
    return "commit" if n == 1 else "commits"


def format_status_long(st: StatusResult) -> str:
    lines: list[str] = []
    if st.detached:
        lines.append(f"HEAD detached at {(st.head or '')[:7]}")
    else:
        lines.append(f"On branch {st.branch}")
    if st.unborn:
        lines.append("")
        lines.append("No commits yet")
    if st.upstream is not None and not st.detached:
        if st.ahead and st.behind:
            lines.append(f"Your branch and '{st.upstream}' have diverged,")
            lines.append(f"and have {st.ahead} and {st.behind} different commits each, respectively.")
        elif st.ahead:
            lines.append(f"Your branch is ahead of '{st.upstream}' by {st.ahead} {_plural(st.ahead)}.")
        elif st.behind:
            lines.append(f"Your branch is behind '{st.upstream}' by {st.behind} {_plural(st.behind)}, "
                         "and can be fast-forwarded.")
        else:
            lines.append(f"Your branch is up to date with '{st.upstream}'.")

    staged: list[str] = []
    unmerged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for xy, path in st.entries:
        if xy == "??":
            untracked.append(f"\t{path}")
            continue
        if xy in UNMERGED_LABELS:
            unmerged.append(f"\t{UNMERGED_LABELS[xy]:<17}{path}")
            continue
        x, y = xy[0], xy[1]
        if x in STAGED_LABELS:
            staged.append(f"\t{STAGED_LABELS[x]:<12}{path}")
        if y in UNSTAGED_LABELS:
            unstaged.append(f"\t{UNSTAGED_LABELS[y]:<12}{path}")

    if st.merging:
        lines.append("You have unmerged paths." if unmerged else "All conflicts fixed but you are still merging.")
    for action, sha, verb in (("cherry-pick", st.cherry_picking, "cherry-picking"),
                              ("revert", st.reverting, "reverting")):
        if sha:
            lines.append(f"You are currently {verb} commit {sha[:7]}.")
            lines.append(f'  (fix conflicts and run "minigit {action} --continue")' if unmerged
                         else f'  (all conflicts fixed: run "minigit {action} --continue")')
            lines.append(f'  (use "minigit {action} --abort" to cancel the {action} operation)')

    sections = [
        ("Changes to be committed:", staged),
        ("Unmerged paths:", unmerged),
        ("Changes not staged for commit:", unstaged),
        ("Untracked files:", untracked),
    ]
    any_section = False
    for heading, items in sections:
        if not items:
            continue
        any_section = True
        lines.append("")
        lines.append(heading)
        lines.extend(items)
    if not any_section:
        if len(lines) > 1:
            lines.append("")
        lines.append("nothing to commit, working tree clean")
    return "\n".join(lines) + "\n"


def format_status_porcelain(st: StatusResult) -> str:
    return "".join(f"{xy} {path}\n" for xy, path in st.entries)
