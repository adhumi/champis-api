"""Interpolation de la météo entre cellules (spec › Scoring, 13 nov. 2026)."""

from __future__ import annotations

import pytest
from champis_api import interp
from pyproj import Transformer


def test_lambert93_matches_pyproj():
    to_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    for lat, lon in [(47.12, 6.31), (42.5, -1.2), (50.9, 2.0), (43.7, 7.3), (48.4, -4.5)]:
        x, y = interp.lambert93(lat, lon)
        ex, ey = to_l93.transform(lon, lat)
        assert x == pytest.approx(ex, abs=1e-6) and y == pytest.approx(ey, abs=1e-6)


def _at(x: float, y: float) -> tuple[float, float]:
    lon, lat = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform(x, y)
    return lat, lon


def test_corners_and_weights():
    # 1 km à l'est et 2 km au nord du centre de c_0930_6680 (932 500, 6 682 500)
    c = interp.corners(*_at(933_500, 6_684_500))
    assert c.containing == "c_0930_6680"
    assert c.ids == ("c_0930_6680", "c_0935_6680", "c_0930_6685", "c_0935_6685")
    assert c.weights == pytest.approx((0.8 * 0.6, 0.2 * 0.6, 0.8 * 0.4, 0.2 * 0.4), abs=1e-6)
    assert sum(c.weights) == pytest.approx(1.0)
    # au sud-ouest du centre : la cellule qui contient le point est le coin NE
    c = interp.corners(*_at(931_000, 6_681_000))
    assert c.containing == "c_0930_6680" and c.ids[3] == "c_0930_6680"


def test_value_at_center_is_the_cell_value():
    c = interp.corners(*_at(932_500.001, 6_682_500.001))
    values = dict(zip(c.ids, (0.8, 0.1, 0.4, 0.6), strict=True))
    assert interp.interpolate(c, values) == pytest.approx(0.8, abs=1e-6)


def test_continuous_across_a_cell_edge():
    west = interp.corners(*_at(934_999.9, 6_683_000))
    east = interp.corners(*_at(935_000.1, 6_683_000))
    assert west.containing == "c_0930_6680" and east.containing == "c_0935_6680"
    values = {"c_0930_6680": 0.9, "c_0935_6680": 0.1, "c_0930_6685": 0.5, "c_0935_6685": 0.3}
    a, b = interp.interpolate(west, values), interp.interpolate(east, values)
    assert a == pytest.approx(b, abs=1e-4)
    assert a == pytest.approx(
        0.9 * 0.5 * 0.9 + 0.1 * 0.5 * 0.9 + 0.5 * 0.5 * 0.1 + 0.3 * 0.5 * 0.1, abs=1e-4
    )


def test_missing_neighbours_are_renormalised_and_flat_at_the_edge():
    c = interp.corners(*_at(934_000, 6_683_000))
    assert interp.interpolate(c, {"c_0930_6680": 0.7}) == pytest.approx(0.7)
    got = interp.interpolate(c, {"c_0930_6680": 0.7, "c_0930_6685": 0.3})
    assert got == pytest.approx(0.7 * 0.9 + 0.3 * 0.1)  # les deux coins est sont ignorés


def test_no_value_when_the_containing_cell_has_none():
    c = interp.corners(*_at(934_000, 6_683_000))
    assert interp.interpolate(c, {"c_0935_6680": 0.9, "c_0930_6685": 0.3}) is None
    assert interp.interpolate(c, {"c_0930_6680": None, "c_0935_6680": 0.9}) is None
    assert interp.interpolate(c, {}) is None
