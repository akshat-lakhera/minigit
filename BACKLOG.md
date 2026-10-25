# Backlog

## Known open issues

What is left after the black-box rounds (`qa/`); everything else on those lists was fixed.

1. **`rebase` replays commits in a different order than git when the branch contains merges.** The result has
   the same tree but different commit ids.
2. **Performance on big histories.** On 2000 commits `fsck` takes 87 s and `blame` 26 s; Python startup alone
   costs 0.6 s per command.
3. **`rebase --continue` with an empty resolution asks for `--skip`.** git does the same; kept as documented.
4. **No gc for orphan `objects/xx/tmp-*` files** left behind when a process dies in the middle of writing an
   object.
5. **`branch -vv` shows no upstream column** (`[origin/main]`, ahead/behind); it prints the same as `-v`.
6. **`status` says nothing when the upstream branch is gone.** git prints
   `Your branch is based on 'origin/x', but the upstream is gone.` (`fetch` already removes the stale
   `origin/*` ref and accepts `--prune`.)
7. **`show` of a merge commit prints a first-parent diff without saying so.**

Done in this round: user names that differ only in case are now refused; a merge made by `pull` no longer puts
the URL's user name into history; `pr show` of a closed PR whose branch is gone says so.

## Next features

All four features that had acceptance tests in `tests_future/` (stash, rebase, server admin, pull requests)
are implemented; their tests now live in `tests/`. `tests_future/` only keeps the shared `__init__.py`.

## Further ideas

- Everyday options that are still missing: `commit --amend`, `add -A`, `log -- path`, `--stat` (diff, show),
  `log --graph`, `pull --rebase`, `cherry-pick` of ranges.
- Tags (lightweight and annotated), `describe`.
- Packfiles and a smarter transfer protocol (negotiation instead of whole-object sends), `gc`.
- Rename detection in merge and diff.
- Native TLS or documented reverse-proxy recipe; token expiry; write access in the web UI.
- A CI workflow running the unit tests on Windows and Linux.
