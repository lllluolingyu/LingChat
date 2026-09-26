"""Exec the SDK CLI in a private session; no shell and no credential handling."""

import os
import sys

if __name__ == "__main__":
    os.setsid()
    os.execv(sys.argv[1], sys.argv[1:])
