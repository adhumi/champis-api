"""Diagnostic « explain » : d'où vient T sur un département, facteur par facteur.

Réponse à la saturation observée sur le Doubs (spec › Points ouverts › saturation de T) : avant de
réviser une valeur d'espèce, mesurer sur les rasters réels la part de chaque facteur. Sortie JSON
(`reports/explain-<espèce>-<dept>.json`) : surface par hôte avec son poids et son T moyen,
distribution de chaque facteur sur la forêt, pH et altitude bruts, et des « et si » neutres
(part « excellent » si un facteur valait 1 partout, ou si l'hôte était plafonné).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window

from .config import BLOCK, REPORTS_DIR, dept_dirs
from .scoring import NODATA_U8, NODATA_U16, PH_FLOOR, terrain_factors, trapezoid, weighted_sum
from .species import host_vocabulary, load_species

log = logging.getLogger(__name__)

FACTORS = ("host", "cover", "ph", "altitude", "slope", "aspect")
EXCELLENT = 0.75
HOST_CAPS = (0.9, 0.8, 0.7)


def explain(dept: str, species_id: str, out_dir: Path = REPORTS_DIR) -> dict[str, Any]:
    g = dept_dirs(dept)["grid"]
    srcs = {k: g / f"{k}.tif" for k in ("host", "cover", "ph", "alt", "slope", "aspect")}
    missing = [k for k, p in srcs.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"rasters grid manquants pour {dept} : {missing}")
    sp = load_species(species_id)
    vocab = host_vocabulary()
    handles = {k: rasterio.open(p) for k, p in srcs.items()}
    ref = handles["host"]
    px_ha = (ref.res[0] * ref.res[1]) / 10_000

    acc = _Acc(len(vocab))
    windows = [
        Window(c, r, min(BLOCK, ref.width - c), min(BLOCK, ref.height - r))
        for r in range(0, ref.height, BLOCK)
        for c in range(0, ref.width, BLOCK)
    ]
    for w in windows:
        host = handles["host"].read(1, window=w)
        forest = host != NODATA_U8
        if not forest.any():
            continue
        ph10 = handles["ph"].read(1, window=w)
        alt = handles["alt"].read(1, window=w)
        f = terrain_factors(
            sp,
            host,
            handles["cover"].read(1, window=w),
            ph10,
            alt,
            handles["slope"].read(1, window=w),
            handles["aspect"].read(1, window=w),
        )
        acc.add(sp, host[forest], ph10[forest], alt[forest], {k: v[forest] for k, v in f.items()})
    for h in handles.values():
        h.close()

    result = acc.result(sp, vocab, px_ha)
    result.update({"species": species_id, "dept": dept})
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"explain-{species_id}-{dept}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("écrit %s", path)
    return result


class _Acc:
    """Accumulateurs par bloc : tout en comptages, rien en RAM pixel par pixel."""

    def __init__(self, n_hosts: int):
        self.n = 0
        self.host_px = np.zeros(n_hosts, dtype=np.int64)
        self.host_t_sum = np.zeros(n_hosts, dtype=np.float64)
        self.host_excellent = np.zeros(n_hosts, dtype=np.int64)
        self.f_hist = {k: np.zeros(10, dtype=np.int64) for k in FACTORS}
        self.f_sum = dict.fromkeys(FACTORS, 0.0)
        self.t_hist = np.zeros(10, dtype=np.int64)
        self.ws_hist = np.zeros(10, dtype=np.int64)
        self.ph_hist = np.zeros(20, dtype=np.int64)  # pH 0–10 par 0,5
        self.ph_nodata = 0
        self.ph_floor = 0
        self.alt_hist = np.zeros(40, dtype=np.int64)  # 0–4 000 m par 100 m
        self.excellent = 0
        self.excellent_if_one = dict.fromkeys(FACTORS, 0)
        self.excellent_if_cap = dict.fromkeys(HOST_CAPS, 0)

    def add(self, sp, host, ph10, alt, f):
        edges = np.linspace(0, 1, 11)
        edges[-1] = 1.0001
        ws = weighted_sum(sp, f)
        t = np.clip(f["host"] * ws, 0, 1)
        self.n += host.size
        np.add.at(self.host_px, host, 1)
        np.add.at(self.host_t_sum, host, t)
        np.add.at(self.host_excellent, host, (t >= EXCELLENT).astype(np.int64))
        for k in FACTORS:
            self.f_hist[k] += np.histogram(f[k], bins=edges)[0]
            self.f_sum[k] += float(f[k].sum())
        self.t_hist += np.histogram(t, bins=edges)[0]
        self.ws_hist += np.histogram(ws, bins=edges)[0]
        valid_ph = ph10 != NODATA_U8
        self.ph_nodata += int((~valid_ph).sum())
        self.ph_hist += np.histogram(ph10[valid_ph] / 10.0, bins=np.linspace(0, 10, 21))[0]
        raw_ph = trapezoid(ph10[valid_ph] / 10.0, sp["terrain"]["ph"])
        self.ph_floor += int((raw_ph < PH_FLOOR).sum())
        valid_alt = alt != NODATA_U16
        self.alt_hist += np.histogram(alt[valid_alt], bins=np.arange(0, 4100, 100))[0]
        self.excellent += int((t >= EXCELLENT).sum())
        for k in FACTORS:
            g = dict(f)
            g[k] = np.ones_like(f[k])
            t1 = g["host"] * weighted_sum(sp, g) if k != "host" else ws
            self.excellent_if_one[k] += int((np.clip(t1, 0, 1) >= EXCELLENT).sum())
        for cap in HOST_CAPS:
            tc = np.minimum(f["host"], cap) * ws
            self.excellent_if_cap[cap] += int((tc >= EXCELLENT).sum())

    def result(self, sp, vocab, px_ha):
        n = max(self.n, 1)
        hosts = []
        for i, name in enumerate(vocab):
            if self.host_px[i] == 0:
                continue
            hosts.append(
                {
                    "host": name,
                    "weight": float(
                        sp["terrain"]["hosts"].get(name, sp["terrain"]["hosts"]["default"])
                    ),
                    "ha": round(float(self.host_px[i] * px_ha), 1),
                    "share": round(float(self.host_px[i] / n), 4),
                    "t_mean": round(float(self.host_t_sum[i] / self.host_px[i]), 3),
                    "excellent_share": round(float(self.host_excellent[i] / self.host_px[i]), 3),
                }
            )
        hosts.sort(key=lambda h: -h["ha"])
        return {
            "forest_ha": round(float(self.n * px_ha), 1),
            "excellent_share": round(self.excellent / n, 4),
            "hosts": hosts,
            "factors": {
                k: {
                    "mean": round(self.f_sum[k] / n, 3),
                    "hist": self.f_hist[k].tolist(),
                    "share_ge_0_9": round(float(self.f_hist[k][9] / n), 4),
                }
                for k in FACTORS
            },
            "t_hist": self.t_hist.tolist(),
            "weighted_sum_hist": self.ws_hist.tolist(),
            "ph": {
                "hist_0_10_step_0_5": self.ph_hist.tolist(),
                "nodata_share": round(self.ph_nodata / n, 4),
                "floor_share": round(self.ph_floor / n, 4),
                "trapezoid": sp["terrain"]["ph"],
            },
            "altitude": {
                "hist_step_100m": self.alt_hist.tolist(),
                "trapezoid": sp["terrain"]["altitude_m"],
            },
            "what_if": {
                "excellent_share_if_factor_is_one": {
                    k: round(v / n, 4) for k, v in self.excellent_if_one.items()
                },
                "excellent_share_if_host_capped_at": {
                    str(c): round(v / n, 4) for c, v in self.excellent_if_cap.items()
                },
            },
            "weights": sp["terrain"]["weights"],
        }
