"""Tests du module géométrie.

Ce sont les tests les plus importants du dépôt : une erreur ici se traduit par
un site sous-dimensionné en portée ou en revisite, et ne se voit qu'après
installation sur pylône.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from openvigie.geometry import (
    IMX675,
    IMX678,
    LENS_27135,
    angular_diff,
    bearing_range_to_latlon,
    coverage_gaps,
    estimate_visibility_m,
    flat_earth_distance_map,
    focal_for_hfov,
    ground_mask,
    ground_sample_m,
    hfov_deg,
    horizon_row,
    ifov_mrad,
    koschmieder_contrast,
    max_range_m,
    min_detectable_width_m,
    pixel_to_bearing,
    plan_adaptive_ring,
    plan_uniform_ring,
    scan_budget,
    triangulate,
)


class TestOptics:
    def test_hfov_valeurs_connues(self):
        # IMX675 : 2592 px x 2.0 µm = 5.184 mm de large
        assert IMX675.width_mm == pytest.approx(5.184, abs=1e-6)
        assert hfov_deg(IMX675, 4.8) == pytest.approx(56.7, abs=0.3)
        assert hfov_deg(IMX675, 13.5) == pytest.approx(21.7, abs=0.3)
        assert hfov_deg(IMX675, 144.0) == pytest.approx(2.06, abs=0.05)

    def test_focal_for_hfov_est_inverse_de_hfov(self):
        for target in (10.0, 25.0, 45.0, 90.0):
            f = focal_for_hfov(IMX675, target)
            assert hfov_deg(IMX675, f) == pytest.approx(target, abs=1e-6)

    def test_ifov_et_gsd(self):
        assert ifov_mrad(IMX675, 4.8) == pytest.approx(0.4167, abs=1e-3)
        # 0.4167 mrad à 10 km = 4.17 m/px
        assert ground_sample_m(IMX675, 4.8, 10_000) == pytest.approx(4.167, abs=0.02)

    def test_gsd_lineaire_en_distance(self):
        a = ground_sample_m(IMX675, 6.0, 5_000)
        b = ground_sample_m(IMX675, 6.0, 10_000)
        assert b == pytest.approx(2 * a, rel=1e-9)

    def test_panache_minimum_croit_avec_la_distance(self):
        near = min_detectable_width_m(IMX675, 6.25, 3_000)
        far = min_detectable_width_m(IMX675, 6.25, 10_000)
        assert far > near > 0
        assert far / near == pytest.approx(10 / 3, rel=1e-9)

    def test_max_range_reciproque_de_min_detectable(self):
        d = 8_000.0
        w = min_detectable_width_m(IMX675, 6.25, d)
        assert max_range_m(IMX675, 6.25, w) == pytest.approx(d, rel=1e-6)

    def test_grand_angle_insuffisant_a_10km(self):
        """Le point clé du dimensionnement : au grand-angle, un panache naissant
        de 30 m n'est pas détectable à 10 km."""
        assert min_detectable_width_m(IMX675, 4.8, 10_000) > 30.0
        # à 3x de zoom en revanche, oui
        assert min_detectable_width_m(IMX675, 14.4, 10_000) < 30.0

    def test_imx678_plus_large_mais_moins_resolvant_a_focale_max(self):
        """Justifie de réserver le 8 MP aux secteurs très lointains, pas d'en
        faire le capteur par défaut."""
        assert hfov_deg(IMX678, 11.0) > hfov_deg(IMX675, 13.5)
        assert ground_sample_m(IMX678, 11.0, 10_000) > ground_sample_m(IMX675, 13.5, 10_000)


class TestAtmosphere:
    def test_koschmieder_decroit(self):
        assert koschmieder_contrast(0, 20_000) == pytest.approx(1.0)
        c5 = koschmieder_contrast(5_000, 20_000)
        c10 = koschmieder_contrast(10_000, 20_000)
        assert 0 < c10 < c5 < 1
        assert c5 == pytest.approx(0.376, abs=0.01)
        assert c10 == pytest.approx(0.141, abs=0.01)

    def test_visibilite_estimee_est_inverse(self):
        v = 15_000.0
        d = 6_000.0
        obs = koschmieder_contrast(d, v)
        assert estimate_visibility_m(obs, 1.0, d) == pytest.approx(v, rel=1e-6)

    def test_visibilite_degradee_reduit_la_portee_utile(self):
        claire = koschmieder_contrast(10_000, 40_000)
        brumeuse = koschmieder_contrast(10_000, 10_000)
        assert claire > 5 * brumeuse


class TestPlanning:
    def test_couronne_uniforme_couvre_360(self):
        views = plan_uniform_ring(IMX675, LENS_27135, 8, 8_000)
        assert len(views) == 8
        assert coverage_gaps(views) == []
        assert views[0].hfov_deg * 8 > 360  # recouvrement effectif

    def test_azimuts_regulierement_espaces(self):
        views = plan_uniform_ring(IMX675, LENS_27135, 6, 5_000)
        az = [v.azimuth_deg for v in views]
        assert az == pytest.approx([0, 60, 120, 180, 240, 300])

    def test_moins_de_cameras_implique_champ_plus_large(self):
        v6 = plan_uniform_ring(IMX675, LENS_27135, 6, 8_000)
        v12 = plan_uniform_ring(IMX675, LENS_27135, 12, 8_000)
        assert v6[0].hfov_deg > v12[0].hfov_deg
        assert v6[0].min_plume_m > v12[0].min_plume_m

    def test_focale_saturee_par_les_limites_objectif(self):
        views = plan_uniform_ring(IMX675, LENS_27135, 40, 8_000)
        assert views[0].focal_mm == pytest.approx(LENS_27135.f_max_mm)

    def test_couronne_adaptative_donne_focale_plus_longue_aux_secteurs_lointains(self):
        views = plan_adaptive_ring(IMX675, LENS_27135, {0.0: 3_000.0, 90.0: 12_000.0}, min_plume_m=30.0)
        proche = next(v for v in views if v.azimuth_deg == 0.0)
        lointain = next(v for v in views if v.azimuth_deg == 90.0)
        assert lointain.focal_mm > proche.focal_mm

    def test_n_views_invalide(self):
        with pytest.raises(ValueError):
            plan_uniform_ring(IMX675, LENS_27135, 0, 8_000)

    def test_overlap_invalide(self):
        with pytest.raises(ValueError):
            plan_uniform_ring(IMX675, LENS_27135, 8, 8_000, overlap=0.95)


class TestScanBudget:
    def test_ptz_8_presets_2min(self):
        b = scan_budget(8, dwell_s=12.0, slew_s=3.0, is_ptz=True)
        assert b.cycle_s == pytest.approx(120.0)
        assert b.cycle_s / 60 == pytest.approx(2.0)

    def test_usure_mecanique_2_millions_par_an(self):
        """Le chiffre qui condamne le balayage PTZ continu."""
        b = scan_budget(8, dwell_s=12.0, slew_s=3.0, is_ptz=True)
        assert 2.0e6 < b.moves_per_year < 2.2e6

    def test_cameras_fixes_sans_usure(self):
        b = scan_budget(8, dwell_s=10.0, slew_s=0.0, is_ptz=False)
        assert b.moves_per_year == 0.0
        assert b.cycle_s == pytest.approx(10.0)

    def test_plus_de_presets_allonge_le_cycle(self):
        assert scan_budget(21, 12, 3).cycle_s > scan_budget(8, 12, 3).cycle_s

    def test_plancher_de_latence_est_trois_cycles(self):
        b = scan_budget(8, 12, 3)
        assert b.detection_latency_floor_s == pytest.approx(3 * b.cycle_s)


class TestGroundProjection:
    def test_horizon_au_centre_sans_tilt(self):
        assert horizon_row(IMX675, 6.0, 0.0) == pytest.approx((IMX675.height_px - 1) / 2, abs=1)

    def test_tilt_vers_le_bas_remonte_lhorizon(self):
        assert horizon_row(IMX675, 6.0, 3.0) < horizon_row(IMX675, 6.0, 0.0)

    def test_distance_infinie_au_dessus_de_lhorizon(self):
        dmap = flat_earth_distance_map(IMX675, 6.0, 40.0, tilt_deg=2.0)
        hr = horizon_row(IMX675, 6.0, 2.0)
        assert not np.isfinite(dmap[max(0, hr - 20), 0])
        assert np.isfinite(dmap[min(dmap.shape[0] - 1, hr + 50), 0])

    def test_distance_decroit_vers_le_bas(self):
        dmap = flat_earth_distance_map(IMX675, 6.0, 40.0, tilt_deg=2.0)
        col = dmap[:, 0]
        finite_rows = np.nonzero(np.isfinite(col))[0]
        vals = col[finite_rows]
        assert np.all(np.diff(vals) <= 1e-6)

    def test_ground_mask_coherent(self):
        dmap = flat_earth_distance_map(IMX675, 6.0, 40.0, tilt_deg=2.0)
        g = ground_mask(dmap)
        assert g.dtype == bool
        assert g.sum() > 0
        assert (~g).sum() > 0

    def test_camera_plus_haute_voit_plus_loin(self):
        d40 = flat_earth_distance_map(IMX675, 6.0, 40.0, tilt_deg=2.0)
        d80 = flat_earth_distance_map(IMX675, 6.0, 80.0, tilt_deg=2.0)
        row = horizon_row(IMX675, 6.0, 2.0) + 200
        assert d80[row, 0] > d40[row, 0]


class TestBearing:
    def test_centre_image_donne_azimut_de_la_vue(self):
        assert pixel_to_bearing((IMX675.width_px - 1) / 2, IMX675, 6.0, 137.0) == pytest.approx(137.0, abs=1e-6)

    def test_bord_droit_azimut_superieur(self):
        b = pixel_to_bearing(IMX675.width_px - 1, IMX675, 6.0, 0.0)
        assert 0 < b < hfov_deg(IMX675, 6.0)

    def test_angular_diff_wrap(self):
        assert angular_diff(1.0, 359.0) == pytest.approx(2.0)
        assert angular_diff(359.0, 1.0) == pytest.approx(-2.0)
        # ±180° est le même écart ; seule la valeur absolue est signifiante ici
        assert abs(angular_diff(180.0, 0.0)) == pytest.approx(180.0)
        assert abs(angular_diff(0.0, 180.0)) == pytest.approx(180.0)


class TestTriangulation:
    def test_intersection_perpendiculaire(self):
        # Tour A au sud, visant plein nord ; tour B à l'ouest, visant plein est.
        lat_a, lon_a = 44.0, 3.0
        lat_b, lon_b = 44.05, 2.95
        res = triangulate(lat_a, lon_a, 0.0, lat_b, lon_b, 90.0)
        assert res is not None
        lat, lon = res
        assert lat == pytest.approx(lat_b, abs=1e-4)
        assert lon == pytest.approx(lon_a, abs=1e-4)

    def test_releves_paralleles_sans_solution(self):
        assert triangulate(44.0, 3.0, 45.0, 44.1, 3.1, 45.0) is None

    def test_intersection_derriere_rejetee(self):
        assert triangulate(44.0, 3.0, 180.0, 44.05, 2.95, 270.0) is None

    def test_boucle_avec_bearing_range(self):
        lat, lon = bearing_range_to_latlon(44.0, 3.0, 90.0, 5_000.0)
        assert lat == pytest.approx(44.0, abs=1e-6)
        assert lon > 3.0
        d = math.hypot((lat - 44.0) * 111_132, (lon - 3.0) * 111_320 * math.cos(math.radians(44)))
        assert d == pytest.approx(5_000, rel=1e-3)
