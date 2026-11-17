#!/bin/sh
# Launcher entry point for ZeroDesk. Isolated Python: ignores PYTHONPATH,
# user site-packages and the current directory.
exec /usr/bin/python3 -I -B /usr/lib/zerodesk/run.py "$@"
