"""minigit.cherry -- CLI glue for ``cherry-pick`` and ``revert`` (features/cherry-pick-revert.md).

``add_parser(subparsers)`` registers both commands and ``cmd_cherry_pick`` / ``cmd_revert`` run them;
``cli.py`` only wires these in. Like ``cli.py`` this module imports the library lazily (the engine is
``minigit/sequencer.py``), so building the parser never imports the library.
"""
from __future__ import annotations

import importlib


def _m(name: str):
    return importlib.import_module("minigit." + name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register ``cherry-pick`` and ``revert`` on the CLI's subparsers action."""
    range_help = ("a commit, or a range A..B (the commits reachable from B but not from A, applied oldest "
                  "first; A^..B includes A); several may be mixed and are applied in order")
    help_ = "Apply the change of an existing commit on top of HEAD"
    s = subparsers.add_parser("cherry-pick", help=help_,
                              description=help_ + " (several revisions or A..B ranges: in order)")
    s.add_argument("-n", "--no-commit", dest="no_commit", action="store_true",
                   help="apply to the index and working tree, do not commit")
    s.add_argument("-x", dest="record_origin", action="store_true",
                   help="append '(cherry picked from commit ...)' to the message")
    s.add_argument("-m", "--mainline", type=int, default=None, metavar="N",
                   help="for a merge commit, the parent (1-based) to diff against")
    s.add_argument("--allow-empty", dest="allow_empty", action="store_true",
                   help="record the commit even when it changes nothing")
    s.add_argument("--continue", dest="continue_", action="store_true",
                   help="commit the resolved conflicts and go on")
    s.add_argument("--abort", action="store_true", help="cancel and restore the pre-operation state")
    s.add_argument("revs", nargs="*", metavar="rev", help="what to apply: " + range_help)

    help_ = "Record a new commit that undoes an existing commit"
    s = subparsers.add_parser("revert", help=help_,
                              description=help_ + " (several revisions or A..B ranges: in order)")
    s.add_argument("-n", "--no-commit", dest="no_commit", action="store_true",
                   help="apply to the index and working tree, do not commit")
    s.add_argument("-m", "--mainline", type=int, default=None, metavar="N",
                   help="for a merge commit, the parent (1-based) to revert to")
    s.add_argument("--continue", dest="continue_", action="store_true",
                   help="commit the resolved conflicts and go on")
    s.add_argument("--abort", action="store_true", help="cancel and restore the pre-operation state")
    s.add_argument("revs", nargs="*", metavar="rev", help="what to revert: " + range_help)


def _say(lines) -> None:
    from .cli import _say as say
    say("\n".join(line for line in lines if line))


def _report_conflict(outcome: PickOutcome) -> int:
    from .cli import _stderr
    _stderr(f"error: {outcome.error}")
    _stderr(f"hint: {outcome.hint}")
    return 1


def _cmd(ns, cwd: str, action: str) -> int:
    seq = _m("sequencer")
    UsageError, MinigitError = _m("errors").UsageError, _m("errors").MinigitError
    repo = _m("repo").Repository.discover(cwd)
    if ns.continue_ and ns.abort:
        raise UsageError(f"{action}: --continue and --abort are mutually exclusive")
    if ns.abort or ns.continue_:
        if ns.revs:
            raise UsageError(f"{action} --abort/--continue takes no revision")
        if ns.abort:
            seq.abort(repo, action)
            return 0
        try:
            _say(seq.continue_(repo, action))
        except seq.Stopped as stop:
            _say(stop.lines)
            return _report_conflict(stop.outcome)
        return 0
    if not ns.revs:
        raise UsageError(f"{action}: a revision is required")
    try:
        outcomes = seq.start(repo, action, list(ns.revs), mainline=ns.mainline, no_commit=ns.no_commit,
                            record_origin=getattr(ns, "record_origin", False),
                            allow_empty=getattr(ns, "allow_empty", False))
    except MinigitError as e:
        for out in getattr(e, "outcomes", []):   # earlier commits of a sequence did land
            _say(out.messages + [out.summary] + out.details)
        raise
    for out in outcomes:
        _say(out.messages + [out.summary] + out.details)
    if outcomes[-1].kind == "conflict":
        return _report_conflict(outcomes[-1])
    return 0


def cmd_cherry_pick(ns, cwd: str) -> int:
    return _cmd(ns, cwd, "cherry-pick")


def cmd_revert(ns, cwd: str) -> int:
    return _cmd(ns, cwd, "revert")
