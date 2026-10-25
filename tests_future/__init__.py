"""Acceptance tests for features that are designed but not implemented yet.

Each module here is skipped by default (``unittest.skip("feature not implemented yet")``). Set
``MINIGIT_RUN_FUTURE=1`` to run one while implementing its feature; once it passes, move it to
``tests_blackbox/`` and drop the skip. Run from E:\\ideas\\minigit with::

    python -m unittest discover -s tests_future -t .
"""
