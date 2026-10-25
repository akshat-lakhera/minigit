# Backlog

## Known open issues

From the original bug lists (`bugs_A..D.json`); everything else on those lists was fixed.

1. **`diff <rev>` invents a mode change on Windows** (100755 -> 100644) for a path that is in the revision but
   missing from the index, because the mode is taken from `stat`. It should keep the revision's mode
   (bugs_D; the black-box test `DiffFormsTest.test_diff_rev_keeps_head_mode_for_path_missing_from_index` in
   `tests_blackbox/test_status_diff.py` still fails on Windows).

## Next features

All four features that had acceptance tests in `tests_future/` (stash, rebase, server admin, pull requests)
are implemented; their tests now live in `tests/`. `tests_future/` only keeps the shared `__init__.py`.

## Further ideas

- Tags (lightweight and annotated), `describe`.
- Packfiles and a smarter transfer protocol (negotiation instead of whole-object sends), `gc`.
- Rename detection in merge and diff; `diff --stat`; `log --graph`.
- Native TLS or documented reverse-proxy recipe; token expiry; write access in the web UI.
- A CI workflow running the unit tests on Windows and Linux.
