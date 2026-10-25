"""In-process tests for the per-user token list in server.json (features/server-admin.md, section 2)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from minigit import admin, auth


def _write(root: str, data: dict) -> None:
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "server.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class TokenFormatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "srv")

    def read(self) -> dict:
        with open(os.path.join(self.root, "server.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_old_format_loads_authenticates_and_migrates_on_write(self):
        old = {"version": 1, "users": {"alice": {"token_sha256": auth.hash_token("old-token")},
                                       "bob": {"token_sha256": auth.hash_token("bob-token")}},
               "repos": {}}
        _write(self.root, old)
        cfg = auth.ServerConfig(self.root)
        self.assertTrue(cfg.authenticate("alice", "old-token"))
        self.assertFalse(cfg.authenticate("alice", "bob-token"))
        self.assertEqual(admin.list_tokens(self.root, "alice"), [("default", None)])
        self.assertEqual(self.read(), old, "reading never rewrites the file")
        new = admin.add_token(self.root, "alice", "ci")
        data = self.read()
        self.assertEqual(data["users"]["bob"], old["users"]["bob"], "untouched users are not rewritten")
        self.assertEqual(data["users"]["alice"]["token_sha256"], auth.hash_token("old-token"))
        self.assertNotIn("token_created", data["users"]["alice"])
        cfg = auth.ServerConfig(self.root)
        self.assertTrue(cfg.authenticate("alice", "old-token"))
        self.assertTrue(cfg.authenticate("alice", new))
        self.assertFalse(cfg.authenticate("bob", new))
        auth.add_user(self.root, "alice", reset=True)
        self.assertIsInstance(self.read()["users"]["alice"]["token_created"], int)
        self.assertTrue(auth.ServerConfig(self.root).authenticate("alice", new))

    def test_malformed_records_never_authenticate(self):
        bogus = {"version": 1, "repos": {}, "users": {
            "a": {"token_sha256": "é" * 64},
            "b": {"tokens": []},
            "c": {"tokens": {"x": "str", "y": {"sha256": 5}, "z": {"sha256": "abc"}}},
            "d": "not-a-dict",
            "e": {"tokens": {"ok": {"sha256": auth.hash_token("good")}}},
        }}
        _write(self.root, bogus)
        cfg = auth.ServerConfig(self.root)
        for user in "abcd":
            for token in ("", "x", "é" * 64, "abc"):
                self.assertFalse(cfg.authenticate(user, token), (user, token))
            self.assertEqual(auth.user_tokens(cfg.users[user]), [])
        self.assertTrue(cfg.authenticate("e", "good"))
        self.assertFalse(cfg.authenticate("e", "bad"))


if __name__ == "__main__":
    unittest.main()
