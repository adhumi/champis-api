from __future__ import annotations

import numpy as np
import pytest
from champis_pipeline.scoring import NODATA_U8, NODATA_U16, decode_u8, encode_u8, terrain_potential
from champis_pipeline.species import ASPECT_CLASSES, COVER_CLASSES, host_index, load_species

EDULIS = load_species("boletus_edulis")
AEREUS = load_species("boletus_aereus")
PALLENS = load_species("cantharellus_pallens")


def pixel(
    host: str,
    cover: str = "fermee",
    ph: float = 5.0,
    alt: int = 800,
    slope: int = 10,
    aspect: str = "N",
):
    """Un pixel 1×1 avec des codes raster."""
    return (
        np.array([[host_index(host)]], dtype=np.uint8)
        if host != "nodata"
        else np.array([[NODATA_U8]], dtype=np.uint8),
        np.array([[COVER_CLASSES.index(cover)]], dtype=np.uint8),
        np.array([[round(ph * 10)]], dtype=np.uint8)
        if ph is not None
        else np.array([[NODATA_U8]], dtype=np.uint8),
        np.array([[alt]], dtype=np.uint16),
        np.array([[slope]], dtype=np.uint8),
        np.array([[ASPECT_CLASSES.index(aspect)]], dtype=np.uint8),
    )


def T(species, **kw) -> float:  # noqa: N802
    return float(terrain_potential(species, *pixel(**kw))[0, 0])


def test_hetraie_ubac_is_excellent_for_edulis():
    assert T(EDULIS, host="hetre", ph=5.5, alt=1100, aspect="N") > 0.9


def test_vineyard_and_cedar_are_not_good():
    assert T(EDULIS, host="herbacee") == 0.0
    assert T(PALLENS, host="herbacee", ph=7.8) == 0.0
    assert (
        T(PALLENS, host="autre_conifere", ph=7.5, alt=600) < 0.1
    )  # cédraie : hôte 0,05 multiplicatif
    assert T(AEREUS, host="autre_conifere", ph=7.5, alt=600) < 0.15


def test_outside_forest_nodata_is_zero():
    assert T(EDULIS, host="nodata") == 0.0


def test_ph_floor_keeps_calcareous_beech_alive():
    # pH SoilGrids 7,8 sur hêtraie : facteur pH plancher 0,15, pas 0
    assert T(EDULIS, host="hetre", ph=7.8, alt=1100) > 0.6


def test_ph_missing_is_neutral():
    assert T(EDULIS, host="hetre", ph=None, alt=1100) == pytest.approx(
        T(EDULIS, host="hetre", ph=4.8, alt=1100) - 0.3 * 0.5, abs=0.02
    )


def test_altitude_hard_rule():
    assert T(AEREUS, host="chenes_decidus", ph=6.0, alt=1500) == 0.0


def test_altitude_missing_no_hard_rule():
    host, cover, ph, _, slope, aspect = pixel(host="hetre", ph=5.0)
    alt = np.array([[NODATA_U16]], dtype=np.uint16)
    assert terrain_potential(EDULIS, host, cover, ph, alt, slope, aspect)[0, 0] > 0.5


def test_encode_decode_roundtrip():
    t = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
    enc = encode_u8(t, nodata_mask=np.array([[False, False, True]]))
    assert enc.tolist() == [[0, 127, 255]]
    dec = decode_u8(enc)
    assert dec[0, 0] == 0.0 and abs(dec[0, 1] - 0.5) < 0.004 and np.isnan(dec[0, 2])


def test_vectorised_block_matches_scalar():
    rng = np.random.default_rng(0)
    n = 64
    host = rng.integers(0, 29, (n, n)).astype(np.uint8)
    cover = rng.integers(0, 5, (n, n)).astype(np.uint8)
    ph = rng.integers(30, 85, (n, n)).astype(np.uint8)
    alt = rng.integers(0, 2000, (n, n)).astype(np.uint16)
    slope = rng.integers(0, 45, (n, n)).astype(np.uint8)
    aspect = rng.integers(0, 9, (n, n)).astype(np.uint8)
    block = terrain_potential(EDULIS, host, cover, ph, alt, slope, aspect)
    for i, j in [(0, 0), (5, 7), (63, 63)]:
        one = terrain_potential(
            EDULIS,
            host[i : i + 1, j : j + 1],
            cover[i : i + 1, j : j + 1],
            ph[i : i + 1, j : j + 1],
            alt[i : i + 1, j : j + 1],
            slope[i : i + 1, j : j + 1],
            aspect[i : i + 1, j : j + 1],
        )
        assert block[i, j] == one[0, 0]


def test_altitude_hard_rule_bounds_inclusive():
    lo = EDULIS["terrain"]["altitude_m"]["min"]
    hi = EDULIS["terrain"]["altitude_m"]["max"]
    assert T(EDULIS, host="hetre", alt=lo) > 0.0  # borne incluse : pas de règle dure
    assert T(EDULIS, host="hetre", alt=hi) > 0.0
    assert T(EDULIS, host="hetre", alt=hi + 1) == 0.0
