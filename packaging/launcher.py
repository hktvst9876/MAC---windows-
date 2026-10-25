"""PyInstaller entry point."""

import multiprocessing
import sys

from mirrorbridge.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
