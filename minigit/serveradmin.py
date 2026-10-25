"""minigit.serveradmin -- the ``minigit server`` command group (features/server-admin.md).

cli.py calls ``add_server_parsers(ss)`` while building its parser and ``cmd_server(ns, cwd)`` to run
the command; the logic lives in ``minigit.auth`` and ``minigit.admin``.
"""
from __future__ import annotations

import time

SUBCOMMANDS = ("init", "add-user", "create-repo", "list", "remove-user", "add-token", "list-tokens",
               "revoke-token", "perm", "set-repo")


def add_server_parsers(ss) -> None:
    """Add every ``server`` subcommand to the subparsers action ``ss`` (created with ``_Parser``)."""
    t = ss.add_parser("init", help="create the server root")
    t.add_argument("root")
    t = ss.add_parser("add-user", help="add a user and print the token")
    t.add_argument("root")
    t.add_argument("name")
    t.add_argument("--reset", action="store_true", help="issue a new default token for an existing user")
    t = ss.add_parser("remove-user", help="delete a user, its tokens and its entries in all access lists")
    t.add_argument("root")
    t.add_argument("name")
    t = ss.add_parser("add-token", help="issue an additional named token for a user and print it")
    t.add_argument("root")
    t.add_argument("user")
    t.add_argument("name", help="token name (letters, digits, '_', '.', '-'); 'default' is the add-user token")
    t = ss.add_parser("list-tokens", help="list the token names of a user with their creation times")
    t.add_argument("root")
    t.add_argument("user")
    t = ss.add_parser("revoke-token", help="revoke one named token of a user, or all with --all")
    t.add_argument("root")
    t.add_argument("user")
    t.add_argument("name", nargs="?", help="token name ('default' is the add-user token)")
    t.add_argument("--all", dest="all_tokens", action="store_true", help="revoke every token of the user")
    t = ss.add_parser("create-repo", help="create a bare repository")
    t.add_argument("root")
    t.add_argument("name")
    t.add_argument("--readers", default="*", help="comma separated users or '*' ('' means nobody)")
    t.add_argument("--writers", default="*", help="comma separated users or '*' ('' means nobody)")
    t.add_argument("--public", action="store_true", help="allow anonymous reads")
    t.add_argument("--allow-force", dest="allow_force", action="store_true", help="allow forced pushes")
    t = ss.add_parser("perm", help="show or set the access of users to a repository")
    t.add_argument("root")
    t.add_argument("repo")
    t.add_argument("users", help="comma separated users or '*'")
    t.add_argument("level", nargs="?", choices=("none", "read", "write"),
                   help="new access level; omit it to show the current one")
    t = ss.add_parser("set-repo", help="change the public / allow-force flags of a repository")
    t.add_argument("root")
    t.add_argument("repo")
    g = t.add_mutually_exclusive_group()
    g.add_argument("--public", dest="public", action="store_const", const=True, default=None,
                   help="allow anonymous reads")
    g.add_argument("--private", dest="public", action="store_const", const=False,
                   help="require a login to read")
    g = t.add_mutually_exclusive_group()
    g.add_argument("--allow-force", dest="allow_force", action="store_const", const=True, default=None,
                   help="allow forced pushes")
    g.add_argument("--no-allow-force", dest="allow_force", action="store_const", const=False,
                   help="refuse forced pushes")
    t = ss.add_parser("list", help="list users (with their token names) and repositories")
    t.add_argument("root")


def _iso(stamp) -> str:
    if stamp is None:
        return "-"
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
    except (OverflowError, OSError, ValueError):
        return "-"


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def cmd_server(ns, cwd):
    from minigit import cli

    auth, admin = cli._m("auth"), cli._m("admin")  # via _m: tests patch sys.modules

    if not ns.server_cmd:
        raise cli._usage(f"server: a subcommand is required ({', '.join(SUBCOMMANDS)})")
    root = cli._abs(cwd, ns.root)
    auth.server_init(root)
    sub = ns.server_cmd
    if sub == "init":
        cli._say(f"Initialized minigit server root in {root}")
    elif sub == "add-user":
        token = auth.add_user(root, ns.name, reset=ns.reset)
        cli._say(token)
        cli._stderr(f"note: this is the only time the token for '{ns.name}' is shown; "
                    f"it cannot be recovered (issue another with 'minigit server add-token')")
    elif sub == "remove-user":
        touched = admin.remove_user(root, ns.name)
        cli._say(f"Removed user '{ns.name}'")
        if touched:
            cli._say(f"Removed '{ns.name}' from the access lists of: {', '.join(touched)}")
    elif sub == "add-token":
        cli._say(admin.add_token(root, ns.user, ns.name))
    elif sub == "list-tokens":
        cli._say("\n".join(f"{n}\t{_iso(c)}" for n, c in admin.list_tokens(root, ns.user)))
    elif sub == "revoke-token":
        if ns.all_tokens == (ns.name is not None):
            raise cli._usage("revoke-token: give either a token name or --all")
        if ns.all_tokens:
            count = admin.revoke_token(root, ns.user, everything=True)
            cli._say(f"Revoked {count} token(s) of user '{ns.user}'")
        else:
            admin.revoke_token(root, ns.user, ns.name)
            cli._say(f"Revoked token '{ns.name}' of user '{ns.user}'")
    elif sub == "create-repo":
        auth.create_repo(root, ns.name, readers=cli._split_users(ns.readers),
                         writers=cli._split_users(ns.writers), public=ns.public,
                         allow_force=ns.allow_force)
        cli._say("/" + ns.name)
        cli._stderr(f"Created repository '{ns.name}' (clone with: minigit clone http://HOST:PORT/{ns.name})")
    elif sub == "perm":
        names = admin.parse_names(ns.users)
        if not names:
            raise cli._usage("perm: name at least one user (or '*')")
        if ns.level is None:
            cli._say("\n".join(admin.get_perm(root, ns.repo, names)))
        else:
            results = admin.set_perm(root, ns.repo, names, ns.level)
            cli._say("\n".join(line for line, _ in results))
            for _, warning in results:
                if warning:
                    cli._stderr(warning)
    elif sub == "set-repo":
        if ns.public is None and ns.allow_force is None:
            raise cli._usage("set-repo: give at least one of --public, --private, --allow-force, "
                             "--no-allow-force")
        public, force = admin.set_repo(root, ns.repo, public=ns.public, allow_force=ns.allow_force)
        cli._say(f"{ns.repo}: public={_yes(public)} allow-force={_yes(force)}")
    elif sub == "list":
        cli._say(auth.list_server(root))
    return 0
