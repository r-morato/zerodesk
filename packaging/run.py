"""Entry point used by the launcher (run with python3 -I -B)."""
import os
import sys

sys.path.insert(0, "/usr/lib/zerodesk")
os.environ.setdefault("ZERODESK_WEB", "/usr/lib/zerodesk/web")

from zerodesk.main import main  # noqa: E402

sys.exit(main())
