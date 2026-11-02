# Backlog

## Known open issues

What is left after the black-box rounds (`qa/`); everything else on those lists was fixed.

1. **Performance on big histories.** Measured on a generated 2000-commit repository (17 800 loose objects, one
   file touched by 90 % of the commits, `blame` on that file) with `python -m minigit` on Windows 11 / NTFS, old
   and new code alternating, best of two rounds, while the machine was busy with other work (file opens cost
   up to 30 ms each, so the absolute numbers are inflated; the ratios hold). Before: `fsck` 83.9 s, `blame`
   22.6 s, `log --oneline` 2.5 s, `status` 1.0 s. After: `fsck` 16.9 s, `blame` 11.7 s, `log --oneline` 2.4 s,
   `status` 0.9 s. What changed: an `ObjectStore` caches the parsed commits and trees and every object's type for
   the lifetime of the process; `fsck` hash-checks every object once and runs the connectivity pass from those
   caches (35 600 file opens and 18 000 stats became 17 800 opens and none, the minimum for a full check);
   `blame` reads each commit, tree and blob once and runs difflib once per blob pair (12 600 opens became 5 800;
   the rest is difflib, about 40 % of its time); `validate_name`, `_is_hex` and `split_lines` lost their
   per-character Python loops. The targets of `fsck` under 10 s and `blame` under 5 s were not confirmed on a
   quiet machine (on a quiet run `log --oneline` took 0.4 s instead of 2.5 s, so the quiet numbers are several
   times lower than the ones above, but they were not measured). Still open: the per-object file open that a
   loose-object store cannot avoid (the first access to freshly written objects is slowest: a cold `fsck` right
   after creating the 17 800 files took over ten minutes), and difflib in `blame`.

   **Startup and hot paths (second round).** Measured with `python -m minigit -C <repo>`, old and new code
   alternating, best of N, on the same busy Windows 11 machine (other test suites were running, so compare the
   columns, not the absolute values). Small repo (5 files, 1 commit), best of 7, before -> after:
   `--version` 150 -> 80 ms, `status` 182 -> 138 ms, `log --oneline` 179 -> 128 ms, `diff` 186 -> 143 ms (a bare
   `python -c pass` is 25-40 ms on this machine). Generated repo with 2000 files (321 tree objects) and 500
   commits, best of 5: clean `status` 342 -> 172 ms, `status` with 5 modified files 467 -> 280 ms, `add .` with
   5 modified files 450 -> 307 ms, `log` 222 -> 174 ms, `log --oneline` 265 -> 222 ms, clean `diff` 295 -> 239 ms,
   `commit` of 5 staged files 538 -> 540 ms (unchanged in wall time: its object reads went from 330 to 6, but it
   is dominated by the stat and fsync of each tree and the index write).
   What changed: `main()` builds only the running command's subparser (`build_parser(only=...)`), so the
   other ~40 parsers and the modules that define them (cherry, stash, pr, rebase, tags, gc, serveradmin) are
   no longer built or imported on every call; `traceback`, `difflib`, `datetime`, `random` and `shutil`
   (which pulls in bz2 and lzma) are imported only where used. `status` hashes the index's directories in
   memory and copies every subtree whose id matches HEAD's (`flatten_tree(..., known=)`): a clean `status`
   reads one commit and no tree instead of 321 trees; it and `add .` also reuse the `scandir` stat of every
   file instead of a second `stat`. `commit` hashes its trees in memory, writes only the missing ones (no
   parse-and-revalidate of the hundreds that already exist) and computes the shortstat against the parent the
   same way. On-disk formats are unchanged. Left: `dataclasses` (about 20 ms
   of import, through `inspect`) is used by most modules; replacing it would touch every owner's file.
2. **`rebase --continue` with an empty resolution asks for `--skip`.** git does the same; kept as documented.

Done in this round: `commit --amend`, `add -A` / `add -u`, `cherry-pick` and `revert` of `A..B` ranges; `log -- path`,
`log --graph`, `--stat` for `diff`, `show` and `log`, `show` of a merge against its first parent (`-m`: each parent);
`branch -vv` shows the upstream column, `status` reports a gone upstream and `branch -u` / `--unset-upstream` fix it;
`pull --rebase`; tags (lightweight and annotated, pushed, fetched, cloned, shown in `log` and the web UI,
`features/tags.md`); `rebase` replays the commits below a merge in git's `--topo-order` (first-parent lineage
first), so the rebased commits now get the same ids as with git (`tests/test_rebase.py` pins a merge-containing
history against the id real git produces); `minigit gc [--force] [--prune]` removes orphan `objects/xx/tmp-*`
files and unreachable objects, and `fsck` warns about the former (SPEC section 6, `tests/test_gc.py`).
Earlier: user names that differ only in case are now refused; a merge made by `pull` no longer puts the URL's
user name into history; `pr show` of a closed PR whose branch is gone says so.

## Next features

All four features that had acceptance tests in `tests_future/` (stash, rebase, server admin, pull requests)
are implemented; their tests now live in `tests/`. `tests_future/` only keeps the shared `__init__.py`.

## Further ideas

- `describe` (tags themselves are done: `features/tags.md`).
- Packfiles and a smarter transfer protocol (negotiation instead of whole-object sends).
- Rename detection in merge and diff.
- Native TLS or documented reverse-proxy recipe; token expiry; write access in the web UI.
- A CI workflow running the unit tests on Windows and Linux.
