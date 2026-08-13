"""Tests du géoréférencement par MNT.

Le passage du modèle terre plate au ray-casting réel est ce qui transforme
« fumée à droite » en coordonnées exploitables. Deux effets se testent ici : la
distance devient juste, et la ligne d'horizon suit les crêtes — donc le veto
« origine au sol » devient nettement plus discriminant en relief.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from openvigie.config import tier_defaults
from openvigie.dem import (
    DEM,
    EFFECTIVE_EARTH_RADIUS_M,
    GeoTransform,
    curvature_drop_m,
    distance_map_from_dem,
    fill_distance_gaps,
    intersect_ground,
    synthetic_dem,
    terrain_profile,
    viewshed_ranges,
)
from openvigie.detectors import get_detector
from openvigie.pipeline import DetectionPipeline, view_maps_from_dem
from openvigie.sources import SyntheticScene, SyntheticSource


def flat_dem(size: int = 120, elevation_m: float = 500.0, span_m: float = 12_000.0) -> DEM:
    """MNT parfaitement plat : la référence analytique pour vérifier les calculs."""
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(44.0))
    px = (span_m / size) / m_per_deg_lon
    py = -(span_m / size) / 111_132.0
    return DEM(
        elevation=np.full((size, size), elevation_m),
        transform=GeoTransform(
            origin_x=3.0 - px * (size - 1) / 2,
            origin_y=44.0 - py * (size - 1) / 2,
            pixel_x=px, pixel_y=py, crs="geographic",
        ),
    )


class TestGeoTransform:
    def test_aller_retour_pixel_monde(self):
        gt = GeoTransform(3.0, 44.0, 0.001, -0.001)
        col, row = gt.to_pixel(3.005, 43.997)
        assert (col, row) == pytest.approx((5.0, 3.0))
        assert gt.to_world(5.0, 3.0) == pytest.approx((3.005, 43.997))

    def test_crs_invalide(self):
        with pytest.raises(ValueError):
            GeoTransform(0, 0, 1, -1, crs="lambert")

    def test_pas_nul_refuse(self):
        with pytest.raises(ValueError):
            GeoTransform(0, 0, 0, -1)

    def test_metres_par_unite_geographique(self):
        gt = GeoTransform(3.0, 44.0, 0.001, -0.001)
        mx, my = gt.meters_per_unit(44.0)
        assert mx == pytest.approx(111_320 * math.cos(math.radians(44)), rel=1e-6)
        assert my == pytest.approx(111_132.0)

    def test_projete_est_deja_en_metres(self):
        assert GeoTransform(0, 0, 1, -1, crs="projected").meters_per_unit(44.0) == (1.0, 1.0)


class TestDEM:
    def test_altitude_interpolee(self):
        dem = flat_dem(elevation_m=750.0)
        assert dem.elevation_at(3.0, 44.0) == pytest.approx(750.0)

    def test_hors_emprise_renvoie_nan(self):
        assert math.isnan(flat_dem().elevation_at(10.0, 40.0))

    def test_grille_non_2d_refusee(self):
        with pytest.raises(ValueError):
            DEM(elevation=np.zeros((3, 3, 3)), transform=GeoTransform(0, 0, 1, -1))

    def test_nodata_devient_nan(self):
        dem = flat_dem()
        dem.elevation[:] = -9999.0
        dem.nodata = -9999.0
        assert math.isnan(dem.elevation_at(3.0, 44.0))

    def test_sauvegarde_et_rechargement(self, tmp_path):
        dem = synthetic_dem(size=40)
        path = tmp_path / "mnt.npy"
        dem.save(path)
        assert path.with_suffix(".json").exists()
        reloaded = DEM.from_npy(path)
        np.testing.assert_allclose(reloaded.elevation, dem.elevation)
        assert reloaded.transform.crs == dem.transform.crs


class TestCurvature:
    def test_correction_a_dix_km(self):
        """~6,7 m à 10 km avec la réfraction standard : sur terrain plat, c'est
        la différence entre voir la base d'un panache et la croire en l'air."""
        assert curvature_drop_m(10_000.0) == pytest.approx(6.7, abs=0.3)

    def test_croissance_quadratique(self):
        assert curvature_drop_m(20_000.0) == pytest.approx(4 * curvature_drop_m(10_000.0), rel=1e-9)

    def test_rayon_effectif_superieur_au_rayon_reel(self):
        assert EFFECTIVE_EARTH_RADIUS_M > 6_371_000.0


class TestProfile:
    def test_terrain_plat_tout_visible(self):
        p = terrain_profile(flat_dem(), 44.0, 3.0, 40.0, 90.0, max_distance_m=5_000, step_m=50)
        assert p.visible.sum() > 0.9 * np.isfinite(p.elevations_m).sum()

    def test_depression_decroit_avec_la_distance(self):
        p = terrain_profile(flat_dem(), 44.0, 3.0, 40.0, 90.0, max_distance_m=5_000, step_m=50)
        dep = p.depression_deg[np.isfinite(p.depression_deg)]
        assert np.all(np.diff(dep) < 1e-9)

    def test_depression_analytique_sur_terrain_plat(self):
        """Vérification chiffrée : à 2 km avec 40 m de mât, la dépression vaut
        atan(40/2000) ≈ 1,146°, moins la courbure."""
        p = terrain_profile(flat_dem(), 44.0, 3.0, 40.0, 90.0, max_distance_m=3_000, step_m=10)
        i = int(np.argmin(np.abs(p.distances_m - 2_000)))
        attendu = math.degrees(math.atan((40.0 + curvature_drop_m(2_000.0)) / 2_000.0))
        assert p.depression_deg[i] == pytest.approx(attendu, abs=0.02)

    def test_camera_hors_emprise(self):
        with pytest.raises(ValueError, match="hors de l'emprise"):
            terrain_profile(flat_dem(), 10.0, 20.0, 40.0, 0.0)

    def test_pas_invalide(self):
        with pytest.raises(ValueError):
            terrain_profile(flat_dem(), 44.0, 3.0, 40.0, 0.0, step_m=0)

    def test_relief_limite_la_portee(self):
        """Un MNT accidenté masque des secteurs : c'est tout l'intérêt du calcul."""
        p = terrain_profile(synthetic_dem(), 44.0, 3.0, 40.0, 45.0, max_distance_m=20_000, step_m=50)
        assert p.max_visible_distance_m < 20_000

    def test_mat_plus_haut_voit_plus_loin(self):
        dem = synthetic_dem()
        court = terrain_profile(dem, 44.0, 3.0, 10.0, 45.0, step_m=50).max_visible_distance_m
        haut = terrain_profile(dem, 44.0, 3.0, 120.0, 45.0, step_m=50).max_visible_distance_m
        assert haut >= court


class TestViewshed:
    def test_secteurs_inegaux_en_relief(self):
        ranges = viewshed_ranges(synthetic_dem(), 44.0, 3.0, 40.0, n_sectors=12, step_m=100)
        assert len(ranges) == 12
        assert max(ranges.values()) > 3 * min(ranges.values())

    def test_terrain_plat_homogene(self):
        ranges = viewshed_ranges(flat_dem(), 44.0, 3.0, 40.0, n_sectors=8, max_distance_m=5_000, step_m=100)
        values = list(ranges.values())
        assert max(values) - min(values) < 0.2 * max(values)

    def test_n_sectors_invalide(self):
        with pytest.raises(ValueError):
            viewshed_ranges(flat_dem(), 44.0, 3.0, 40.0, n_sectors=0)

    def test_alimente_la_planification_adaptative(self):
        """Le viewshed sert directement à choisir les focales par secteur."""
        from openvigie.geometry import IMX675, LENS_27135, plan_adaptive_ring

        ranges = viewshed_ranges(synthetic_dem(), 44.0, 3.0, 40.0, n_sectors=8, step_m=100)
        views = plan_adaptive_ring(IMX675, LENS_27135, ranges, min_plume_m=30.0)
        assert len(views) == 8
        loin = max(views, key=lambda v: v.target_range_m)
        pres = min(views, key=lambda v: v.target_range_m)
        assert loin.focal_mm >= pres.focal_mm


class TestDistanceMap:
    def test_carte_coherente_sur_terrain_plat(self):
        dmap, horizon = distance_map_from_dem(
            flat_dem(), 44.0, 3.0, 40.0, 90.0, 30.0, 22.0, 60, 40,
            max_distance_m=6_000, step_m=25,
        )
        assert dmap.shape == (40, 60)
        assert np.isfinite(dmap).any()
        assert (horizon >= 0).any()

    def test_distance_decroit_vers_le_bas_de_limage(self):
        dmap, _ = distance_map_from_dem(
            flat_dem(), 44.0, 3.0, 40.0, 90.0, 30.0, 22.0, 40, 40,
            max_distance_m=6_000, step_m=25,
        )
        dmap = fill_distance_gaps(dmap)
        col = dmap[:, 20]
        finite = col[np.isfinite(col)]
        assert finite.size > 5
        assert finite[0] > finite[-1]

    def test_ciel_reste_infini(self):
        dmap, horizon = distance_map_from_dem(
            flat_dem(), 44.0, 3.0, 40.0, 90.0, 30.0, 22.0, 40, 40,
            max_distance_m=6_000, step_m=25,
        )
        hr = int(horizon[20])
        assert not np.isfinite(dmap[max(0, hr - 5), 20])

    def test_horizon_suit_les_cretes_en_relief(self):
        """Sur un relief, la ligne d'horizon n'est pas une horizontale."""
        _, horizon = distance_map_from_dem(
            synthetic_dem(), 44.0, 3.0, 40.0, 200.0, 40.0, 30.0, 60, 40,
            max_distance_m=15_000, step_m=100,
        )
        valid = horizon[horizon >= 0]
        assert valid.size > 10
        assert valid.max() > valid.min()

    def test_remplissage_ne_deborde_pas_au_dessus_de_lhorizon(self):
        dmap, horizon = distance_map_from_dem(
            flat_dem(), 44.0, 3.0, 40.0, 90.0, 30.0, 22.0, 30, 40,
            max_distance_m=6_000, step_m=25,
        )
        filled = fill_distance_gaps(dmap)
        for col in range(filled.shape[1]):
            hr = int(horizon[col])
            if hr > 0:
                assert not np.isfinite(filled[: max(hr - 1, 0), col]).any()

    def test_dimensions_invalides(self):
        with pytest.raises(ValueError):
            distance_map_from_dem(flat_dem(), 44.0, 3.0, 40.0, 0.0, 30.0, 22.0, 0, 10)


class TestIntersection:
    def test_intersection_sur_terrain_plat_verifiable(self):
        """À 1,146° de dépression et 40 m de mât, on doit tomber vers 2 km."""
        r = intersect_ground(flat_dem(), 44.0, 3.0, 40.0, 90.0, 1.146,
                             max_distance_m=6_000, step_m=10)
        assert r is not None
        assert r["distance_m"] == pytest.approx(2_000, rel=0.1)
        assert r["longitude"] > 3.0
        assert r["latitude"] == pytest.approx(44.0, abs=1e-4)

    def test_vise_au_dessus_de_lhorizon_rejetee(self):
        """Le test qui distingue un nuage d'un panache."""
        assert intersect_ground(flat_dem(), 44.0, 3.0, 40.0, 90.0, -2.0) is None

    def test_depression_hors_du_terrain_visible(self):
        assert intersect_ground(synthetic_dem(), 44.0, 3.0, 40.0, 45.0, 0.01,
                                tolerance_deg=0.2, step_m=50) is None

    def test_azimut_respecte(self):
        nord = intersect_ground(flat_dem(), 44.0, 3.0, 40.0, 0.0, 1.146, step_m=10)
        est = intersect_ground(flat_dem(), 44.0, 3.0, 40.0, 90.0, 1.146, step_m=10)
        assert nord["latitude"] > 44.0 and nord["longitude"] == pytest.approx(3.0, abs=1e-6)
        assert est["longitude"] > 3.0 and est["latitude"] == pytest.approx(44.0, abs=1e-4)

    def test_distance_croit_quand_la_visee_se_releve(self):
        proche = intersect_ground(flat_dem(), 44.0, 3.0, 40.0, 90.0, 2.3, step_m=10)
        loin = intersect_ground(flat_dem(), 44.0, 3.0, 40.0, 90.0, 0.6, step_m=10)
        assert loin["distance_m"] > proche["distance_m"]


class TestPipelineWithDem:
    def _run(self, mode: str):
        cfg = tier_defaults("full")
        cfg.operating.mode = "shadow"
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"))
        dmap, horizon = view_maps_from_dem(
            synthetic_dem(), cfg, 225.0, cfg.optics.focal_mm, 240, 140, step_m=100,
        )
        pipe.register_view("V00", 225.0, cfg.optics.focal_mm,
                           distance_map=dmap, horizon_rows=horizon)
        hr = int(horizon[horizon >= 0].min())
        scene = SyntheticScene(height=140, width=240, horizon_row=max(10, min(110, hr)))
        src = SyntheticSource(scene=scene, mode=mode, n_background=6, n_plume=8)
        t = 0.0
        while True:
            item = src.read()
            if item is None:
                break
            pipe.process_frame("V00", item[0], item[1], t_monotonic=t)
            t += 30.0
        return pipe

    def test_le_pipeline_signale_utiliser_le_mnt(self):
        assert self._run("plume").summary()["geolocation"] == "dem"

    def test_panache_alerte_avec_coordonnees(self):
        pipe = self._run("plume")
        assert pipe.stats["alerts"] >= 1
        alert = pipe.alerts.emitted[0]
        assert alert.latitude is not None and alert.longitude is not None
        assert alert.localization == "dem_intersect"

    def test_evenement_produit_avec_incertitude(self):
        pipe = self._run("plume")
        event = pipe.events[0]
        assert event.state == "confirmed"
        assert event.uncertainty and event.uncertainty["semi_major_m"] > 0
        assert event.tower_votes == [pipe.cfg.site_id]

    def test_nuage_toujours_rejete_avec_un_mnt(self):
        assert self._run("cloud").stats["alerts"] == 0
