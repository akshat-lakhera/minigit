"""minigit.pr -- owner: M6 client. ``minigit pr`` (features/pull-requests-lite.md).

Client porcelain and formatting for pull requests stored on the server, plus the CLI glue
(``add_parser(subparsers)`` and ``cmd_pr(ns, cwd)``; ``cli.py`` only wires these in). The library
functions never print; ``cmd_pr`` prints through the CLI's output helpers.
"""
from __future__ import annotations

import importlib

CONFLICT_HINT = "merge '{base}' into '{head}' locally, resolve the conflicts, push '{head}', then merge again"
CONFLICT_HINT_PLAIN = "merge the base branch into the head branch locally, resolve the conflicts, push it, then merge again"
HEADS = "refs/heads/"


def _m(name: str):
    return importlib.import_module("minigit." + name)


def _short(name: str) -> str:
    return name[len(HEADS):] if name.startswith(HEADS) else name


# ---------------------------------------------------------------------------
# porcelain (no printing)
# ---------------------------------------------------------------------------

def pr_remote(repo, remote: str | None):
    """(remote name, HttpRemote) for ``--remote`` or the default remote (as for push)."""
    rem_mod = _m("remote")
    name = remote or rem_mod._default_remote(repo)
    url = rem_mod.remote_url(repo, name)
    return name, _m("transport").HttpRemote(url)


def pr_open(repo, remote: str | None, *, title: str, body: str = "", head: str | None = None,
            base: str | None = None) -> tuple[dict, list[str]]:
    MinigitError = _m("errors").MinigitError
    if head is None:
        head = repo.refs.current_branch()
        if head is None:
            raise MinigitError("you are not on a branch; say which branch to merge with --head")
    head = _short(head)
    name, rem = pr_remote(repo, remote)
    info = rem.info_refs()
    if base is None:
        if info.head is None:
            raise MinigitError("the remote has no default branch; use --base")
        base = _short(info.head)
    base = _short(base)
    remote_tip = info.refs.get(HEADS + head)
    if remote_tip is None:
        raise MinigitError(f"branch '{head}' does not exist on '{name}'",
                           hint=f"push it first: minigit push -u {name} {head}")
    messages: list[str] = []
    try:
        local_tip = repo.refs.resolve(HEADS + head)
    except MinigitError:
        local_tip = None
    if local_tip is not None and local_tip != remote_tip:
        messages.append(f"warning: your local '{head}' differs from '{name}/{head}'; "
                        "the pull request uses the branch on the server")
    return rem.open_pull(head, base, title, body), messages


def pr_list(repo, remote: str | None, state: str = "open") -> list[dict]:
    return pr_remote(repo, remote)[1].list_pulls(state)


def pr_show(repo, remote: str | None, number: int) -> dict:
    return pr_remote(repo, remote)[1].get_pull(number)


def pr_diff(repo, remote: str | None, number: int) -> bytes:
    return pr_remote(repo, remote)[1].pull_diff(number)


def pr_merge(repo, remote: str | None, number: int, message: str | None = None) -> dict:
    errors = _m("errors")
    rem = pr_remote(repo, remote)[1]
    try:
        return rem.merge_pull(number, message)
    except errors.RemoteError as e:
        if e.code != "merge_conflict":
            raise
        payload = e.payload or {}
        head, base = payload.get("head"), payload.get("base")
        if isinstance(head, str) and isinstance(base, str):
            hint = CONFLICT_HINT.format(head=head, base=base)
        else:
            hint = CONFLICT_HINT_PLAIN
        raise errors.MinigitError(e.message, hint=hint) from None


def pr_close(repo, remote: str | None, number: int) -> dict:
    return pr_remote(repo, remote)[1].close_pull(number)


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def format_pull_line(pull: dict) -> str:
    return (f"#{pull.get('number'):<3} {pull.get('state'):<7} {pull.get('head')} -> {pull.get('base')}  "
            f"{pull.get('title')}  ({pull.get('author')})")


def format_pull_list(pulls: list[dict]) -> str:
    return "\n".join(format_pull_line(p) for p in pulls)


def _state_text(pull: dict) -> str:
    state = pull.get("state")
    if state == "merged":
        return f"merged by {pull.get('merged_by')} as {(pull.get('merge_commit') or '')[:7]}"
    if state == "closed":
        return f"closed by {pull.get('closed_by')}"
    return str(state)


def _mergeable_text(pull: dict) -> str:
    m = pull.get("mergeable")
    if m == "conflict":
        conflicts = pull.get("conflicts") or []
        return f"conflict ({', '.join(conflicts)})" if conflicts else "conflict"
    if m == "unrelated":
        return "unrelated histories"
    if m == "unknown":
        missing = pull.get("head") if pull.get("head_sha") is None else pull.get("base")
        return f"unknown (branch '{missing}' missing)"
    return str(m)


def format_pull_show(pull: dict) -> str:
    ahead, behind = pull.get("ahead", 0), pull.get("behind", 0)
    commits_word = "commit" if ahead == 1 else "commits"
    lines = [
        f"#{pull.get('number')} {pull.get('title')}",
        f"State:     {_state_text(pull)}",
        f"Author:    {pull.get('author')}",
        f"Branches:  {pull.get('head')} -> {pull.get('base')}  ({ahead} {commits_word} ahead, {behind} behind)",
    ]
    if pull.get("state") == "open":
        lines.append(f"Mergeable: {_mergeable_text(pull)}")
    elif isinstance(pull.get("missing_branch"), str):
        lines.append(f"note: branch '{pull['missing_branch']}' no longer exists")
    body = pull.get("body") or ""
    if body.strip():
        lines.append("")
        lines.extend(("    " + ln) if ln.strip() else "" for ln in body.rstrip("\n").split("\n"))
    commits = pull.get("commits") or []
    if commits:
        lines.append("")
        lines.extend(f"{c.get('sha', '')[:7]} {c.get('subject', '')}" for c in commits)
    return "\n".join(lines)


def format_open_result(pull: dict) -> str:
    return (f"Created pull request #{pull.get('number')}: {pull.get('title')} "
            f"({pull.get('head')} -> {pull.get('base')})")


def format_merge_result(result: dict) -> str:
    pull = result.get("pull") or {}
    if result.get("kind") == "up-to-date":
        detail = "already up to date"
    else:
        detail = (result.get("merge_commit") or "")[:7]
    return f"Merged pull request #{pull.get('number')} into {pull.get('base')} ({detail})"


def format_close_result(pull: dict) -> str:
    return f"Closed pull request #{pull.get('number')}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SUBCOMMANDS = ("open", "list", "show", "diff", "merge", "close")


def add_parser(subparsers) -> None:
    """Register ``pr`` and its subcommands on the CLI's subparsers action."""
    help_ = "Pull requests on the server: open, list, show, diff, merge, close"
    s = subparsers.add_parser("pr", help=help_, description=help_)
    ss = s.add_subparsers(dest="pr_cmd", metavar="<subcommand>", parser_class=type(s))

    def sub(name: str, help_text: str):
        p = ss.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--remote", default=None, metavar="NAME",
                       help="remote to talk to (default: the branch's remote, else origin)")
        return p

    p = sub("open", "Open a pull request to merge a branch on the server into another")
    p.add_argument("-t", "--title", required=True, help="one-line title")
    p.add_argument("-m", "--body", action="append", default=None, metavar="BODY",
                   help="description (repeat for several paragraphs)")
    p.add_argument("--head", default=None, metavar="BRANCH", help="branch to merge (default: current branch)")
    p.add_argument("--base", default=None, metavar="BRANCH",
                   help="branch to merge into (default: the remote's default branch)")
    p = sub("list", "List pull requests")
    p.add_argument("--state", choices=("open", "merged", "closed", "all"), default="open",
                   help="which pull requests to list (default: open)")
    for name, text in (("show", "Show a pull request"), ("diff", "Show the changes of a pull request"),
                       ("merge", "Merge a pull request on the server"), ("close", "Close a pull request")):
        p = sub(name, text)
        p.add_argument("number", type=int, metavar="N", help="pull request number")
        if name == "merge":
            p.add_argument("-m", "--message", default=None, metavar="MESSAGE",
                           help="merge commit message (default: 'Merge pull request #N from <head>')")


def cmd_pr(ns, cwd: str) -> int:
    from .cli import _out, _say, _warn

    errors = _m("errors")
    action = getattr(ns, "pr_cmd", None)
    if action is None:
        raise errors.UsageError("pr: a subcommand is required (" + ", ".join(SUBCOMMANDS) + ")")
    number = getattr(ns, "number", None)
    if number is not None and number < 1:
        raise errors.UsageError("pr: N must be a positive number")
    repo = _m("repo").Repository.discover(cwd)
    if action == "open":
        body = "\n\n".join(ns.body) if ns.body else ""
        view, messages = pr_open(repo, ns.remote, title=ns.title, body=body, head=ns.head, base=ns.base)
        for msg in messages:
            _warn(msg)
        _say(format_open_result(view))
    elif action == "list":
        _say(format_pull_list(pr_list(repo, ns.remote, ns.state)))
    elif action == "show":
        _say(format_pull_show(pr_show(repo, ns.remote, number)))
    elif action == "diff":
        _out(pr_diff(repo, ns.remote, number))
    elif action == "merge":
        _say(format_merge_result(pr_merge(repo, ns.remote, number, ns.message)))
    else:
        _say(format_close_result(pr_close(repo, ns.remote, number)))
    return 0
