minigit {VERSION}: a small git clone with a multi-user HTTP server. Nothing else to install.

## Downloads

| System | File | Install |
|---|---|---|
| Windows 10/11 (x64) | `minigit-{VERSION}-windows-x64-setup.exe` | run it; it installs for your user and adds `minigit` to PATH (no admin needed) |
| Windows, no installer | `minigit-{VERSION}-windows-x64.zip` | unzip `minigit.exe` anywhere on your PATH |
| Linux x64 | `minigit-{VERSION}-linux-x64.tar.gz` | `tar xzf` it, then `./minigit-{VERSION}/install.sh` (installs to `~/.local/bin`) |
| macOS Intel / Apple silicon | `minigit-{VERSION}-macos-x64.tar.gz`, `minigit-{VERSION}-macos-arm64.tar.gz` | same as Linux; the script also clears the quarantine flag |
| Any OS with Python 3.11+ | `minigit-{VERSION}-py3-none-any.whl` | `pipx install <file>` or `pip install <file>` |

`SHA256SUMS.txt` lists the checksum of every file.

The executables are not code-signed. Windows SmartScreen and macOS Gatekeeper may ask once; choose "Run anyway"
(Windows) or allow it under System Settings > Privacy & Security (macOS).

## What is new in 0.4.0

- The web UI is now **minigithub**: a new look with its own logo and colours, light and dark themes, a code page with the file table, last commit per file and the README rendered, commits grouped by day, colored diffs with line numbers (unified or split), a blame view, and pull request pages. It works on phones and needs no JavaScript.

## What was new in 0.3.0

No new commands: this release makes the existing ones behave like git and fail better than git.

- Output matches git: `status` (with git's hint lines), `diff` hunk headers with the function name, `log --graph` order, `log -- PATH` history simplification, `commit`/`merge`/`cherry-pick` summaries with diffstat and create/delete lines, `Auto-merging` and `CONFLICT` lines, fetch/push lines.
- Merges produce the same files and conflict hunks as git (git's histogram diff and conflict trimming); commit messages are cleaned up like git's, so the same commit gets the same id.
- git's exit codes: `fatal:` exits 128, usage errors 129, ctrl-C 130, and a closed pipe (`minigit log | head`) is silent.
- Clearer than git: "did you mean" hints for commands, options and branch names; plain-language network errors (connection refused, unknown host); tokens are never printed.
- Safer: `push origin HEAD` pushes the current branch, a damaged branch file is reported instead of silently starting a new history, and `gc --prune` refuses while a ref is unreadable.
- Faster: startup is about twice as fast and `status` on a 2000-file repository about 40 % faster.
- New revision syntax `REV^{tree}`, `^{commit}`, `^{}`.

## What was new in 0.2.0

- Tags: `tag`, `tag -a -m`, `tag -d`, `tag -l`, pushed with `push origin v1` or `push --tags`, fetched and cloned, shown in `log` and the web UI.
- `commit --amend`, `add -A`, `add -u`, `cherry-pick A..B`, `revert A..B`.
- `log -- PATH`, `log --graph`, `--stat` for `diff`, `show` and `log`, `show` of a merge against its first parent (`-m` for each parent).
- `branch -vv` with the upstream column, `branch -u` / `--unset-upstream`, `status` reports a gone upstream, `pull --rebase`.
- `gc` removes orphan temporary objects and unreachable objects; `fsck` is about five times faster and `blame` twice as fast on long histories.
- `rebase` replays commits below a merge in the same order as git, so the rebased commit ids match git's.

## First steps

```
minigit config --global user.name "Your Name"
minigit config --global user.email you@example.com
minigit login --user USER --token TOKEN https://your-hub.example.com
minigit clone https://your-hub.example.com/project
```

Run `minigit --help` for the command list and `minigit <command> --help` for one command.
