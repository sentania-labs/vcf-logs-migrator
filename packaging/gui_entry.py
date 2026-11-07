"""Entry point of the windowed Windows program: opens the window, never a console.

Built with the GUI subsystem next to the console `logs-migrate.exe`, over the same
engine. A windowed build has no stdout or stderr, so they are pointed at the null
device to keep library writes from failing.
"""

import os
import sys

from launcher import run

if __name__ == "__main__":
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))  # noqa: SIM115
    sys.exit(run(["gui", *sys.argv[1:]]))
