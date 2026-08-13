"""Tests de l'étalonnage géométrique par trafic aérien.

L'enjeu de ce module est particulier : il ne détecte rien, il **affirme des
chiffres** — un azimut, une assiette, une incertitude — qui se propagent ensuite
dans toutes les alertes du site. Un étalonnage silencieusement faux est donc
plus dangereux qu'un étalonnage absent. Les tests portent en conséquence autant
sur les cas où le module doit **refuser de conclure** que sur ceux où il doit
retrouver la pose.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from openvigie.calibration import (
    AircraftState,
    AircraftTrack,
    CalibrationResult,
    CameraPose,
    Site,
    StaticAdsbSource,
    apparent_elevation_deg,
    associate,
    calibrate,
    check_drift,
    detect_sky_points,
    fit_pose,
    great_circle,
    identifiability,
    look_angles,
    predict_pixel,
    synthesize_observations,
    synthesize_traffic,
)
from openvigie.cli import main
from openvigie.config import CalibrationConfig, tier_defaults
from openvigie.geometry import IMX675

SITE = Site(latitude=44.0, longitude=3.0, altitude_m=500.0, height_m=40.0)
POSE_KW = {"sensor": IMX675, "width_px": 1296, "height_px": 972}
TRUE = {"yaw_deg": 87.3, "pitch_deg": -1.42, "roll_deg": 0.6, "focal_mm": 5.31}
GUESS = {"yaw_deg": 90.0, "pitch_deg": 0.0, "roll_deg": 0.0, "focal_mm": 5.20}
FIT = ("yaw_deg", "pitch_deg", "roll_deg", "focal_mm")


def true_pose() -> CameraPose:
    return CameraPose(**TRUE, **POSE_KW)


def guess_pose(**over) -> CameraPose:
    return CameraPose(**{**GUESS, **over}, **POSE_KW)


def scenario(n_aircraft=25, noise_px=1.5, outliers=5, clock_error=0.0, altitude_offset_m=0.0,
             heading_spread=180.0, geometric=True, seed=3):
    tracks = synthesize_traffic(
        SITE, n_aircraft=n_aircraft, heading_spread_deg=heading_spread,
        geometric_altitude=geometric, seed=seed,
    )
    obs = synthesize_observations(
        SITE, tracks, true_pose(), noise_px=noise_px, clock_offset_s=clock_error,
        altitude_offset_m=altitude_offset_m, n_outliers=outliers, seed=seed + 8,
    )
    return tracks, obs


# --------------------------------------------------------------------------- #
class TestCameraModel:
    def test_axe_optique_au_centre(self):
        p = guess_pose()
        col, row = p.project(p.yaw_deg, p.pitch_deg)
        assert (col, row) == pytest.approx(p.center, abs=1e-6)

    def test_azimut_croissant_va_vers_la_droite(self):
        p = CameraPose(yaw_deg=90, pitch_deg=0, **POSE_KW, focal_mm=5.2)
        assert p.project(95, 0)[0] > p.center[0]
        assert p.project(85, 0)[0] < p.center[0]

    def test_elevation_croissante_monte_dans_limage(self):
        """L'axe des lignes descend : ce signe est la source d'erreur classique."""
        p = CameraPose(yaw_deg=90, pitch_deg=0, **POSE_KW, focal_mm=5.2)
        assert p.project(90, 5)[1] < p.center[1]
        assert p.project(90, -5)[1] > p.center[1]

    def test_aller_retour_projection(self):
        p = CameraPose(yaw_deg=137.0, pitch_deg=-1.5, roll_deg=2.0, focal_mm=6.1, **POSE_KW)
        for az, el in ((137.0, 0.0), (150.0, 8.0), (125.0, -3.0)):
            assert p.unproject(*p.project(az, el)) == pytest.approx((az, el), abs=1e-6)

    def test_direction_derriere_la_camera(self):
        assert CameraPose(yaw_deg=90, **POSE_KW, focal_mm=5.2).project(270, 0) is None

    def test_roulis_fait_tourner_limage(self):
        droit = CameraPose(yaw_deg=90, roll_deg=0, focal_mm=5.2, **POSE_KW)
        penche = CameraPose(yaw_deg=90, roll_deg=10, focal_mm=5.2, **POSE_KW)
        # un point à droite de l'axe change de ligne quand la caméra roule
        assert droit.project(100, 0)[1] != pytest.approx(penche.project(100, 0)[1], abs=1.0)

    def test_focale_plus_longue_ecarte_du_centre(self):
        court = CameraPose(yaw_deg=90, focal_mm=5.0, **POSE_KW)
        long_ = CameraPose(yaw_deg=90, focal_mm=10.0, **POSE_KW)
        cx = court.center[0]
        assert (long_.project(95, 0)[0] - cx) > (court.project(95, 0)[0] - cx)

    def test_from_tilt_inverse_le_signe(self):
        """La configuration exprime un tilt positif vers le bas, la pose un
        pitch positif vers le haut."""
        p = CameraPose.from_tilt(90.0, tilt_deg=1.5, focal_mm=5.2, **POSE_KW)
        assert p.pitch_deg == -1.5
        assert p.tilt_deg == 1.5

    def test_sous_echantillonnage_pris_en_compte(self):
        pleine = CameraPose(yaw_deg=90, focal_mm=5.2, sensor=IMX675, width_px=2592, height_px=1944)
        reduite = CameraPose(yaw_deg=90, focal_mm=5.2, sensor=IMX675, width_px=648, height_px=486)
        assert reduite.pixel_mm == pytest.approx(4 * pleine.pixel_mm)
        # même direction -> même position relative dans l'image
        assert pleine.project(95, 0)[0] / 2592 == pytest.approx(reduite.project(95, 0)[0] / 648, rel=1e-3)

    def test_dimensions_invalides(self):
        with pytest.raises(ValueError):
            CameraPose(yaw_deg=0, width_px=0, height_px=0)

    def test_focale_invalide(self):
        with pytest.raises(ValueError):
            CameraPose(yaw_deg=0, focal_mm=0, **POSE_KW)

    def test_bearing_of_column(self):
        p = CameraPose(yaw_deg=90, focal_mm=5.2, **POSE_KW)
        assert p.bearing_of_column(p.center[0]) == pytest.approx(90.0, abs=1e-6)
        assert p.bearing_of_column(p.width_px - 1) > 90.0


class TestGeodesy:
    def test_orthodromie_nord_sud(self):
        d, b = great_circle(44.0, 3.0, 44.1, 3.0)
        assert d == pytest.approx(11_119, rel=0.01)
        assert b == pytest.approx(0.0, abs=1e-6)

    def test_convergence_des_meridiens(self):
        """À 80 km vers l'est, l'azimut orthodromique n'est pas 90° : le calcul
        en plan tangent fausserait l'étalonnage de 0,35°, soit dix fois la
        précision visée."""
        _, b = great_circle(44.0, 3.0, 44.0, 4.0)
        assert b == pytest.approx(89.65, abs=0.02)
        assert abs(b - 90.0) > 0.3

    def test_distance_symetrique(self):
        d1, _ = great_circle(44.0, 3.0, 44.5, 3.5)
        d2, _ = great_circle(44.5, 3.5, 44.0, 3.0)
        assert d1 == pytest.approx(d2, rel=1e-9)

    def test_elevation_avec_courbure(self):
        """Un avion de croisière est dans la partie haute du champ : c'est ce qui
        rend la méthode possible."""
        assert apparent_elevation_deg(10_668, 540, 30_000) == pytest.approx(18.5, abs=0.2)
        assert apparent_elevation_deg(10_668, 540, 100_000) == pytest.approx(5.4, abs=0.2)

    def test_courbure_abaisse_les_cibles_lointaines(self):
        sans = math.degrees(math.atan2(10_668 - 540, 100_000))
        avec = apparent_elevation_deg(10_668, 540, 100_000)
        assert avec < sans

    def test_look_angles(self):
        state = AircraftState("abc", 0.0, 44.3, 3.0, 10_668.0)
        az, el, d = look_angles(SITE, state)
        assert az == pytest.approx(0.0, abs=0.1)
        assert d == pytest.approx(33_350, rel=0.02)
        assert 15 < el < 20


class TestTrackInterpolation:
    def _track(self) -> AircraftTrack:
        return AircraftTrack("abc", [
            AircraftState("abc", 100.0, 44.0, 3.0, 10_000.0, track_deg=90.0),
            AircraftState("abc", 110.0, 44.0, 3.1, 10_000.0, track_deg=90.0),
        ])

    def test_valeur_exacte(self):
        assert self._track().state_at(100.0).longitude == pytest.approx(3.0)

    def test_interpolation_lineaire(self):
        assert self._track().state_at(105.0).longitude == pytest.approx(3.05)

    def test_pas_dextrapolation(self):
        """Extrapoler pendant un virage produirait une mire fausse — et une mire
        fausse est pire qu'une mire absente."""
        assert self._track().state_at(95.0) is None
        assert self._track().state_at(200.0) is None

    def test_trou_de_donnees_refuse(self):
        t = AircraftTrack("abc", [
            AircraftState("abc", 0.0, 44.0, 3.0, 10_000.0),
            AircraftState("abc", 300.0, 44.0, 3.5, 10_000.0),
        ])
        assert t.state_at(150.0, max_gap_s=30.0) is None
        assert t.state_at(150.0, max_gap_s=600.0) is not None

    def test_trace_vide(self):
        assert AircraftTrack("abc", []).state_at(0.0) is None

    def test_etats_tries_automatiquement(self):
        t = AircraftTrack("abc", [
            AircraftState("abc", 110.0, 44.0, 3.1, 10_000.0),
            AircraftState("abc", 100.0, 44.0, 3.0, 10_000.0),
        ])
        assert t.times == [100.0, 110.0]

    def test_fraction_altitude_geometrique(self):
        t = AircraftTrack("abc", [
            AircraftState("abc", 0.0, 44.0, 3.0, 10_000.0, geometric_altitude=True),
            AircraftState("abc", 10.0, 44.0, 3.1, 10_000.0, geometric_altitude=False),
        ])
        assert t.geometric_fraction == 0.5


class TestAssociation:
    def test_appariement_correct(self):
        tracks, obs = scenario(n_aircraft=10, outliers=0, seed=5)
        corr = associate(SITE, obs, tracks, true_pose(), gate_px=30)
        assert len(corr) > 5
        assert all(c.residual_px <= 30 for c in corr)

    def test_fenetre_trop_serree_ne_trouve_rien(self):
        tracks, obs = scenario(n_aircraft=10, outliers=0, seed=5)
        assert associate(SITE, obs, tracks, guess_pose(), gate_px=1.0) == []

    def test_fausses_detections_non_appariees(self):
        tracks, obs = scenario(n_aircraft=5, outliers=40, seed=5)
        corr = associate(SITE, obs, tracks, true_pose(), gate_px=15)
        assert len(corr) < len(obs)

    def test_ambiguite_ecartee(self):
        """Deux aéronefs superposés : mieux vaut perdre l'observation que
        l'attribuer au mauvais."""
        base = AircraftState("aaa", 1000.0, 44.3, 3.0, 10_668.0, track_deg=90.0)
        jumeau = AircraftState("bbb", 1000.0, 44.3001, 3.0, 10_668.0, track_deg=90.0)
        tracks = {
            "aaa": AircraftTrack("aaa", [base, AircraftState("aaa", 1010.0, 44.3, 3.02, 10_668.0)]),
            "bbb": AircraftTrack("bbb", [jumeau, AircraftState("bbb", 1010.0, 44.3001, 3.02, 10_668.0)]),
        }
        pose = CameraPose(yaw_deg=0.0, pitch_deg=8.0, focal_mm=5.2, **POSE_KW)
        got = predict_pixel(SITE, tracks["aaa"], pose, 1000.0)
        assert got is not None
        from openvigie.calibration import SkyObservation

        obs = [SkyObservation(t=1000.0, col=got[0][0], row=got[0][1])]
        assert associate(SITE, obs, tracks, pose, gate_px=200, require_unambiguous=True) == []
        assert len(associate(SITE, obs, tracks, pose, gate_px=200, require_unambiguous=False)) == 1

    def test_aeronefs_bien_separes_restent_appariables(self):
        """Le critère d'ambiguïté ne doit pas jeter des appariements francs :
        deux avions séparés de plusieurs dizaines de pixels ne posent pas de
        problème."""
        a = [AircraftState("aaa", t, 44.3, 3.0 + 0.002 * i, 10_668.0, track_deg=90.0)
             for i, t in enumerate((1000.0, 1010.0))]
        b = [AircraftState("bbb", t, 44.9, 3.0 + 0.002 * i, 10_668.0, track_deg=90.0)
             for i, t in enumerate((1000.0, 1010.0))]
        tracks = {"aaa": AircraftTrack("aaa", a), "bbb": AircraftTrack("bbb", b)}
        pose = CameraPose(yaw_deg=0.0, pitch_deg=8.0, focal_mm=5.2, **POSE_KW)
        from openvigie.calibration import SkyObservation

        (col, row), *_ = predict_pixel(SITE, tracks["aaa"], pose, 1000.0)
        got = associate(SITE, [SkyObservation(t=1000.0, col=col, row=row)], tracks, pose, gate_px=400)
        assert len(got) == 1 and got[0].icao24 == "aaa"

    def test_elevation_minimale_respectee(self):
        tracks, obs = scenario(n_aircraft=15, outliers=0, seed=5)
        corr = associate(SITE, obs, tracks, true_pose(), gate_px=30, min_elevation_deg=10.0)
        assert all(c.elevation_deg >= 10.0 for c in corr)


class TestIdentifiability:
    def test_diversite_de_cap_requise_pour_lhorloge(self):
        """Sur un couloir unique, un retard d'horloge est indiscernable d'une
        erreur d'azimut : le paramètre ne doit pas être ajusté."""
        tracks, obs = scenario(n_aircraft=25, heading_spread=8.0, outliers=0, seed=5)
        ident = identifiability(associate(SITE, obs, tracks, true_pose(), gate_px=60))
        assert ident["heading_spread_deg"] < 10.0
        assert not ident["can_fit"]["clock_offset_s"]

    def test_caps_varies_autorisent_lhorloge(self):
        tracks, obs = scenario(n_aircraft=30, heading_spread=180.0, outliers=0, seed=5)
        ident = identifiability(associate(SITE, obs, tracks, true_pose(), gate_px=60))
        assert ident["heading_spread_deg"] > 25.0
        assert ident["can_fit"]["clock_offset_s"]

    def test_peu_de_points_interdit_tout(self):
        ident = identifiability([])
        assert ident["n"] == 0
        assert not any(ident["can_fit"].values())

    def test_dispersion_angulaire_insensible_au_passage_par_zero(self):
        from openvigie.calibration import _angular_spread

        assert _angular_spread([359.0, 1.0]) < 5.0
        assert _angular_spread([0.0, 180.0]) > 50.0

    def test_fraction_altitude_geometrique_reportee(self):
        tracks, obs = scenario(n_aircraft=15, geometric=False, outliers=0, seed=5)
        ident = identifiability(associate(SITE, obs, tracks, true_pose(), gate_px=60))
        assert ident["geometric_altitude_fraction"] == 0.0


class TestFit:
    def test_retrouve_la_pose_vraie(self):
        tracks, obs = scenario()
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        assert r.quality == "good"
        assert abs(r.pose.yaw_deg - TRUE["yaw_deg"]) < 0.03
        assert abs(r.pose.pitch_deg - TRUE["pitch_deg"]) < 0.03
        assert abs(r.pose.focal_mm - TRUE["focal_mm"]) < 0.02

    def test_bien_meilleur_quune_boussole(self):
        """L'argument de fond : ±2° au compas contre quelques centièmes ici."""
        tracks, obs = scenario()
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        assert abs(r.pose.yaw_deg - TRUE["yaw_deg"]) < 2.0 / 20

    @pytest.mark.parametrize("erreur_boussole", [1.0, 3.0, 6.0, 12.0])
    def test_converge_depuis_une_boussole_tres_fausse(self, erreur_boussole):
        tracks, obs = scenario()
        start = guess_pose(yaw_deg=TRUE["yaw_deg"] + erreur_boussole)
        r = calibrate(SITE, obs, tracks, start, gate_px=400, fit=FIT)
        assert abs(r.pose.yaw_deg - TRUE["yaw_deg"]) < 0.05

    def test_robuste_aux_fausses_detections(self):
        tracks, obs = scenario(outliers=30)
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        assert abs(r.pose.yaw_deg - TRUE["yaw_deg"]) < 0.05

    def test_precision_croit_avec_le_nombre_dobservations(self):
        petit = calibrate(SITE, *scenario(n_aircraft=5, outliers=0)[::-1], guess_pose(), gate_px=250, fit=FIT)
        grand = calibrate(SITE, *scenario(n_aircraft=40, outliers=0)[::-1], guess_pose(), gate_px=250, fit=FIT)
        assert grand.n_used > petit.n_used
        assert grand.sigma.get("yaw_deg", 1.0) < petit.sigma.get("yaw_deg", 1.0)

    def test_decalage_dhorloge_estime(self):
        """Une seconde d'erreur vaut 0,29° d'azimut à 50 km : le mesurer plutôt
        que le subir."""
        tracks, obs = scenario(clock_error=1.0, outliers=0)
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT + ("clock_offset_s",))
        assert abs(abs(r.clock_offset_s) - 1.0) < 0.15
        assert abs(r.pose.yaw_deg - TRUE["yaw_deg"]) < 0.03

    def test_horloge_gelee_si_couloir_unique(self):
        tracks, obs = scenario(heading_spread=8.0, outliers=0)
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT + ("clock_offset_s",))
        assert "clock_offset_s" in r.frozen
        assert any("clock_offset_s" in n for n in r.notes)

    def test_biais_daltitude_barometrique_corrige(self):
        """300 m d'erreur d'altitude, c'est 0,33° d'élévation à 50 km — soit
        directement une erreur d'assiette, donc de portée estimée."""
        tracks, obs = scenario(altitude_offset_m=300.0, geometric=False, outliers=0)
        sans = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        avec = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250,
                         fit=FIT + ("altitude_offset_m",))
        assert abs(sans.pose.pitch_deg - TRUE["pitch_deg"]) > 0.1
        assert abs(avec.pose.pitch_deg - TRUE["pitch_deg"]) < 0.05

    def test_bruit_eleve_degrade_la_qualite_annoncee(self):
        tracks, obs = scenario(noise_px=12.0, outliers=0)
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        assert r.quality in ("poor", "usable")
        assert r.bearing_sigma_deg > 0.05

    def test_donnees_insuffisantes_refusees(self):
        tracks, obs = scenario(n_aircraft=1, outliers=0)
        r = fit_pose(SITE, associate(SITE, obs, tracks, guess_pose(), gate_px=250)[:2], guess_pose())
        assert not r.converged
        assert r.quality == "insufficient"

    def test_aucune_observation(self):
        r = fit_pose(SITE, [], guess_pose())
        assert r.quality == "insufficient"
        assert r.frozen == ("yaw_deg", "pitch_deg", "roll_deg", "focal_mm",
                            "clock_offset_s", "altitude_offset_m")

    def test_incertitude_annoncee_est_conservatrice(self):
        """Une incertitude sous-estimée donne à un opérateur une confiance qu'il
        n'a pas : on la veut plus large que l'erreur réelle."""
        tracks, obs = scenario()
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        erreur_reelle = abs(r.pose.yaw_deg - TRUE["yaw_deg"])
        assert r.bearing_sigma_deg >= erreur_reelle
        assert r.bearing_sigma_deg >= 0.02

    def test_rms_en_degres_coherent(self):
        tracks, obs = scenario()
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        ifov = math.degrees(math.atan(r.pose.pixel_mm / r.pose.focal_mm))
        assert r.rms_deg == pytest.approx(r.rms_px * ifov, rel=1e-6)

    def test_serialisation(self, tmp_path):
        tracks, obs = scenario()
        r = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        path = tmp_path / "cal.json"
        r.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["quality"] == "good"
        assert "bearing_sigma_deg" in data


class TestDrift:
    def _calibration(self, yaw_shift: float, seed: int = 11) -> CalibrationResult:
        tracks = synthesize_traffic(SITE, n_aircraft=30, seed=3)
        pose = true_pose().copy_with(yaw_deg=TRUE["yaw_deg"] + yaw_shift)
        obs = synthesize_observations(SITE, tracks, pose, noise_px=1.5, seed=seed)
        return calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)

    def test_pose_stable(self):
        ref = self._calibration(0.0, seed=11)
        assert check_drift(ref, self._calibration(0.0, seed=41))["status"] == "stable"

    def test_glissement_detecte(self):
        """L'usage le plus rentable en exploitation : un preset qui a glissé se
        voit ici, pas sur une alerte mal localisée."""
        ref = self._calibration(0.0, seed=11)
        drift = check_drift(ref, self._calibration(0.4, seed=41))
        assert drift["status"] == "drifted"
        assert drift["delta_yaw_deg"] == pytest.approx(0.4, abs=0.05)

    def test_petit_ecart_non_signale(self):
        ref = self._calibration(0.0, seed=11)
        assert check_drift(ref, self._calibration(0.05, seed=41))["status"] == "stable"

    def test_etalonnage_insuffisant_ne_conclut_pas(self):
        ref = self._calibration(0.0)
        vide = fit_pose(SITE, [], guess_pose())
        assert check_drift(ref, vide)["status"] == "unknown"

    def test_passage_par_360_gere(self):
        ref = self._calibration(0.0)
        courant = self._calibration(0.0)
        courant.pose = courant.pose.copy_with(yaw_deg=courant.pose.yaw_deg)
        ref.pose = ref.pose.copy_with(yaw_deg=359.95)
        courant.pose = courant.pose.copy_with(yaw_deg=0.05)
        assert abs(check_drift(ref, courant)["delta_yaw_deg"]) < 0.2


class TestSkyDetection:
    def _scene(self, height=120, width=160, horizon=70):
        rng = np.random.default_rng(4)
        sky = np.linspace(190, 165, horizon)[:, None] * np.ones((1, width))
        ground = 90 + 40 * rng.random((height - horizon, width))
        base = np.vstack([sky, ground]).astype(np.float32)
        return base

    def test_avion_detecte_dans_le_ciel(self):
        base = self._scene()
        frame = base.copy()
        frame[30, 80] = 255.0        # un avion à 50 km fait un ou deux pixels
        frame[30, 81] = 240.0
        found = detect_sky_points(frame, base, t=1000.0, horizon_row=70)
        assert found
        assert found[0].col == pytest.approx(80.5, abs=1.5)
        assert found[0].row == pytest.approx(30, abs=1.5)

    def test_rien_dans_une_scene_stable(self):
        base = self._scene()
        assert detect_sky_points(base, base, t=0.0, horizon_row=70) == []

    def test_le_sol_est_ignore(self):
        """Le module ne doit jamais regarder sous l'horizon : c'est le domaine
        du détecteur de fumée, et un feu n'est pas une mire."""
        base = self._scene()
        frame = base.copy()
        frame[100, 80] = 255.0
        assert detect_sky_points(frame, base, t=0.0, horizon_row=70) == []

    def test_grosse_tache_ecartee(self):
        """Une traînée de condensation est très visible mais dérive avec le
        vent : elle ne dit pas où est l'avion."""
        base = self._scene()
        frame = base.copy()
        frame[20:40, 40:120] = 255.0
        assert detect_sky_points(frame, base, t=0.0, horizon_row=70, max_area_px=60) == []

    def test_horizon_par_colonne(self):
        base = self._scene()
        frame = base.copy()
        frame[30, 80] = 255.0
        horizon = np.full(160, 70, dtype=np.int32)
        assert detect_sky_points(frame, base, t=0.0, horizon_rows=horizon)

    def test_formes_incompatibles(self):
        with pytest.raises(ValueError):
            detect_sky_points(np.zeros((10, 10)), np.zeros((12, 12)), t=0.0, horizon_row=5)


class TestAdsbSources:
    def test_regroupement_en_traces(self):
        src = StaticAdsbSource([
            AircraftState("aaa", 0.0, 44.0, 3.0, 10_000.0),
            AircraftState("aaa", 10.0, 44.0, 3.1, 10_000.0),
            AircraftState("bbb", 5.0, 45.0, 3.0, 10_000.0),      # une seule position
        ])
        tracks = src.tracks(0.0, 100.0, min_states=2)
        assert set(tracks) == {"aaa"}
        assert len(tracks["aaa"].states) == 2

    def test_fenetre_temporelle(self):
        src = StaticAdsbSource([
            AircraftState("aaa", 0.0, 44.0, 3.0, 10_000.0),
            AircraftState("aaa", 500.0, 44.0, 3.1, 10_000.0),
        ])
        assert len(src.states(0.0, 100.0)) == 1

    def test_aller_retour_jsonl(self, tmp_path):
        src = StaticAdsbSource([AircraftState("aaa", 1.0, 44.0, 3.0, 10_000.0, callsign="AF123")])
        path = tmp_path / "adsb.jsonl"
        src.save_jsonl(path)
        assert StaticAdsbSource.from_jsonl(path).states(0, 10)[0].callsign == "AF123"


class TestConfigAndPipeline:
    def test_desactive_par_defaut(self):
        assert CalibrationConfig().enabled is False
        assert tier_defaults("medium").calibration.enabled is False

    def test_active_sur_le_tier_full(self):
        cfg = tier_defaults("full").calibration
        assert cfg.enabled and cfg.adsb_source == "dump1090"

    def test_source_invalide(self):
        with pytest.raises(ValueError):
            CalibrationConfig(adsb_source="radar")

    def test_sigma_invalide(self):
        with pytest.raises(ValueError):
            CalibrationConfig(bearing_sigma_deg=0.0)

    def test_site_depuis_la_configuration(self):
        cfg = tier_defaults("full")
        site = cfg.site()
        assert site.latitude == cfg.latitude
        assert site.height_m == cfg.optics.camera_height_m

    def test_pipeline_utilise_le_sigma_de_repli(self):
        from openvigie.pipeline import DetectionPipeline

        cfg = tier_defaults("medium")
        assert DetectionPipeline(cfg).bearing_sigma_deg == cfg.calibration.bearing_sigma_deg

    def test_application_dun_etalonnage(self):
        from openvigie.detectors import get_detector
        from openvigie.pipeline import DetectionPipeline

        cfg = tier_defaults("full")
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"))
        pipe.register_view("V00", azimuth_deg=90.0, focal_mm=cfg.optics.focal_mm)
        tracks, obs = scenario()
        result = calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT)
        pipe.apply_calibration("V00", result)
        assert pipe.views["V00"].pose is not None
        assert pipe.views["V00"].azimuth_deg == pytest.approx(TRUE["yaw_deg"], abs=0.05)
        assert pipe.bearing_sigma_deg < cfg.calibration.bearing_sigma_deg
        assert "V00" in pipe.summary()["calibrated_views"]

    def test_etalonnage_insuffisant_refuse(self):
        from openvigie.pipeline import DetectionPipeline

        pipe = DetectionPipeline(tier_defaults("full"))
        pipe.register_view("V00", 90.0)
        with pytest.raises(ValueError, match="insuffisant"):
            pipe.apply_calibration("V00", fit_pose(SITE, [], guess_pose()))

    def test_vue_inconnue(self):
        from openvigie.pipeline import DetectionPipeline

        pipe = DetectionPipeline(tier_defaults("full"))
        tracks, obs = scenario()
        with pytest.raises(KeyError):
            pipe.apply_calibration("V99", calibrate(SITE, obs, tracks, guess_pose(), gate_px=250, fit=FIT))

    def test_config_yaml_accepte_le_bloc(self, tmp_path):
        from openvigie.config import load_site_config

        p = tmp_path / "s.yaml"
        p.write_text(
            "site_id: t1\ntier: full\ncalibration:\n  enabled: true\n  bearing_sigma_deg: 0.03\n",
            encoding="utf-8",
        )
        assert load_site_config(p).calibration.bearing_sigma_deg == 0.03


class TestCli:
    def test_validation_simulee(self, capsys):
        assert main(["calibrate", "-t", "full", "--simulate"]) == 0
        out = capsys.readouterr().out
        assert "recouvrement de la pose vraie : OK" in out
        assert "sigma azimut" in out

    def test_validation_avec_decalage_dhorloge(self, capsys):
        assert main(["calibrate", "-t", "full", "--simulate", "--clock-error", "1.5"]) == 0
        assert "décalage horloge" in capsys.readouterr().out

    def test_json(self, capsys):
        assert main(["calibrate", "-t", "full", "--simulate", "--json"]) == 0
        data = json.loads(capsys.readouterr().out.split("\n\n")[0])
        assert data["quality"] in ("good", "usable")

    def test_ecriture_du_resultat(self, tmp_path, capsys):
        out = tmp_path / "cal.json"
        assert main(["calibrate", "-t", "full", "--simulate", "--output", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["pose"]["yaw_deg"]

    def test_donnees_reelles_exigent_les_deux_fichiers(self):
        assert main(["calibrate", "-t", "full"]) == 2
