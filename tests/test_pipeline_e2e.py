"""Tests d'intégration du pipeline complet.

Ces tests sont les plus proches de la réalité opérationnelle : ils vérifient
non seulement qu'un panache déclenche une alerte, mais surtout que les
**négatifs durs** n'en déclenchent pas — nuage au-dessus de l'horizon, scène
stable, changement global, vibration du mât. En exploitation, c'est le second
groupe qui décide de l'adoption du système.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from openvigie.config import TIERS, tier_defaults
from openvigie.detectors import get_detector
from openvigie.geometry import horizon_row
from openvigie.pipeline import DetectionPipeline
from openvigie.registration import shift_image
from openvigie.sources import SyntheticScene, SyntheticSource


def build(cfg=None, detector="classical", height=180, width=320, mode="shadow"):
    """Pipeline + scène dont l'horizon coïncide avec le modèle géométrique.

    ``mode`` est explicite : depuis 0.4.0, un site ne peut plus émettre d'alerte
    par accident (AUDIT P0-06).
    """
    cfg = cfg or tier_defaults("medium")
    cfg.operating.mode = mode
    pipe = DetectionPipeline(cfg, detector=get_detector(detector))
    state = pipe.register_view("V00", azimuth_deg=120.0, focal_mm=cfg.optics.focal_mm)
    sensor = cfg.optics.sensor_spec()
    hr = int(round(horizon_row(sensor, state.focal_mm, cfg.optics.tilt_deg) * height / sensor.height_px))
    scene = SyntheticScene(height=height, width=width, horizon_row=max(10, min(height - 30, hr)))
    return pipe, scene


def run(pipe, source, view="V00", period_s=30.0):
    results = []
    t = 0.0
    while True:
        item = source.read()
        if item is None:
            break
        frame, ts = item
        results.append(pipe.process_frame(view, frame, ts, t_monotonic=t))
        t += period_s
    return results


class TestPositif:
    @pytest.mark.parametrize("tier", TIERS)
    def test_panache_croissant_declenche_une_alerte(self, tier):
        cfg = tier_defaults(tier)
        pipe, scene = build(cfg, mode="shadow")
        results = run(pipe, SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8))
        assert pipe.stats["alerts"] >= 1, f"tier {tier}: aucune alerte sur un panache croissant"
        assert any(r.alerts for r in results)

    def test_alerte_porte_un_azimut_absolu_coherent(self):
        pipe, scene = build()
        run(pipe, SyntheticSource(scene=scene, mode="plume"))
        alert = pipe.alerts.emitted[0]
        # la vue est orientée à 120°, le panache est centré : azimut proche de 120°
        assert abs((alert.bearing_deg - 120.0 + 180) % 360 - 180) < 15

    def test_alerte_journalise_les_features_et_la_version(self):
        pipe, scene = build()
        run(pipe, SyntheticSource(scene=scene, mode="plume"))
        a = pipe.alerts.emitted[0]
        assert "growth_score" in a.features and "bbox" in a.features
        assert a.pipeline_tier and a.model_version
        assert a.n_visits >= 3

    def test_localisation_par_mnt(self):
        pipe, scene = build()
        run(pipe, SyntheticSource(scene=scene, mode="plume"))
        a = pipe.alerts.emitted[0]
        assert a.localization == "dem_intersect"
        assert a.latitude is not None and a.distance_m is not None

    def test_pas_dalerte_avant_maturite_du_fond(self):
        pipe, scene = build()
        results = run(pipe, SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8))
        assert results[0].status == "no_reference"
        assert not results[0].alerts
        assert pipe.stats["skipped"] >= 1

    def test_dedoublonnage_evite_la_repetition(self):
        """Un feu confirmé ne doit pas réalerter à chaque cycle."""
        pipe, scene = build()
        run(pipe, SyntheticSource(scene=scene, mode="plume", n_plume=14))
        assert pipe.stats["alerts"] == 1


class TestNegatifs:
    @pytest.mark.parametrize("tier", TIERS)
    def test_nuage_au_dessus_de_lhorizon_rejete(self, tier):
        """Négatif dur n°1 : même apparence qu'un panache, mais pas d'origine
        au sol. Seule la géométrie le distingue."""
        pipe, scene = build(tier_defaults(tier), mode="shadow")
        run(pipe, SyntheticSource(scene=scene, mode="cloud", n_background=6, n_plume=8))
        assert pipe.stats["alerts"] == 0, f"tier {tier}: le nuage a déclenché une alerte"

    def test_scene_stable_ne_genere_aucun_candidat(self):
        pipe, scene = build()
        src = SyntheticSource(scene=scene, mode="plume", n_background=14, n_plume=0)
        results = run(pipe, src)
        assert pipe.stats["alerts"] == 0
        assert sum(r.n_candidates for r in results) == 0

    def test_changement_global_dexposition_rejete(self):
        """Brouillard soudain, bascule WDR, commutation du filtre IR."""
        pipe, scene = build()
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        for i in range(6, 12):
            bright = np.clip(scene.frame().astype(np.float32) + 55, 0, 255).astype(np.uint8)
            pipe.process_frame("V00", bright, ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        assert pipe.stats["alerts"] == 0

    def test_vibration_du_mat_suspend_lanalyse(self):
        """Un pylône qui bouge doit faire sauter le cycle, pas produire des
        candidats sur tous les contours de la scène."""
        cfg = tier_defaults("medium")
        cfg.pipeline.vibration_gate_px = 3.0
        pipe, scene = build(cfg, mode="shadow")
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        shaken = shift_image(scene.frame().astype(np.float32), 9, 6).astype(np.uint8)
        res = pipe.process_frame("V00", shaken, ts + dt.timedelta(seconds=210), t_monotonic=210.0)
        assert res.status == "misaligned"
        assert not res.alerts

    def test_objet_opaque_stationnaire_ne_declenche_pas(self):
        """Un véhicule ou un engin arrêté : pas de croissance, donc rejet."""
        pipe, scene = build()
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        for i in range(6, 16):
            frame = scene.frame().astype(np.float32)
            frame[scene.horizon_row + 25 : scene.horizon_row + 40, 150:172] = 20.0  # tache opaque fixe
            pipe.process_frame(
                "V00", frame.astype(np.uint8), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i
            )
        assert pipe.stats["alerts"] == 0


class TestRobustesse:
    def test_vue_non_enregistree(self):
        pipe, scene = build()
        with pytest.raises(KeyError, match="non enregistrée"):
            pipe.process_frame("INCONNUE", scene.frame(), dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc))

    def test_repli_de_backend_est_trace(self):
        cfg = tier_defaults("medium")  # demande 'nnie', absent sur un PC
        pipe = DetectionPipeline(cfg)
        assert pipe.detector.name == "classical"
        assert pipe.degraded_reason and "nnie" in pipe.degraded_reason
        assert pipe.summary()["degraded"] is not None

    def test_resolution_reduite_supportee(self):
        """Le sous-échantillonnage est le premier levier de performance sur une
        carte caméra : les cartes de distance doivent suivre."""
        pipe, scene = build(height=120, width=160)
        results = run(pipe, SyntheticSource(scene=scene, mode="plume"))
        assert all(r.status in ("ok", "no_reference") for r in results)

    def test_deux_vues_independantes(self):
        cfg = tier_defaults("medium")
        pipe, scene = build(cfg, mode="shadow")
        pipe.register_view("V01", azimuth_deg=200.0, focal_mm=cfg.optics.focal_mm)
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc)
        src_plume = SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8)
        src_calme = SyntheticSource(scene=scene, mode="plume", n_background=14, n_plume=0)
        t = 0.0
        while True:
            a, b = src_plume.read(), src_calme.read()
            if a is None or b is None:
                break
            pipe.process_frame("V00", a[0], ts + dt.timedelta(seconds=t), t_monotonic=t)
            pipe.process_frame("V01", b[0], ts + dt.timedelta(seconds=t), t_monotonic=t)
            t += 30.0
        assert all(al.view_id == "V00" for al in pipe.alerts.emitted)

    def test_summary_expose_letat(self):
        pipe, scene = build()
        run(pipe, SyntheticSource(scene=scene, mode="plume"))
        s = pipe.summary()
        assert s["frames"] > 0 and s["tier"] == "medium" and "V00" in s["views"]

    def test_taux_de_faux_positifs_sur_negatifs_longs(self):
        """Approximation du test d'acceptation : 60 cycles de scène stable,
        objectif zéro alerte. En exploitation, ce test se fait sur 30 jours de
        flux réel du site, et c'est lui qui fixe le seuil."""
        pipe, scene = build()
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc)
        for i in range(60):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        assert pipe.stats["alerts"] == 0
        assert pipe.alerts.stats(observation_days=60 * 30 / 86400)["emitted"] == 0
