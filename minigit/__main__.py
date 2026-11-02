"""minigit.__main__ -- owner: M7 cli. Runs the CLI: ``python -m minigit <command>``."""
import sys

try:
    from minigit.cli import main

    rc = main()
except KeyboardInterrupt:  # ctrl-C while Python is still importing: no traceback either
    sys.stderr.write("interrupted\n")
    rc = 130
except BrokenPipeError:  # stderr itself went away while an error was being reported
    rc = 141

sys.exit(rc)
