from __future__ import annotations

import numpy as np
import pytest

from openvigie.config import tier_defaults
from openvigie.geometry import horizon_row
from openvigie.sources import SyntheticScene, SyntheticSource


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(1234)


@pytest.fixture(params=["minimal", "medium", "full"])
def any_tier_cfg(request):
    """Chaque test structurel tourne sur les trois tiers : c'est la garantie
    qu'un site MINIMAL est promouvable en FULL sans réécriture."""
    return tier_defaults(request.param)


@pytest.fixture
def scene_for(any_tier_cfg):
    """Scène synthétique dont l'horizon coïncide avec le modèle géométrique."""

    def _build(cfg=None, height: int = 180, width: int = 320) -> SyntheticScene:
        cfg = cfg or any_tier_cfg
        sensor = cfg.optics.sensor_spec()
        hr = int(round(horizon_row(sensor, cfg.optics.focal_mm, cfg.optics.tilt_deg) * height / sensor.height_px))
        return SyntheticScene(height=height, width=width, horizon_row=max(10, min(height - 30, hr)))

    return _build


@pytest.fixture
def plume_sequence(scene_for):
    scene = scene_for()
    return SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8), scene


@pytest.fixture
def cloud_sequence(scene_for):
    scene = scene_for()
    return SyntheticSource(scene=scene, mode="cloud", n_background=6, n_plume=8), scene
