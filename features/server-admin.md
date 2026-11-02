# Feature: `server-admin` (users, named tokens and permissions in `minigit server`)

Status: implemented. Acceptance tests: `tests/test_server_admin.py` (30). Code: `minigit/serveradmin.py`
(parsers and command dispatch), `minigit/admin.py` (the operations), `minigit/auth.py` (config format, token
check, `add-user`, `create-repo`, `list`).

## 1. Commands

All commands are `minigit server <subcommand> ROOT ...`. Every command that changes `server.json` does it
under the lock (`server.json.lock`) with an atomic write, so concurrent admin commands lose no update, and a
refused command changes nothing. A running `minigit serve` re-reads `server.json` on every request, so every
change below applies to the very next request. Fields this feature does not know are kept.

Only `init` creates a server root. Every other subcommand first checks that `ROOT/server.json` exists and
otherwise fails (exit 1, nothing created) with `error: 'ROOT' is not a minigit server root` and
`hint: create it with 'minigit server init ROOT'`, so a mistyped ROOT never becomes a new, empty root.

| Command | Output (stdout) | Notes |
|---|---|---|
| `init ROOT` | `Initialized minigit server root in ROOT` (`Reinitialized existing minigit server root in ROOT` when it exists) | an existing root keeps all its data |
| `add-user ROOT NAME [--reset]` | the token, alone on one line | stderr carries a note that the token is shown once. `--reset` replaces only the `default` token; named tokens and other fields stay. `--reset` of a user that does not exist is an error (`user 'NAME' does not exist`, hint: run without `--reset`). The names `.` and `..` and a name that differs only in case from an existing user (`ALICE` next to `alice`) are refused |
| `remove-user ROOT NAME` | `Removed user 'NAME'` and, if it was listed anywhere, `Removed 'NAME' from the access lists of: r1, r2` | deletes the user and all its tokens and removes the name from every `readers` and `writers` list. A user created later under the same name gets nothing back. `*` entries stay |
| `add-token ROOT USER NAME` | the new token, alone on one line (stderr empty) | NAME is `[A-Za-z0-9_.-]{1,64}`. `default` names the `add-user` token, which is possible again after it was revoked. A duplicate is an error (`token 'ci' of user 'alice' already exists`) |
| `list-tokens ROOT USER` | one line per usable token: `NAME<TAB>CREATED`, `default` first, then by name | CREATED is `YYYY-MM-DDTHH:MM:SSZ` (UTC), or `-` when unknown (records written before this feature). Never shows a token or a hash. A user without tokens prints nothing |
| `revoke-token ROOT USER NAME` | `Revoked token 'NAME' of user 'USER'` | `default` can be revoked like any other. Unknown NAME: `user 'U' has no token named 'N'` |
| `revoke-token ROOT USER --all` | `Revoked N token(s) of user 'USER'` | N counts usable tokens. The user stays but cannot log in. NAME and `--all` together, or neither, is a usage error (exit 129) |
| `create-repo ROOT NAME [--readers L] [--writers L] [--public] [--allow-force]` | `/NAME` | `--readers ""` / `--writers ""` is an explicit empty list: nobody (only the omitted option defaults to `*`). Like `perm`, every listed name must be an existing user or `*` (`user 'ghost' does not exist`). stderr: `Created repository 'NAME' (clone with: minigit clone http://HOST:PORT/NAME)` |
| `perm ROOT REPO USERS [read\|write\|none]` | one line `REPO: USER = LEVEL` per user | USERS is a comma separated list of user names and/or `*`. Without a level it only reports (nothing is written). See 3 |
| `set-repo ROOT REPO [--public\|--private] [--allow-force\|--no-allow-force]` | `REPO: public=yes\|no allow-force=yes\|no` | at least one option is required; contradicting options are a usage error. Prints the resulting values of both flags |
| `list ROOT` | users with their token names, then repos | the user line is `  NAME  tokens=default,ci,laptop` (`tokens=-` when none); the repo line is unchanged |

Exit codes: 0 ok, 1 an error (`error: ...` on stderr), 129 a usage error (missing or invalid arguments). Names
are validated before anything is touched (`invalid user name`, `invalid token name`, `invalid repository
name`). Unknown objects are `user 'X' does not exist` and `repository 'X' does not exist`.

## 2. Config format

```json
{"version": 1,
 "users": {"alice": {"token_sha256": "<hex>", "token_created": 1700000000,
                     "tokens": {"ci": {"sha256": "<hex>", "created": 1700000000}}}},
 "repos": {"proj": {"readers": ["bob"], "writers": ["alice"], "public": false, "allow_force": false}}}
```

- `token_sha256` (+ `token_created`, epoch seconds) is the `default` token, exactly the field older versions
  wrote. `tokens` holds the additional named tokens. Only `sha256(token)` is stored. A user is allowed in with
  any one of its tokens; the name of a token is not part of the credential (login is still `user:token`).
- **Migration from the old format:** the new format is a superset, so an existing `server.json` (one
  `token_sha256` per user, nothing else) needs no conversion: it loads as a user with only a `default` token
  of unknown creation time, and the first write adds `tokens` / `token_created` only to the records it touches.
  `add-user --reset` on an old record adds `token_created`. Nothing is rewritten wholesale.
- Malformed entries (a hash that is not 64 hex digits, a `tokens` entry that is not an object, `tokens` not an
  object, a non-integer time) are ignored: they never authenticate, never appear in `list-tokens`, never cause
  an error, and `revoke-token --all` clears them. `revoke-token` drops an empty `tokens` object.

## 3. ACL semantics

- A name sits in at most one of `readers` / `writers` after `perm` touched it. `write` means "in `writers`"
  (writers can read: `can_read` accepts both lists); `read` means "in `readers`, not in `writers`"; `none`
  removes it from both. Order of the lists is kept, new names are appended. Names that `perm` does not mention
  are never touched. Repeating a command is a no-op.
- `*` is an ordinary entry that matches any authenticated user. Only `none` may name something that is not a
  user (so stale entries can be cleaned out); `read`, `write` and the query form need existing users.
- The printed level is the **effective** one. If a user keeps more access than requested because `*` is in a
  list, or because the repository is `public` (anonymous read), the effective level is printed and a warning
  goes to stderr: `warning: alice still has read access to 'proj' through '*'` /
  `... because the repository is public`.
- Signed-in users without read access get HTTP 403 `permission denied: user 'U' may not read 'R'` for
  every URL of an existing repository (the CLI says `permission denied`); for a signed-in user a repository
  that does not exist is 404. Anonymous callers get 401 for a non-public repository whether or not it exists.
  **This changes the earlier behaviour** where a signed-in user got 404 for a private repository (to hide its
  name); an administrator needs to tell "no access" from "no such repository". Repository names remain hidden
  from anonymous callers (a missing and a private repository look the same) and from `GET /` listings, which
  show only the repositories the caller may read. The web UI under `/-/` follows the same rule
  (`features/web-ui.md`, 2.2).
