"""tqdm du pipeline : désactivé quand CHAMPIS_QUIET est posé (processus du batch)."""

from __future__ import annotations

import os
from functools import partial

from tqdm import tqdm as _tqdm

tqdm = partial(_tqdm, disable=bool(os.environ.get("CHAMPIS_QUIET")))
