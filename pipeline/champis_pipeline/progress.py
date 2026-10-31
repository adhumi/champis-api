"""tqdm du pipeline : désactivé quand `CHAMPIS_QUIET` est posé (processus du batch, option `-q`).

La variable est lue à chaque barre, pas à l'import : `console.setup(quiet=True)` la pose après que
les modules sont chargés.
"""

from __future__ import annotations

import os
from typing import Any

from tqdm import tqdm as _tqdm


def tqdm(*args: Any, **kwargs: Any) -> _tqdm:
    kwargs.setdefault("disable", bool(os.environ.get("CHAMPIS_QUIET")))
    return _tqdm(*args, **kwargs)
