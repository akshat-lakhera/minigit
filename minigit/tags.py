"""minigit.tags -- tags: ``refs/tags/<name>`` plus annotated tag objects (features/tags.md).

Library functions (list, create, delete) return data or message strings and raise ``MinigitError``
subclasses; ``add_parser`` and ``cmd_tag`` are the CLI glue that ``cli.py`` wires in. Like ``cli.py``
this module imports the library lazily, so building the parser never imports it.
"""
from __future__ import annotations

import fnmatch
import importlib
from dataclasses import dataclass

TAGS = "refs/tags/"
LIST_NAME_WIDTH = 15  # git's ``tag -n`` pads the name to 15 columns


def _m(name: str):
    return importlib.import_module("minigit." + name)


@dataclass
class TagInfo:
    name: str
    ref_sha: str            # what refs/tags/<name> holds: the tag object for an annotated tag
    commit: str | None      # the commit the tag names (peeled); None when it names no commit
    annotated: bool
    subject: str            # first message line (annotated) or the commit subject (lightweight)


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

def tag_ref(name: str) -> str:
    """``refs/tags/<name>`` for a valid tag name (git's ``check-ref-format`` rules)."""
    MinigitError = _m("errors").MinigitError
    FatalError = _m("errors").FatalError
    invalid = FatalError(f"'{name}' is not a valid tag name", hint=_m("ops")._REF_NAME_RULES)
    if (not name or name.startswith("refs/") or name.startswith("-") or name in ("HEAD", "@")
            or name.endswith(".") or name.endswith("/")):
        raise invalid
    ref = TAGS + name
    try:
        _m("refs").check_ref_name(ref)
    except MinigitError as e:
        if ": too long" in e.message:
            raise FatalError(f"{invalid.message}: {e.message.rsplit(': ', 1)[1]}") from None
        raise invalid from None
    return ref


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def read_tag(repo, sha: str):
    """Parse the annotated tag object ``sha`` (``ObjectError`` when it is not one)."""
    objects = _m("objects")
    return objects.parse_tag(repo.objects.read_typed(sha, "tag"))


def tag_info(repo, name: str, ref_sha: str) -> TagInfo:
    objects = _m("objects")
    errors = _m("errors")
    annotated = False
    subject = ""
    commit: str | None = None
    try:
        obj_type, data = repo.objects.read(ref_sha)
        if obj_type == "tag":
            annotated = True
            subject = objects.parse_tag(data).subject
        peeled_type, peeled = objects.peel(repo.objects, ref_sha)
        if peeled_type == "commit":
            commit = peeled
            if not annotated:
                subject = objects.parse_commit(repo.objects.read_typed(peeled, "commit")).subject
    except errors.MinigitError:
        pass  # a dangling tag is still listed, with what we know about it
    return TagInfo(name, ref_sha, commit, annotated, subject)


def tag_list(repo, pattern: str | None = None) -> list[TagInfo]:
    """Every tag, sorted by name; with ``pattern`` only the names ``fnmatch`` accepts."""
    items: list[TagInfo] = []
    for ref, sha in repo.refs.list(TAGS).items():
        name = ref[len(TAGS):]
        if pattern is not None and not fnmatch.fnmatchcase(name, pattern):
            continue
        items.append(tag_info(repo, name, sha))
    items.sort(key=lambda t: t.name)
    return items


def format_tag_list(items: list[TagInfo], with_message: bool = False) -> str:
    if with_message:
        return "".join(f"{t.name:<{LIST_NAME_WIDTH}} {t.subject}".rstrip() + "\n" for t in items)
    return "".join(t.name + "\n" for t in items)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _abbrev(sha: str) -> str:
    return sha[:7]


def tag_create(repo, name: str, rev: str | None = None, *, messages=None, annotate: bool = False,
               force: bool = False) -> str:
    """Create ``refs/tags/<name>`` at ``rev`` (default HEAD); annotated when ``annotate`` or a message is given.

    Returns ``""``, or ``Updated tag 'v1' (was abc1234)`` when ``force`` replaced an existing tag.
    Errors: ``tag 'v1' already exists``, ``no tag message``, ``'x' is not a valid tag name`` and the
    revision errors of ``revparse``.
    """
    errors = _m("errors")
    objects = _m("objects")
    refs = _m("refs")
    revparse = _m("revparse")

    ref = tag_ref(name)
    messages = [m for m in (messages or [])]
    annotate = annotate or bool(messages)
    current = repo.refs.resolve(ref)
    if current is not None and not force:
        raise errors.FatalError(f"tag '{name}' already exists")
    target = revparse.resolve_object(repo, rev if rev is not None else "HEAD")
    label = rev if rev is not None else "HEAD"
    # minigit tags name commits (possibly through another tag); a blob or tree is refused
    revparse.peel_to_commit(repo, target, label)
    if annotate:
        # the tag command's default cleanup: whitespace, and '#' lines are dropped even with -m
        message = _m("ops").cleanup_message("\n\n".join(messages), strip_comments=True)
        if not message:
            raise errors.MinigitError("no tag message", hint="give one with -m MSG")
        target_type = repo.objects.read(target)[0]
        tagger = repo.identity()  # identity errors surface before anything is written
        tag = objects.Tag(object=target, type=target_type, tag=name, tagger=tagger, message=message)
        value = repo.objects.write("tag", objects.serialize_tag(tag))
    else:
        value = target
    try:
        repo.refs.update(ref, value, expected=refs.ANY if force else None)
    except errors.RefUpdateError as e:
        if e.actual is not None:
            raise errors.FatalError(f"tag '{name}' already exists") from e
        raise
    if current is not None and current != value:
        return f"Updated tag '{name}' (was {_abbrev(current)})"
    return ""


def tag_delete(repo, name: str) -> str:
    errors = _m("errors")
    ref = TAGS + name
    try:
        _m("refs").check_ref_name(ref)
    except errors.MinigitError:
        raise errors.MinigitError(f"tag '{name}' not found.") from None
    sha = repo.refs.resolve(ref)
    if sha is None:
        raise errors.MinigitError(f"tag '{name}' not found.")
    repo.refs.update(ref, None, expected=sha)
    return f"Deleted tag '{name}' (was {_abbrev(sha)})"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register ``tag`` on the CLI's subparsers action."""
    help_ = "List, create or delete tags"
    s = subparsers.add_parser(
        "tag", help=help_,
        description=help_ + ". 'tag' lists, 'tag NAME [REV]' creates a lightweight tag, "
                            "'tag -a NAME [REV] -m MSG' an annotated one.")
    s.add_argument("-a", "--annotate", action="store_true", help="create an annotated tag object")
    s.add_argument("-m", "--message", dest="messages", action="append", default=[], metavar="MSG",
                   help="tag message (repeat for paragraphs); implies -a")
    s.add_argument("-f", "--force", action="store_true", help="replace an existing tag")
    s.add_argument("-d", "--delete", action="store_true", help="delete the named tags")
    s.add_argument("-l", "--list", dest="list_", action="store_true",
                   help="list tags whose names match the given shell patterns")
    s.add_argument("-n", dest="with_message", action="store_true",
                   help="when listing, show the first line of each tag's message")
    s.add_argument("args", nargs="*", metavar="name [rev] | pattern",
                   help="the tag to create (and the revision it names, default HEAD), "
                        "the tags to delete with -d, or the patterns to list with -l")


def cmd_tag(ns, cwd: str) -> int:
    from .cli import _say, _usage

    repo = _m("repo").Repository.discover(cwd)
    args = list(ns.args) + list(getattr(ns, "after", []) or [])
    if ns.delete:
        if ns.list_ or ns.annotate or ns.messages or ns.force:
            raise _usage("tag -d takes only tag names")
        if not args:
            raise _usage("tag: a tag name is required with -d")
        for name in args:
            _say(tag_delete(repo, name))
        return 0
    if ns.list_ or (not args and not ns.annotate and not ns.messages):
        if ns.annotate or ns.messages or ns.force:
            raise _usage("tag: -a, -m and -f create a tag; use them with a tag name")
        patterns = args or [None]
        seen: set[str] = set()
        items = []
        for pattern in patterns:
            for item in tag_list(repo, pattern):
                if item.name not in seen:
                    seen.add(item.name)
                    items.append(item)
        items.sort(key=lambda t: t.name)
        _say(format_tag_list(items, ns.with_message))
        return 0
    if not args:
        raise _usage("tag: a tag name is required")
    if len(args) > 2:
        raise _usage("tag: too many arguments (expected NAME [REV])")
    if ns.with_message:
        raise _usage("tag: -n only applies when listing tags")
    _say(tag_create(repo, args[0], args[1] if len(args) > 1 else None, messages=ns.messages,
                    annotate=ns.annotate, force=ns.force))
    return 0
