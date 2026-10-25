"""minigit.__main__ -- owner: M7 cli. Runs the CLI: ``python -m minigit <command>``."""
import sys

from minigit.cli import main

sys.exit(main())
