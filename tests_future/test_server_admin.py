"""Acceptance tests for the "server-admin" feature (design: features/server-admin.md).

The feature extends ``minigit server`` with ``remove-user``, ``add-token``,
``list-tokens``, ``revoke-token``, ``perm`` and ``set-repo``, keeps several named
tokens per user in ``server.json``, and makes ``create-repo --readers ""`` an
explicit empty list instead of ``*``.

Everything here drives the REAL CLI (``python -m minigit``) as a subprocess in
temporary directories; the live tests also start ``minigit serve`` on a free
localhost port and always stop it before the temporary directory goes. Nothing
imports the library or ``tests/helpers.py``.

Both classes are skipped (``unittest.skip("feature not implemented yet")``) until
the feature is implemented. While implementing, set ``MINIGIT_RUN_FUTURE=1``
(PowerShell: ``$env:MINIGIT_RUN_FUTURE = "1"``) and run from E:\\ideas\\minigit::

    python -m unittest tests_future.test_server_admin -v

Once everything passes, move the file to ``tests_blackbox/`` and replace ``SKIP``
with a no-op (or delete the two ``@SKIP`` lines).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLI_TIMEOUT = 60
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
REPO_KEYS = {"readers", "writers", "public", "allow_force"}
SKIP_REASON = "feature not implemented yet"
if os.environ.get("MINIGIT_RUN_FUTURE") == "1":
    def SKIP(cls):  # run the tests while the feature is being implemented
        return cls
else:
    SKIP = unittest.skip(SKIP_REASON)


# --------------------------------------------------------------------------
# small self-contained helpers
# --------------------------------------------------------------------------

@dataclass
class Result:
    args: list
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def __str__(self) -> str:  # diagnostics only
        return (f"minigit {' '.join(self.args)} -> exit {self.returncode}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def _with_pythonpath(env: dict) -> dict:
    full = dict(env)
    prev = full.get("PYTHONPATH")
    full["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    full.setdefault("PYTHONIOENCODING", "utf-8")
    return full


def run(args, cwd, env, check=True) -> Result:
    args = [str(a) for a in args]
    proc = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd, env=_with_pythonpath(env),
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=CLI_TIMEOUT)
    res = Result(args, proc.returncode, proc.stdout, proc.stderr)
    if check and res.returncode != 0:
        raise AssertionError(f"command failed:\n{res}")
    return res


def make_env(tmp: str, user: str | None = None, token: str | None = None) -> dict:
    env = dict(os.environ)
    home = os.path.join(os.path.abspath(tmp), "home")
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["MINIGIT_AUTHOR_NAME"] = user or "Admin"
    env["MINIGIT_AUTHOR_EMAIL"] = f"{user or 'admin'}@example.com"
    env["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
    for key in ("MINIGIT_USER", "MINIGIT_TOKEN", "MINIGIT_DEBUG"):
        env.pop(key, None)
    if user is not None and token is not None:
        env["MINIGIT_USER"] = user
        env["MINIGIT_TOKEN"] = token
    return env


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class _AdminCase(unittest.TestCase):
    """A fresh server root per test plus helpers for ``minigit server <sub> ROOT ...``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)  # registered first, so it runs last
        self.dir = os.path.realpath(tmp.name)
        self.env = make_env(os.path.join(self.dir, "admin"))
        self.srv = os.path.join(self.dir, "srv")
        run(["server", "init", self.srv], cwd=self.dir, env=self.env)

    def adm(self, sub: str, *args, check: bool = True) -> Result:
        return run(["server", sub, self.srv, *args], cwd=self.dir, env=self.env, check=check)

    def fails(self, code: int, sub: str, *args) -> Result:
        res = self.adm(sub, *args, check=False)
        self.assertEqual(res.returncode, code, str(res))
        self.assertNotIn("internal error", res.err, str(res))
        if code == 1:
            self.assertIn("error:", res.err, str(res))
        return res

    def server_json_text(self) -> str:
        return read_bytes(os.path.join(self.srv, "server.json")).decode("utf-8")

    def server_json(self) -> dict:
        return json.loads(self.server_json_text())

    def user(self, name: str) -> dict:
        return self.server_json()["users"][name]

    def repo(self, name: str) -> dict:
        return self.server_json()["repos"][name]

    def add_user(self, name: str) -> str:
        res = self.adm("add-user", name)
        token = res.out.strip()
        self.assertRegex(token, TOKEN_RE)
        return token

    def add_token(self, user: str, name: str) -> str:
        res = self.adm("add-token", user, name)
        self.assertEqual(res.stderr, b"", str(res))
        lines = res.stdout.split(b"\n")
        self.assertEqual(len(lines), 2, f"the token must be alone on one line: {res.stdout!r}")
        self.assertEqual(lines[1], b"")
        token = lines[0].decode("ascii")
        self.assertRegex(token, TOKEN_RE)
        self.assertFalse(token.startswith("-"), "a leading '-' would break 'login --token <token>'")
        return token

    def create_repo(self, name: str, *flags) -> None:
        res = self.adm("create-repo", name, *flags)
        self.assertEqual(res.out.strip(), "/" + name)


# ==========================================================================
# offline admin CLI behaviour (no server needed)
# ==========================================================================

@SKIP
class ServerAdminCliTests(_AdminCase):

    # -- tokens ------------------------------------------------------------
    def test_add_token_prints_it_once_and_stores_only_its_hash(self):
        default = self.add_user("alice")
        ci = self.add_token("alice", "ci")
        self.assertNotEqual(ci, default)
        raw = self.server_json_text()
        self.assertNotIn(ci, raw, "the plain token must never be written to server.json")
        rec = self.user("alice")
        self.assertEqual(rec["token_sha256"], sha256(default), "the default token is untouched")
        self.assertIsInstance(rec["token_created"], int)
        self.assertEqual(set(rec["tokens"]), {"ci"})
        self.assertEqual(rec["tokens"]["ci"]["sha256"], sha256(ci))
        self.assertIsInstance(rec["tokens"]["ci"]["created"], int)
        self.assertLessEqual(abs(rec["tokens"]["ci"]["created"] - int(time.time())), 600)
        # duplicates are refused and change nothing
        before = self.server_json()
        dup = self.fails(1, "add-token", "alice", "ci")
        self.assertIn("already exists", dup.err)
        dup = self.fails(1, "add-token", "alice", "default")
        self.assertIn("already exists", dup.err)
        self.assertEqual(self.server_json(), before)

    def test_token_names_and_owner_are_validated(self):
        self.add_user("alice")
        before = self.server_json()
        for bad in ["", "a b", "a/b", "a:b", "x" * 65, "\u00e9t\u00e9"]:
            with self.subTest(name=bad):
                res = self.fails(1, "add-token", "alice", bad)
                self.assertIn("invalid token name", res.err)
        res = self.fails(1, "add-token", "bob", "ci")
        self.assertIn("user 'bob' does not exist", res.err)
        self.assertEqual(self.server_json(), before, "a refused add-token must not create anything")
        self.assertNotIn("bob", self.server_json()["users"])

    def test_list_tokens_shows_names_and_times_but_never_secrets(self):
        default = self.add_user("alice")
        laptop = self.add_token("alice", "laptop")
        ci = self.add_token("alice", "ci")
        res = self.adm("list-tokens", "alice")
        self.assertEqual(res.stderr, b"", str(res))
        lines = res.out.splitlines()
        names = [line.split("\t")[0] for line in lines]
        self.assertEqual(names, ["default", "ci", "laptop"], "default first, then by name")
        for line in lines:
            name, created = line.split("\t")
            self.assertRegex(created, ISO_RE, line)
        for secret in (default, laptop, ci, sha256(default), sha256(ci)):
            self.assertNotIn(secret, res.out)
        listing = self.adm("list").out
        alice_line = [line for line in listing.splitlines() if line.strip().startswith("alice")]
        self.assertEqual(len(alice_line), 1, listing)
        self.assertIn("tokens=default,ci,laptop", alice_line[0])
        for secret in (default, laptop, ci, sha256(default), sha256(laptop), sha256(ci)):
            self.assertNotIn(secret, listing)
        res = self.fails(1, "list-tokens", "nobody")
        self.assertIn("does not exist", res.err)

    def test_revoke_one_token_then_the_default_then_all(self):
        self.add_user("alice")
        self.add_token("alice", "ci")
        laptop = self.add_token("alice", "laptop")
        res = self.adm("revoke-token", "alice", "ci")
        self.assertEqual(res.out, "Revoked token 'ci' of user 'alice'\n")
        rec = self.user("alice")
        self.assertEqual(set(rec["tokens"]), {"laptop"})
        self.assertEqual(rec["tokens"]["laptop"]["sha256"], sha256(laptop))
        self.assertIn("token_sha256", rec)
        res = self.fails(1, "revoke-token", "alice", "ci")
        self.assertIn("no token named 'ci'", res.err)
        # the default token can be revoked like any other
        res = self.adm("revoke-token", "alice", "default")
        self.assertEqual(res.out, "Revoked token 'default' of user 'alice'\n")
        rec = self.user("alice")
        self.assertNotIn("token_sha256", rec)
        self.assertNotIn("token_created", rec)
        self.assertEqual([line.split("\t")[0] for line in self.adm("list-tokens", "alice").out.splitlines()],
                         ["laptop"])
        # --all removes whatever is left; the user stays (and cannot log in)
        res = self.adm("revoke-token", "alice", "--all")
        self.assertEqual(res.out, "Revoked 1 token(s) of user 'alice'\n")
        rec = self.user("alice")
        self.assertNotIn("token_sha256", rec)
        self.assertNotIn("tokens", rec, "an empty 'tokens' object is dropped")
        self.assertEqual(self.adm("list-tokens", "alice").out, "")
        self.assertIn("tokens=-", self.adm("list").out)
        self.assertEqual(self.adm("revoke-token", "alice", "--all").out, "Revoked 0 token(s) of user 'alice'\n")
        # a new default token can be issued again
        again = self.add_token("alice", "default")
        self.assertEqual(self.user("alice")["token_sha256"], sha256(again))

    def test_revoke_token_usage_and_unknown_user(self):
        self.add_user("alice")
        self.add_token("alice", "ci")
        before = self.server_json()
        self.fails(2, "revoke-token", "alice")
        self.fails(2, "revoke-token", "alice", "ci", "--all")
        res = self.fails(1, "revoke-token", "bob", "ci")
        self.assertIn("user 'bob' does not exist", res.err)
        self.assertEqual(self.server_json(), before)

    def test_add_user_reset_replaces_only_the_default_token(self):
        first = self.add_user("alice")
        ci = self.add_token("alice", "ci")
        res = self.adm("add-user", "alice", "--reset")
        second = res.out.strip()
        self.assertRegex(second, TOKEN_RE)
        self.assertNotEqual(second, first)
        rec = self.user("alice")
        self.assertEqual(rec["token_sha256"], sha256(second))
        self.assertEqual(rec["tokens"]["ci"]["sha256"], sha256(ci), "--reset must keep named tokens")

    def test_legacy_user_record_keeps_working(self):
        """A server.json written before this feature (token_sha256 only) needs no migration."""
        legacy = "legacy-token-abcdefghijklmnop"
        data = {"version": 1, "users": {"alice": {"token_sha256": sha256(legacy)}},
                "repos": {}}
        write_bytes(os.path.join(self.srv, "server.json"), json.dumps(data).encode("utf-8"))
        self.assertEqual(self.adm("list-tokens", "alice").out, "default\t-\n")
        self.add_token("alice", "ci")
        rec = self.user("alice")
        self.assertEqual(rec["token_sha256"], sha256(legacy))
        self.assertEqual(set(rec["tokens"]), {"ci"})
        self.assertIn("tokens=default,ci", self.adm("list").out)

    def test_concurrent_admin_commands_lose_no_update(self):
        self.add_user("alice")
        results: dict[int, Result] = {}

        def worker(i: int) -> None:
            results[i] = self.adm("add-token", "alice", f"t{i}", check=False)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(CLI_TIMEOUT + 10)
        self.assertEqual(sorted(results), list(range(8)))
        for i, res in results.items():
            self.assertEqual(res.returncode, 0, str(res))
        tokens = self.user("alice")["tokens"]
        self.assertEqual(set(tokens), {f"t{i}" for i in range(8)}, "every concurrent update must survive")
        for i, res in results.items():
            self.assertEqual(tokens[f"t{i}"]["sha256"], sha256(res.out.strip()))
        self.assertFalse(os.path.exists(os.path.join(self.srv, "server.json.lock")))

    # -- users -------------------------------------------------------------
    def test_remove_user_drops_tokens_and_scrubs_every_acl(self):
        self.add_user("alice")
        self.add_token("alice", "ci")
        self.add_user("bob")
        self.add_user("carol")
        self.create_repo("proj", "--readers", "alice,bob", "--writers", "alice")
        self.create_repo("team", "--readers", "*", "--writers", "alice")
        self.create_repo("other", "--readers", "bob", "--writers", "bob")
        res = self.adm("remove-user", "alice")
        self.assertEqual(res.out, "Removed user 'alice'\nRemoved 'alice' from the access lists of: proj, team\n")
        data = self.server_json()
        self.assertNotIn("alice", data["users"])
        self.assertEqual(set(data["users"]), {"bob", "carol"})
        self.assertEqual((data["repos"]["proj"]["readers"], data["repos"]["proj"]["writers"]), (["bob"], []))
        self.assertEqual((data["repos"]["team"]["readers"], data["repos"]["team"]["writers"]), (["*"], []))
        self.assertEqual((data["repos"]["other"]["readers"], data["repos"]["other"]["writers"]), (["bob"], ["bob"]))
        for name in ("proj", "team", "other"):
            self.assertEqual(set(data["repos"][name]), REPO_KEYS)
        res = self.fails(1, "remove-user", "alice")
        self.assertIn("user 'alice' does not exist", res.err)
        self.assertEqual(self.adm("remove-user", "carol").out, "Removed user 'carol'\n")
        res = self.adm("remove-user", "bob")
        self.assertEqual(res.out, "Removed user 'bob'\nRemoved 'bob' from the access lists of: other, proj\n")
        # a user re-created with the same name does not inherit old permissions
        self.add_user("alice")
        self.assertEqual(self.adm("perm", "proj", "alice").out, "proj: alice = none\n")
        self.assertEqual(self.adm("perm", "team", "alice").out, "team: alice = read\n")  # through '*'
        self.assertNotIn("tokens", self.user("alice"))

    # -- permissions ---------------------------------------------------------
    def test_perm_sets_exact_levels_and_keeps_each_user_in_one_list(self):
        self.add_user("alice")
        self.add_user("bob")
        self.create_repo("proj", "--readers", "bob", "--writers", "bob")
        res = self.adm("perm", "proj", "alice", "write")
        self.assertEqual((res.out, res.stderr), ("proj: alice = write\n", b""))
        self.assertEqual((self.repo("proj")["readers"], self.repo("proj")["writers"]), (["bob"], ["bob", "alice"]))
        res = self.adm("perm", "proj", "alice", "read")
        self.assertEqual(res.out, "proj: alice = read\n")
        self.assertEqual((self.repo("proj")["readers"], self.repo("proj")["writers"]), (["bob", "alice"], ["bob"]))
        res = self.adm("perm", "proj", "alice", "none")
        self.assertEqual(res.out, "proj: alice = none\n")
        self.assertEqual((self.repo("proj")["readers"], self.repo("proj")["writers"]), (["bob"], ["bob"]))
        # the query form changes nothing
        before = self.server_json()
        self.assertEqual(self.adm("perm", "proj", "alice").out, "proj: alice = none\n")
        self.assertEqual(self.adm("perm", "proj", "bob").out, "proj: bob = write\n")
        self.assertEqual(self.adm("perm", "proj", "alice,bob").out, "proj: alice = none\nproj: bob = write\n")
        self.assertEqual(self.server_json(), before)
        # setting a level normalizes the touched user only, and is idempotent
        for _ in range(2):
            res = self.adm("perm", "proj", "bob", "read")
            self.assertEqual(res.out, "proj: bob = read\n")
            self.assertEqual((self.repo("proj")["readers"], self.repo("proj")["writers"]), (["bob"], []))
        self.assertEqual(set(self.repo("proj")), REPO_KEYS)
        self.assertEqual((self.repo("proj")["public"], self.repo("proj")["allow_force"]), (False, False))

    def test_perm_takes_lists_and_star_and_warns_when_access_remains(self):
        for name in ("alice", "bob", "carol"):
            self.add_user(name)
        self.create_repo("proj", "--readers", "carol", "--writers", "carol")
        res = self.adm("perm", "proj", "alice,bob,alice", "write")
        self.assertEqual(res.out, "proj: alice = write\nproj: bob = write\n")
        self.assertEqual(self.repo("proj")["writers"], ["carol", "alice", "bob"])
        res = self.adm("perm", "proj", "*", "read")
        self.assertEqual((res.out, res.stderr), ("proj: * = read\n", b""))
        self.assertIn("*", self.repo("proj")["readers"])
        # alice keeps read access through '*': the effective level is printed, with a warning
        res = self.adm("perm", "proj", "alice", "none")
        self.assertEqual(res.out, "proj: alice = read\n")
        self.assertIn("warning:", res.err)
        self.assertIn("still has read access", res.err)
        self.assertIn("'*'", res.err)
        self.assertNotIn("alice", self.repo("proj")["readers"] + self.repo("proj")["writers"])
        res = self.adm("perm", "proj", "*", "write")
        self.assertEqual(res.out, "proj: * = write\n")
        self.assertEqual((self.repo("proj")["readers"].count("*"), self.repo("proj")["writers"].count("*")), (0, 1))
        res = self.adm("perm", "proj", "bob", "read")
        self.assertEqual(res.out, "proj: bob = write\n")
        self.assertIn("still has write access", res.err)
        self.assertEqual(self.adm("perm", "proj", "*").out, "proj: * = write\n")
        res = self.adm("perm", "proj", "*", "none")
        self.assertEqual(res.out, "proj: * = none\n")
        rec = self.repo("proj")
        self.assertNotIn("*", rec["readers"] + rec["writers"])
        self.assertEqual(self.adm("perm", "proj", "alice").out, "proj: alice = none\n")

    def test_perm_on_a_public_repo_warns_that_reads_stay_open(self):
        self.add_user("alice")
        self.create_repo("pub", "--readers", "alice", "--writers", "alice", "--public")
        res = self.adm("perm", "pub", "alice", "none")
        self.assertEqual(res.out, "pub: alice = read\n")
        self.assertIn("warning:", res.err)
        self.assertIn("public", res.err)
        self.assertEqual((self.repo("pub")["readers"], self.repo("pub")["writers"]), ([], []))

    def test_perm_errors_change_nothing(self):
        self.add_user("alice")
        self.create_repo("proj", "--readers", "alice", "--writers", "alice")
        before = self.server_json()
        res = self.fails(1, "perm", "nosuch", "alice", "read")
        self.assertIn("repository 'nosuch' does not exist", res.err)
        res = self.fails(1, "perm", "proj", "ghost", "read")
        self.assertIn("user 'ghost' does not exist", res.err)
        res = self.fails(1, "perm", "proj", "alice,ghost", "write")
        self.assertIn("user 'ghost' does not exist", res.err)
        res = self.fails(1, "perm", "proj", "ghost")
        self.assertIn("user 'ghost' does not exist", res.err)
        res = self.fails(1, "perm", "proj", "bad name", "read")
        self.assertIn("invalid user name", res.err)
        self.fails(2, "perm", "proj", "alice", "admin")
        self.fails(2, "perm", "proj", "", "read")
        self.fails(2, "perm", "proj", " , ", "read")
        self.assertEqual(self.server_json(), before, "a refused perm must not touch server.json")

    def test_perm_none_cleans_up_a_name_that_is_not_a_user(self):
        self.add_user("alice")
        self.create_repo("stale", "--readers", "ghost,alice", "--writers", "ghost")
        res = self.adm("perm", "stale", "ghost", "none")
        self.assertEqual(res.out, "stale: ghost = none\n")
        self.assertEqual((self.repo("stale")["readers"], self.repo("stale")["writers"]), (["alice"], []))

    # -- repository options -------------------------------------------------------
    def test_set_repo_toggles_public_and_allow_force(self):
        self.create_repo("proj", "--readers", "*", "--writers", "*")
        res = self.adm("set-repo", "proj", "--public")
        self.assertEqual(res.out, "proj: public=yes allow-force=no\n")
        self.assertEqual(self.adm("set-repo", "proj", "--allow-force").out, "proj: public=yes allow-force=yes\n")
        rec = self.repo("proj")
        self.assertEqual((rec["public"], rec["allow_force"]), (True, True))
        proj_line = [line for line in self.adm("list").out.splitlines() if line.strip().startswith("proj")][0]
        self.assertIn("public", proj_line)
        self.assertIn("allow-force", proj_line)
        res = self.adm("set-repo", "proj", "--private", "--no-allow-force")
        self.assertEqual(res.out, "proj: public=no allow-force=no\n")
        rec = self.repo("proj")
        self.assertEqual(rec, {"readers": ["*"], "writers": ["*"], "public": False, "allow_force": False})
        before = self.server_json()
        self.fails(2, "set-repo", "proj")
        self.fails(2, "set-repo", "proj", "--public", "--private")
        self.fails(2, "set-repo", "proj", "--allow-force", "--no-allow-force")
        res = self.fails(1, "set-repo", "nosuch", "--public")
        self.assertIn("repository 'nosuch' does not exist", res.err)
        self.assertEqual(self.server_json(), before)

    def test_create_repo_with_an_empty_acl_is_explicitly_closed(self):
        self.add_user("alice")
        self.create_repo("locked", "--readers", "", "--writers", "")
        self.assertEqual(self.repo("locked"), {"readers": [], "writers": [], "public": False, "allow_force": False})
        self.create_repo("open")
        self.assertEqual((self.repo("open")["readers"], self.repo("open")["writers"]), (["*"], ["*"]))
        self.assertEqual(self.adm("perm", "locked", "alice").out, "locked: alice = none\n")
        self.assertEqual(self.adm("perm", "open", "alice").out, "open: alice = write\n")

    def test_help_lists_the_new_subcommands(self):
        res = run(["server", "--help"], cwd=self.dir, env=self.env)
        for sub in ("remove-user", "add-token", "list-tokens", "revoke-token", "perm", "set-repo"):
            self.assertIn(sub, res.out + res.err)
        # the "subcommand is required" usage error names them too
        res = run(["server"], cwd=self.dir, env=self.env, check=False)
        self.assertEqual(res.returncode, 2, str(res))
        for sub in ("remove-user", "add-token", "list-tokens", "revoke-token", "perm", "set-repo"):
            self.assertIn(sub, res.err)

    def test_missing_arguments_are_usage_errors(self):
        self.add_user("alice")
        self.create_repo("proj")
        before = self.server_json()
        for args in (["remove-user"], ["add-token", "alice"], ["list-tokens"], ["revoke-token"],
                     ["perm", "proj"], ["perm"], ["set-repo"], ["set-repo", "proj", "--bogus"]):
            with self.subTest(args=args):
                self.fails(2, *args)
        self.assertEqual(self.server_json(), before)

    def test_bad_names_are_refused_before_anything_changes(self):
        self.add_user("alice")
        self.create_repo("proj", "--readers", "alice", "--writers", "alice")
        before = self.server_json()
        cases = [
            (["remove-user", "bad name"], "invalid user name"),
            (["list-tokens", "a/b"], "invalid user name"),
            (["add-token", "a b", "ci"], "invalid user name"),
            (["revoke-token", "alice", "a b"], "invalid token name"),
            (["revoke-token", "a:b", "--all"], "invalid user name"),
            (["perm", "../proj", "alice", "read"], "invalid repository name"),
            (["perm", "a..b", "alice"], "invalid repository name"),
            (["set-repo", ".hidden", "--public"], "invalid repository name"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                res = self.fails(1, *args)
                self.assertIn(message, res.err)
        self.assertEqual(self.server_json(), before)
        self.assertEqual(sorted(os.listdir(os.path.join(self.srv, "repos"))), ["proj"])

    def test_admin_commands_keep_unknown_keys(self):
        """Fields written by other (future) features survive every admin command."""
        token = "legacy-token-abcdefghijklmnop"
        data = {"version": 1,
                "users": {"alice": {"token_sha256": sha256(token), "email": "alice@example.com"}},
                "repos": {"proj": {"readers": ["alice"], "writers": [], "public": False, "allow_force": False,
                                   "description": "kept"}},
                "extra": {"kept": True}}
        write_bytes(os.path.join(self.srv, "server.json"), json.dumps(data).encode("utf-8"))
        os.makedirs(os.path.join(self.srv, "repos", "proj"), exist_ok=True)
        self.add_token("alice", "ci")
        self.adm("add-user", "alice", "--reset")
        self.adm("revoke-token", "alice", "--all")
        self.adm("perm", "proj", "alice", "write")
        self.adm("set-repo", "proj", "--public")
        after = self.server_json()
        self.assertEqual(after["users"]["alice"], {"email": "alice@example.com"})
        self.assertEqual(after["repos"]["proj"]["description"], "kept")
        self.assertEqual(after["repos"]["proj"]["writers"], ["alice"])
        self.assertEqual(after["extra"], {"kept": True})
        self.assertEqual(after["version"], 1)


# ==========================================================================
# the changes reach a running server on the very next request
# ==========================================================================

class _LiveServer:
    """``minigit serve --port 0 --ready-file F --quiet``; always stopped by ``stop``."""

    def __init__(self, root: str, env: dict) -> None:
        self.root = root
        self.env = env
        parent = os.path.dirname(root)
        self.ready = os.path.join(parent, "ready.txt")
        self.log_path = os.path.join(parent, "serve.log")
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self._log = None

    def start(self) -> "_LiveServer":
        self._log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "minigit", "serve", "--root", self.root, "--host", "127.0.0.1",
             "--port", "0", "--ready-file", self.ready, "--quiet"],
            cwd=os.path.dirname(self.root), env=_with_pythonpath(self.env),
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early: {read_bytes(self.log_path)!r}")
            try:
                line = read_bytes(self.ready).decode("ascii").strip()
            except OSError:
                line = ""
            if line:
                self.url = "http://" + line
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not become ready within 15 s")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        if self._log is not None:
            self._log.close()
            self._log = None


@SKIP
class ServerAdminLiveTests(_AdminCase):

    def setUp(self):
        super().setUp()
        self.server = _LiveServer(self.srv, self.env)
        self.addCleanup(self.server.stop)  # LIFO: runs before the temp dir cleanup
        self.server.start()

    def status(self, path: str, user: str | None = None, token: str | None = None) -> int:
        req = urllib.request.Request(self.server.url + path, method="GET")
        req.add_header("X-Minigit-Protocol", "1")
        if user is not None:
            raw = f"{user}:{token}".encode("utf-8")
            req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as e:
            e.read()
            return e.code

    def root_listing(self, user: str | None = None, token: str | None = None) -> list:
        req = urllib.request.Request(self.server.url + "/", method="GET")
        req.add_header("X-Minigit-Protocol", "1")
        if user is not None:
            raw = f"{user}:{token}".encode("utf-8")
            req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["repos"]

    def client(self, user: str, token: str) -> tuple[str, dict]:
        work = os.path.join(self.dir, "work-" + user)
        os.makedirs(work, exist_ok=True)
        return work, make_env(os.path.join(self.dir, "env-" + user), user=user, token=token)

    def commit(self, wt: str, env: dict, name: str, data: bytes, msg: str) -> None:
        write_bytes(os.path.join(wt, name), data)
        run(["add", name], cwd=wt, env=env)
        run(["commit", "-m", msg], cwd=wt, env=env)

    def test_token_and_user_changes_apply_to_the_running_server(self):
        default = self.add_user("alice")
        ci = self.add_token("alice", "ci")
        self.create_repo("proj", "--readers", "alice", "--writers", "alice")
        self.assertEqual(self.status("/proj/info/refs", "alice", default), 200)
        self.assertEqual(self.status("/proj/info/refs", "alice", ci), 200, "a named token logs in too")
        self.assertEqual(self.status("/proj/info/refs", "alice", ci + "x"), 401)
        self.assertEqual(self.status("/proj/info/refs", "alice", "default"), 401, "the token NAME is not a token")
        self.adm("revoke-token", "alice", "ci")
        self.assertEqual(self.status("/proj/info/refs", "alice", ci), 401, "a revoked token fails at once")
        self.assertEqual(self.status("/proj/info/refs", "alice", default), 200)
        fresh = self.adm("add-user", "alice", "--reset").out.strip()
        self.assertEqual(self.status("/proj/info/refs", "alice", default), 401)
        self.assertEqual(self.status("/proj/info/refs", "alice", fresh), 200)
        laptop = self.add_token("alice", "laptop")
        self.adm("revoke-token", "alice", "--all")
        self.assertEqual(self.status("/proj/info/refs", "alice", fresh), 401)
        self.assertEqual(self.status("/proj/info/refs", "alice", laptop), 401)
        again = self.add_token("alice", "default")
        self.assertEqual(self.status("/proj/info/refs", "alice", again), 200)
        self.adm("remove-user", "alice")
        self.assertEqual(self.status("/proj/info/refs", "alice", again), 401, "a removed user cannot log in")
        # re-created with the same name: authenticates, but the scrubbed ACL grants nothing
        newest = self.add_user("alice")
        self.assertEqual(self.status("/proj/info/refs", "alice", newest), 403)

    def test_permission_changes_apply_to_clone_fetch_and_push(self):
        a_tok = self.add_user("alice")
        b_tok = self.add_user("bob")
        self.create_repo("proj", "--readers", "alice", "--writers", "alice")
        url = f"{self.server.url}/proj"
        a_work, a_env = self.client("alice", a_tok)
        run(["clone", url, "a"], cwd=a_work, env=a_env)
        a_wt = os.path.join(a_work, "a")
        self.commit(a_wt, a_env, "f.txt", b"one\n", "first")
        run(["push", "-u", "origin", "main"], cwd=a_wt, env=a_env)

        b_work, b_env = self.client("bob", b_tok)
        res = run(["clone", url, "b0"], cwd=b_work, env=b_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("permission denied", res.err)
        self.assertEqual(self.status("/proj/info/refs", "bob", b_tok), 403)
        self.assertNotIn("proj", self.root_listing("bob", b_tok))

        self.assertEqual(self.adm("perm", "proj", "bob", "read").out, "proj: bob = read\n")
        self.assertIn("proj", self.root_listing("bob", b_tok))
        run(["clone", url, "b"], cwd=b_work, env=b_env)
        b_wt = os.path.join(b_work, "b")
        self.assertEqual(read_bytes(os.path.join(b_wt, "f.txt")), b"one\n")
        self.commit(b_wt, b_env, "g.txt", b"two\n", "second")
        res = run(["push", "origin", "main"], cwd=b_wt, env=b_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("permission denied", res.err)

        self.assertEqual(self.adm("perm", "proj", "bob", "write").out, "proj: bob = write\n")
        run(["push", "origin", "main"], cwd=b_wt, env=b_env)
        run(["pull"], cwd=a_wt, env=a_env)
        self.assertEqual(read_bytes(os.path.join(a_wt, "g.txt")), b"two\n")

        self.assertEqual(self.adm("perm", "proj", "bob", "none").out, "proj: bob = none\n")
        res = run(["fetch"], cwd=b_wt, env=b_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("permission denied", res.err)
        # alice's own access was never touched
        self.assertEqual(self.status("/proj/info/refs", "alice", a_tok), 200)

    def test_public_toggle_controls_anonymous_reads(self):
        tok = self.add_user("alice")
        self.create_repo("proj", "--readers", "*", "--writers", "*")
        self.assertEqual(self.status("/proj/info/refs"), 401)
        self.assertEqual(self.root_listing(), [])
        self.adm("set-repo", "proj", "--public")
        self.assertEqual(self.status("/proj/info/refs"), 200)
        self.assertEqual(self.root_listing(), ["proj"])
        self.adm("set-repo", "proj", "--private")
        self.assertEqual(self.status("/proj/info/refs"), 401)
        self.assertEqual(self.status("/proj/info/refs", "alice", tok), 200)

    def test_empty_acl_repo_refuses_every_user(self):
        tok = self.add_user("alice")
        self.create_repo("locked", "--readers", "", "--writers", "")
        self.assertEqual(self.status("/locked/info/refs", "alice", tok), 403)
        self.adm("perm", "locked", "alice", "read")
        self.assertEqual(self.status("/locked/info/refs", "alice", tok), 200)

    def test_malformed_token_records_never_authenticate_and_never_crash(self):
        """Hand-edited server.json: bad hashes are ignored (401), never a 500."""
        good = self.add_user("bob")
        self.create_repo("proj", "--readers", "*", "--writers", "*")
        path = os.path.join(self.srv, "server.json")
        data = self.server_json()
        data["users"]["alice"] = {"token_sha256": "é" * 64, "token_created": "yesterday",
                                  "tokens": {"ci": {"sha256": 5}, "x": "not-an-object",
                                             "ok": {"sha256": sha256("named-token-0123456789")}}}
        data["users"]["carol"] = {"tokens": []}
        write_bytes(path, json.dumps(data).encode("utf-8"))
        for user, token in (("alice", "é" * 64), ("alice", "anything"), ("carol", "x"), ("alice", "")):
            with self.subTest(user=user, token=token):
                self.assertEqual(self.status("/proj/info/refs", user, token), 401)
        self.assertEqual(self.status("/proj/info/refs", "alice", "named-token-0123456789"), 200,
                         "a well-formed named token next to broken ones still works")
        self.assertEqual(self.status("/proj/info/refs", "bob", good), 200)
        # the admin CLI shows only the usable tokens and does not crash either
        self.assertEqual(self.adm("list-tokens", "alice").out, "ok\t-\n")
        self.assertEqual(self.adm("list-tokens", "carol").out, "")


if __name__ == "__main__":
    unittest.main()
