"""End-to-end tests for collaboration through a live server (owner: M7 cli).

One ``minigit serve`` process (started with ``ServerProcess``) serves the
whole class; every test creates its own repository on it. Alice, bob and
carol (read-only) drive the real CLI from their own clones.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import unittest

from tests.helpers import ServerProcess, make_env, read, run, write

HEX7 = r"[0-9a-f]{7}"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class MultiUserTests(unittest.TestCase):
    server: ServerProcess
    tokens: dict[str, str]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = os.path.realpath(cls.tmp.name)
        cls.admin_env = make_env(cls.root)
        cls.srv_root = os.path.join(cls.root, "srv")
        run(["server", "init", cls.srv_root], cwd=cls.root, env=cls.admin_env)
        cls.tokens = {}
        for user in ("alice", "bob", "carol"):
            res = run(["server", "add-user", cls.srv_root, user], cwd=cls.root, env=cls.admin_env)
            token = res.out.strip()
            assert token and "\n" not in token, res
            cls.tokens[user] = token
        cls.server = ServerProcess(cls.srv_root, env=cls.admin_env)
        try:
            cls.server.start()
        except Exception:
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def setUp(self):
        self.name = re.sub(r"[^A-Za-z0-9_]", "_", self._testMethodName)[:40]
        self.work = os.path.join(self.root, self.name)
        os.makedirs(self.work, exist_ok=True)
        self.counter = 0

    def env_for(self, user):
        return make_env(os.path.join(self.work, "env-" + user), user=user, token=self.tokens[user])

    def create_repo(self, suffix="", *extra):
        name = self.name + suffix
        res = run(["server", "create-repo", self.srv_root, name, *extra], cwd=self.root, env=self.admin_env)
        self.assertEqual(res.out.strip(), "/" + name)
        return name

    def server_repo_dir(self, name):
        return os.path.join(self.srv_root, "repos", name)

    def clone(self, user, repo, label=None, url=None, env=None, check=True):
        env = env or self.env_for(user)
        label = label or user
        dest = os.path.join(self.work, label)
        res = run(["clone", url or f"{self.server.url}/{repo}", dest], cwd=self.work, env=env, check=check)
        return dest, env, res

    def mg(self, cwd, env, *args, check=True):
        return run(args, cwd=cwd, env=env, check=check)

    def fail_mg(self, cwd, env, *args, code=1):
        res = run(args, cwd=cwd, env=env, check=False)
        self.assertEqual(res.returncode, code, str(res))
        return res

    def commit(self, cwd, env, rel, data, msg):
        write(os.path.join(cwd, rel), data)
        self.mg(cwd, env, "add", rel)
        self.mg(cwd, env, "commit", "-m", msg)
        return self.head(cwd, env)

    def head(self, cwd, env=None):
        sha = run(["rev-parse", "HEAD"], cwd=cwd, env=env or self.admin_env).out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def read(self, cwd, rel):
        return read(os.path.join(cwd, rel))

    def assert_rejected(self, res):
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("[rejected]", res.err)
        self.assertIn("pull first", res.err)
        self.assertIn("error: failed to push some refs", res.err)

    # -- tests ------------------------------------------------------------
    def test_1_clone_push_pull_merge(self):
        repo = self.create_repo()
        alice, a_env, res = self.clone("alice", repo)
        self.assertIn("warning: You appear to have cloned an empty repository.", res.err)
        self.assertTrue(os.path.isdir(os.path.join(alice, ".minigit")))
        self.assertIn("No commits yet", self.mg(alice, a_env, "status").out)
        self.assertEqual(self.mg(alice, a_env, "remote", "-v").out,
                         f"origin\t{self.server.url}/{repo} (fetch)\norigin\t{self.server.url}/{repo} (push)\n")

        first = self.commit(alice, a_env, "README.md", "hello\n", "init")
        res = self.mg(alice, a_env, "push", "-u", "origin", "main")
        self.assertIn(f"To {self.server.url}/{repo}", res.out)
        self.assertIn("main -> main", res.out)
        self.assertIn("[new branch]", res.out)
        self.assertEqual(self.mg(alice, a_env, "branch", "-r").out, "  origin/main\n")
        self.assertIn("up to date with 'origin/main'", self.mg(alice, a_env, "status").out)
        self.assertEqual(self.head(self.server_repo_dir(repo)), first)
        self.assertEqual(self.mg(alice, a_env, "push").out.strip(), "Everything up-to-date")

        bob, b_env, res = self.clone("bob", repo)
        self.assertNotIn("warning", res.err)
        self.assertEqual(self.read(bob, "README.md"), b"hello\n")
        self.assertEqual(self.head(bob, b_env), first)
        self.assertIn("(HEAD -> main, origin/main)", self.mg(bob, b_env, "log", "--oneline").out)

        # Step 2: both commit, bob pushes first, alice is rejected.
        self.commit(alice, a_env, "a.txt", "alice\n", "alice a")
        bob_head = self.commit(bob, b_env, "b.txt", "bob\n", "bob b")
        res = self.mg(bob, b_env, "push")
        self.assertRegex(res.out, rf"{HEX7}\.\.{HEX7}\s+main -> main")
        self.assertEqual(self.head(self.server_repo_dir(repo)), bob_head)
        res = self.fail_mg(alice, a_env, "push")
        self.assert_rejected(res)
        self.assertEqual(self.head(self.server_repo_dir(repo)), bob_head)

        # Step 3: alice pulls (merge commit) and pushes; bob fast-forwards.
        res = self.mg(alice, a_env, "pull")
        self.assertIn("main -> origin/main", res.out)
        self.assertIn("Merge made by the 'three-way' strategy.", res.out)
        self.assertEqual(self.read(alice, "b.txt"), b"bob\n")
        self.assertIn("Merge: ", self.mg(alice, a_env, "log", "-n", "1").out)
        self.assertIn(f"Merge branch 'main' of {self.server.url}/{repo}",
                      self.mg(alice, a_env, "log", "--oneline").out)
        self.assertIn("is ahead of 'origin/main' by 2 commit", self.mg(alice, a_env, "status").out)
        self.mg(alice, a_env, "push")
        merged = self.head(alice, a_env)
        res = self.mg(bob, b_env, "pull")
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.read(bob, "a.txt"), b"alice\n")
        self.assertEqual(self.head(bob, b_env), merged)
        self.assertEqual(self.head(self.server_repo_dir(repo)), merged)
        self.assertEqual(self.mg(bob, b_env, "fetch").out, "")
        self.assertIn("up to date with 'origin/main'", self.mg(bob, b_env, "status").out)
        self.assertIn("Already up to date.", self.mg(bob, b_env, "pull").out)

        # Bob is behind again: status reports it after a fetch.
        self.commit(alice, a_env, "c.txt", "c\n", "alice c")
        self.mg(alice, a_env, "push")
        self.mg(bob, b_env, "fetch")
        self.assertIn("behind 'origin/main' by 1 commit", self.mg(bob, b_env, "status").out)
        self.assertEqual(self.mg(bob, b_env, "fsck").returncode, 0)
        self.assertEqual(self.mg(self.server_repo_dir(repo), self.admin_env, "fsck").out, "")

    def test_2_conflict_through_pull(self):
        repo = self.create_repo()
        alice, a_env, _ = self.clone("alice", repo)
        self.commit(alice, a_env, "a.txt", "one\ntwo\nthree\n", "base")
        self.mg(alice, a_env, "push", "-u", "origin", "main")
        bob, b_env, _ = self.clone("bob", repo)
        self.commit(bob, b_env, "a.txt", "one\nBOB\nthree\n", "bob edit")
        self.mg(bob, b_env, "push")
        self.commit(alice, a_env, "a.txt", "one\nALICE\nthree\n", "alice edit")

        res = self.fail_mg(alice, a_env, "pull")
        self.assertIn("CONFLICT (content): Merge conflict in a.txt", res.out)
        self.assertIn("Automatic merge failed", res.out)
        self.assertEqual(self.read(alice, "a.txt"),
                         b"one\n<<<<<<< HEAD\nALICE\n=======\nBOB\n>>>>>>> origin/main\nthree\n")
        self.assertIn("UU a.txt", self.mg(alice, a_env, "status", "--porcelain").out)
        self.fail_mg(alice, a_env, "commit", "-m", "nope")

        write(os.path.join(alice, "a.txt"), "one\nMERGED\nthree\n")
        self.mg(alice, a_env, "add", "a.txt")
        res = self.mg(alice, a_env, "commit")
        self.assertIn(f"Merge branch 'main' of {self.server.url}/{repo}", res.out)
        merged = self.head(alice, a_env)
        self.mg(alice, a_env, "push")
        self.assertEqual(self.head(self.server_repo_dir(repo)), merged)

        res = self.mg(bob, b_env, "pull")
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.read(bob, "a.txt"), b"one\nMERGED\nthree\n")
        self.assertEqual(self.head(bob, b_env), merged)
        self.assertIn("Merge: ", self.mg(bob, b_env, "log", "-n", "1").out)

    def test_3_auth_and_permissions(self):
        repo = self.create_repo("", "--writers", "alice,bob")
        alice, a_env, _ = self.clone("alice", repo)
        self.commit(alice, a_env, "a.txt", "a\n", "init")
        self.mg(alice, a_env, "push", "-u", "origin", "main")

        bad_env = make_env(os.path.join(self.work, "env-bad"), user="alice", token="wrong-token")
        _, _, res = self.clone("alice", repo, label="bad", env=bad_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication failed", res.err)
        self.assertFalse(os.path.exists(os.path.join(self.work, "bad")))

        anon_env = make_env(os.path.join(self.work, "env-anon"))
        _, _, res = self.clone("alice", repo, label="anon", env=anon_env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("authentication required", res.err)

        carol, c_env, _ = self.clone("carol", repo)
        self.assertEqual(self.read(carol, "a.txt"), b"a\n")
        self.commit(carol, c_env, "c.txt", "c\n", "carol")
        res = self.fail_mg(carol, c_env, "push")
        self.assertIn("permission denied", res.err)
        self.assertEqual(self.head(self.server_repo_dir(repo)), self.head(alice, a_env))

        # Credentials from the URL are stripped from the stored remote and saved.
        url_env = make_env(os.path.join(self.work, "env-url"))
        dest, _, res = self.clone("bob", repo, label="bob-url", env=url_env,
                                  url=f"http://bob:{self.tokens['bob']}@{self.server.url[len('http://'):]}/{repo}")
        self.assertEqual(res.returncode, 0, str(res))
        self.assertEqual(self.mg(dest, url_env, "remote", "get-url", "origin").out.strip(),
                         f"{self.server.url.replace('://', '://bob@')}/{repo}")
        self.assertNotIn(self.tokens["bob"], read(os.path.join(dest, ".minigit", "config")).decode())
        self.commit(dest, url_env, "b.txt", "b\n", "bob via url")
        self.mg(dest, url_env, "push")

        # minigit login stores credentials for later commands.
        login_env = make_env(os.path.join(self.work, "env-login"))
        res = self.mg(self.work, login_env, "login", self.server.url, "--user", "alice",
                      "--token", self.tokens["alice"])
        self.assertIn("stored credentials", res.out)
        self.assertTrue(os.path.exists(login_env["MINIGIT_CREDENTIALS"]))
        dest2, _, res = self.clone("alice", repo, label="alice-login", env=login_env)
        self.assertEqual(self.read(dest2, "b.txt"), b"b\n")

        listing = run(["server", "list", self.srv_root], cwd=self.root, env=self.admin_env).out
        for name in ("alice", "bob", "carol", repo):
            self.assertIn(name, listing)
        res = run(["server", "add-user", self.srv_root, "alice"], cwd=self.root, env=self.admin_env, check=False)
        self.assertEqual(res.returncode, 1)

    def test_4_concurrent_pushes(self):
        repo = self.create_repo()
        alice, a_env, _ = self.clone("alice", repo)
        base = self.commit(alice, a_env, "base.txt", "base\n", "base")
        self.mg(alice, a_env, "push", "-u", "origin", "main")

        clones = []
        for i in range(5):
            user = "alice" if i % 2 == 0 else "bob"
            dest, env, _ = self.clone(user, repo, label=f"clone{i}")
            self.commit(dest, env, f"file{i}.txt", f"{i}\n", f"commit {i}")
            clones.append((dest, env))

        results = [None] * len(clones)
        barrier = threading.Barrier(len(clones))

        def pusher(i):
            dest, env = clones[i]
            barrier.wait()
            results[i] = run(["push"], cwd=dest, env=env, check=False)

        threads = [threading.Thread(target=pusher, args=(i,)) for i in range(len(clones))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        codes = [r.returncode for r in results]
        self.assertEqual(codes.count(0), 1, "\n".join(str(r) for r in results))
        winners = [i for i, r in enumerate(results) if r.returncode == 0]
        for i, r in enumerate(results):
            if i not in winners:
                self.assertEqual(r.returncode, 1, str(r))
                self.assertIn("[rejected]", r.err)
                self.assertIn("pull first", r.err)
        winner_head = self.head(clones[winners[0]][0], clones[winners[0]][1])
        self.assertEqual(self.head(self.server_repo_dir(repo)), winner_head)
        res = self.mg(self.server_repo_dir(repo), self.admin_env, "fsck")
        self.assertEqual(res.out, "")

        # The losers recover with pull + push, one after another.
        for i, (dest, env) in enumerate(clones):
            if i in winners:
                continue
            res = self.mg(dest, env, "pull")
            self.assertIn("Merge made by the 'three-way' strategy.", res.out)
            self.mg(dest, env, "push")
        final = self.head(self.server_repo_dir(repo))
        self.mg(alice, a_env, "pull")
        self.assertEqual(self.head(alice, a_env), final)
        for i in range(5):
            self.assertEqual(self.read(alice, f"file{i}.txt"), f"{i}\n".encode())
        self.assertNotEqual(final, base)
        self.assertEqual(self.mg(self.server_repo_dir(repo), self.admin_env, "fsck").out, "")

    def test_5_branches_and_delete(self):
        repo = self.create_repo()
        alice, a_env, _ = self.clone("alice", repo)
        self.commit(alice, a_env, "a.txt", "a\n", "init")
        self.mg(alice, a_env, "push", "-u", "origin", "main")
        self.mg(alice, a_env, "switch", "-c", "feature")
        self.commit(alice, a_env, "f.txt", "f\n", "feature")
        res = self.mg(alice, a_env, "push", "-u", "origin", "feature")
        self.assertIn("[new branch]", res.out)
        self.assertIn("feature -> feature", res.out)
        self.assertEqual(self.mg(alice, a_env, "branch", "-r").out, "  origin/feature\n  origin/main\n")

        bob, b_env, _ = self.clone("bob", repo)
        self.assertEqual(self.read(bob, "a.txt"), b"a\n")
        self.assertFalse(os.path.exists(os.path.join(bob, "f.txt")))
        res = self.mg(bob, b_env, "switch", "feature")  # DWIM from origin/feature
        self.assertIn("Switched to", res.out)
        self.assertIn("'feature'", res.out)
        self.assertEqual(self.mg(bob, b_env, "branch").out, "* feature\n  main\n")
        self.assertEqual(self.read(bob, "f.txt"), b"f\n")
        self.assertIn("up to date with 'origin/feature'", self.mg(bob, b_env, "status").out)

        self.mg(alice, a_env, "switch", "main")
        self.mg(alice, a_env, "merge", "feature")
        self.mg(alice, a_env, "push")
        res = self.mg(alice, a_env, "push", "--delete", "origin", "feature")
        self.assertIn("feature", res.out)
        res = self.mg(bob, b_env, "fetch")
        self.assertIn("[deleted]", res.out)
        self.assertEqual(self.mg(bob, b_env, "branch", "-r").out, "  origin/main\n")
        res = self.fail_mg(alice, a_env, "push", "--delete", "origin", "main")
        self.assertIn("[rejected]", res.err)
        self.assertEqual(self.mg(self.server_repo_dir(repo), self.admin_env, "fsck").out, "")

    def test_6_force_push_is_disabled(self):
        repo = self.create_repo()
        alice, a_env, _ = self.clone("alice", repo)
        self.commit(alice, a_env, "a.txt", "a\n", "init")
        self.commit(alice, a_env, "b.txt", "b\n", "second")
        self.mg(alice, a_env, "push", "-u", "origin", "main")
        self.mg(alice, a_env, "reset", "--hard", "HEAD~1")
        self.commit(alice, a_env, "c.txt", "c\n", "rewritten")
        res = self.fail_mg(alice, a_env, "push")
        self.assert_rejected(res)
        res = self.fail_mg(alice, a_env, "push", "--force")
        self.assertIn("[rejected]", res.err)
        self.assertEqual(self.mg(self.server_repo_dir(repo), self.admin_env, "fsck").out, "")


if __name__ == "__main__":
    unittest.main()
