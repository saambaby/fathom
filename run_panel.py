"""Repo-root launcher for the Fathom Streamlit admin panel.

Run with::

    streamlit run run_panel.py [-- --db-path PATH]

Why this launcher exists
------------------------
``streamlit run <script>`` prepends the *script's own directory* to
``sys.path[0]``.  Launching ``panel/app.py`` directly therefore puts ``panel/``
first on the path, and Python's default finder then resolves the top-level
``data`` package to ``panel/data.py`` (the panel view-model module), raising
``ModuleNotFoundError: No module named 'data.store'; 'data' is not a package``.

Running Streamlit against this repo-root script keeps ``sys.path[0]`` at the
repository root, so the real ``data`` package and the ``panel`` package both
resolve correctly.  The panel code itself is unchanged; this is a launch shim.
"""

from __future__ import annotations

import runpy
from pathlib import Path

_APP = Path(__file__).resolve().parent / "panel" / "app.py"

runpy.run_path(str(_APP), run_name="__main__")
