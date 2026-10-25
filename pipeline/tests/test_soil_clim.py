"""Climatologie du sol ERA5-Land (CDS) : NetCDF synthétique, sans réseau (option a, 25 oct. 2026)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import numpy as np
import pytest
import xarray as xr
from champis_pipeline import soil_clim


def _netcdf(path, year: int, lats, lons, sea_ij=(0, 0)):
    """Moyenne journalière = mois/100 + latitude/1000, constante dans le mois ; un pixel « mer »."""
    days = np.arange(f"{year}-01-01", f"{year + 1}-01-01", dtype="datetime64[D]")
    months = (days.astype("datetime64[M]").astype(int) % 12) + 1
    vals = (
        months[:, None, None] / 100 + np.asarray(lats)[None, :, None] / 1000 + 0 * np.asarray(lons)
    )
    vals = vals.astype("float32")
    vals[:, sea_ij[0], sea_ij[1]] = np.nan
    ds = xr.Dataset(
        {"swvl1": (("valid_time", "latitude", "longitude"), vals)},
        coords={"valid_time": days.astype("datetime64[ns]"), "latitude": lats, "longitude": lons},
    )
    ds.to_netcdf(path)


class _FakeCds:
    def __init__(self, lats, lons):
        self.lats, self.lons, self.requests = lats, lons, []

    def retrieve(self, dataset, req, target):
        self.requests.append((dataset, req))
        _netcdf(target, int(req["year"]), self.lats, self.lons)


def test_soil_clim_quantiles_sampling_and_seed_db(tmp_path):
    lats, lons = [47.2, 47.1, 47.0], [6.2, 6.3, 6.4]
    grid = tmp_path / "weather_grid.json"
    grid.write_text(
        json.dumps(
            {
                "version": 1,
                "cell_m": 5000,
                "cells": [
                    {"id": "c_in", "lat": 47.12, "lon": 6.31, "alt_classes": [450]},
                    {"id": "c_sea", "lat": 47.21, "lon": 6.19, "alt_classes": [150]},  # pixel NaN
                ],
            }
        )
    )
    cds = _FakeCds(lats, lons)
    out = tmp_path / "weather_seed.db"
    n = soil_clim.build(grid, [2024, 2025], out, tmp_path / "era5", client=cds)
    assert n == 2
    assert [r["year"] for _, r in cds.requests] == ["2024", "2025"]
    assert cds.requests[0][0] == "derived-era5-land-daily-statistics"
    assert cds.requests[0][1]["variable"] == ["volumetric_soil_water_layer_1"]
    assert cds.requests[0][1]["daily_statistic"] == "daily_mean"
    db = sqlite3.connect(out)
    rows = dict(db.execute("SELECT cell_id, months_json FROM soil_clim"))
    assert set(rows) == {"c_in", "c_sea"}
    m_in = json.loads(rows["c_in"])
    assert list(m_in) == [str(m) for m in range(1, 13)] and len(m_in["1"]) == 11
    # valeur constante dans le mois → 11 quantiles égaux ; pixel (47.1, 6.3) : 1/100 + 47.1/1000
    assert m_in["1"] == pytest.approx([0.01 + 0.0471] * 11, abs=1e-6)
    assert m_in["10"] == pytest.approx([0.10 + 0.0471] * 11, abs=1e-6)
    # cellule sur un pixel « mer » : pixel valide le plus proche (47.2, 6.3)
    assert json.loads(rows["c_sea"])["1"] == pytest.approx([0.01 + 0.0472] * 11, abs=1e-6)
    assert (
        dict(db.execute("SELECT key, value FROM meta"))["seed_generated"]
        == date.today().isoformat()
    )
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
        "history",
        "soil_clim",
        "meta",
    }
    # reprise : les NetCDF présents ne sont pas retéléchargés
    soil_clim.build(grid, [2024, 2025], out, tmp_path / "era5", client=cds)
    assert len(cds.requests) == 2


def test_soil_clim_rejects_a_cell_far_from_any_valid_pixel(tmp_path):
    lats, lons = [47.2, 47.1], [6.2, 6.3]
    _netcdf(tmp_path / "a.nc", 2025, lats, lons, sea_ij=(0, 0))
    clim = soil_clim.climatology([tmp_path / "a.nc"])
    assert clim.shape == (12, 11, 2, 2)
    with pytest.raises(ValueError, match="hors de l'emprise"):
        soil_clim.sample(clim, {"far": (45.0, 2.0)})
    with pytest.raises(ValueError, match="aucun pixel"):  # tout le voisinage est en mer
        sea = clim.where(False)
        soil_clim.sample(sea, {"c": (47.2, 6.2)})
    assert soil_clim.default_years(date(2026, 10, 25)) == list(range(2016, 2026))
    assert soil_clim.request(2025)["area"] == [51.5, -5.5, 41.0, 10.0]
