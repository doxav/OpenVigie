"""Tests de la fusion, de la décision, des backends et de l'alerting."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from openvigie.alerting import Alert, AlertStore, localize, make_alert_id
from openvigie.detectors import (
    ClassicalDetector,
    NullDetector,
    available_backends,
    get_detector,
)
from openvigie.geometry import IMX675, LENS_27135, plan_uniform_ring
from openvigie.ptz import (
    ScanScheduler,
    SimulatedPtz,
    health_warnings,
    pelco_d_frame,
    pelco_d_goto_preset,
    pelco_d_set_preset,
)
from openvigie.scoring import (
    FEATURE_ORDER,
    DecisionConfig,
    FusionModel,
    decide,
    fit_logistic,
    threshold_for_fp_budget,
)
from openvigie.tracking import TrackFeatures


def _feats(**kw) -> TrackFeatures:
    base = {
        "persistence": 1.0, "growth_score": 0.7, "growth_m2_s": 12.0, "upward_score": 0.6,
        "upward_m_s": 1.2, "ground_origin": 1.0, "contrast_loss": 0.4, "translucency": 0.8,
        "wind_coherence": 0.7, "cnn_score": 0.8,
    }
    base.update(kw)
    return TrackFeatures(**base)


class TestFusion:
    def test_score_dans_zero_un(self):
        m = FusionModel()
        assert 0.0 <= m.score(_feats()) <= 1.0
        assert 0.0 <= m.score(TrackFeatures()) <= 1.0

    def test_monotonie_en_cnn(self):
        m = FusionModel()
        assert m.score(_feats(cnn_score=0.9)) > m.score(_feats(cnn_score=0.1))

    def test_monotonie_en_croissance(self):
        m = FusionModel()
        assert m.score(_feats(growth_score=0.9)) > m.score(_feats(growth_score=0.05))

    def test_features_faibles_donnent_score_faible(self):
        assert FusionModel().score(TrackFeatures()) < 0.1

    def test_veto_horizon(self):
        m = FusionModel()
        assert m.veto(_feats(ground_origin=0.0)) is not None
        assert m.veto(_feats(ground_origin=1.0)) is None

    def test_veto_desactivable(self):
        assert FusionModel().veto(_feats(ground_origin=0.0), require_ground_origin=False) is None

    def test_veto_surface_decroissante(self):
        assert FusionModel().veto(_feats(growth_m2_s=-5.0, persistence=1.0)) is not None

    def test_serialisation_aller_retour(self):
        m = FusionModel(weights=dict.fromkeys(FEATURE_ORDER, 0.5), bias=-2.0, fitted=True)
        assert FusionModel.from_dict(m.as_dict()).as_dict() == m.as_dict()

    def test_version_de_schema_incompatible_refusee(self):
        with pytest.raises(ValueError, match="réentraîner"):
            FusionModel.from_dict({"schema_version": 99, "weights": {}, "bias": 0.0})

    def test_apprentissage_separe_deux_classes(self):
        rng = np.random.default_rng(0)
        n = 300
        pos = rng.uniform(0.6, 1.0, (n, len(FEATURE_ORDER)))
        neg = rng.uniform(0.0, 0.4, (n, len(FEATURE_ORDER)))
        X = np.vstack([pos, neg])
        y = np.hstack([np.ones(n), np.zeros(n)])
        model = fit_logistic(X, y)
        assert model.fitted
        assert model.score(_feats()) > 0.5
        assert model.score(TrackFeatures()) < 0.5

    def test_apprentissage_rejette_dimensions_invalides(self):
        with pytest.raises(ValueError):
            fit_logistic(np.zeros((10, 3)), np.zeros(10))
        with pytest.raises(ValueError):
            fit_logistic(np.zeros((10, len(FEATURE_ORDER))), np.zeros(9))


class TestThreshold:
    def test_seuil_respecte_le_budget_de_faux_positifs(self):
        """Le seuil se règle sur les FP/jour mesurés, pas sur le F1."""
        scores = np.linspace(0, 1, 300)
        thr = threshold_for_fp_budget(scores, observation_days=30.0, fp_per_day_budget=1.0)
        assert (scores >= thr).sum() <= 31

    def test_budget_plus_strict_donne_seuil_plus_haut(self):
        scores = np.linspace(0, 1, 300)
        strict = threshold_for_fp_budget(scores, 30.0, 0.2)
        laxe = threshold_for_fp_budget(scores, 30.0, 2.0)
        assert strict > laxe

    def test_jeu_vide(self):
        assert threshold_for_fp_budget(np.array([]), 30.0, 1.0) == 0.5


class TestDecision:
    def test_alerte_apres_persistance_suffisante(self):
        cfg = DecisionConfig(enter_threshold=0.6, min_persistence_visits=3)
        state, _ = decide("CANDIDATE", 0.9, _feats(), n_visits=3, cfg=cfg)
        assert state == "CONFIRMED"

    def test_pas_dalerte_avant_persistance(self):
        cfg = DecisionConfig(enter_threshold=0.6, min_persistence_visits=3)
        state, _ = decide("NEW", 0.99, _feats(), n_visits=1, cfg=cfg)
        assert state == "CANDIDATE"

    def test_veto_horizon_ecrase_un_score_eleve(self):
        state, reason = decide("CANDIDATE", 0.99, _feats(ground_origin=0.0), n_visits=5)
        assert state == "DISMISSED"
        assert "horizon" in reason

    def test_hysteresis_maintient_une_alerte(self):
        cfg = DecisionConfig(enter_threshold=0.8, exit_threshold=0.4)
        state, _ = decide("ALERTED", 0.55, _feats(), n_visits=5, cfg=cfg)
        assert state == "ALERTED"

    def test_sortie_dalerte_sous_le_seuil_bas(self):
        cfg = DecisionConfig(enter_threshold=0.8, exit_threshold=0.4)
        state, _ = decide("ALERTED", 0.2, _feats(), n_visits=5, cfg=cfg)
        assert state == "CANDIDATE"

    def test_absence_de_croissance_rejetee(self):
        cfg = DecisionConfig(min_persistence_visits=3, require_growth=True)
        state, reason = decide("CANDIDATE", 0.95, _feats(growth_m2_s=0.0), n_visits=4, cfg=cfg)
        assert state == "DISMISSED"
        assert "croissance" in reason


class TestDetectors:
    def test_registre(self):
        assert {"classical", "null", "onnx", "ultralytics", "nnie"} <= set(available_backends())

    def test_backend_inconnu(self):
        with pytest.raises(ValueError, match="inconnu"):
            get_detector("magique")

    def test_null_neutre(self):
        assert NullDetector().score_roi(np.zeros((16, 16, 3))) == 0.5

    def test_classical_dans_zero_un(self, rng):
        det = ClassicalDetector()
        for roi in (np.zeros((32, 32, 3)), rng.integers(0, 255, (32, 32, 3)).astype(np.uint8)):
            assert 0.0 <= det.score_roi(roi) <= 1.0

    def test_classical_prefere_une_roi_de_type_fumee(self, rng):
        """Une région grise, peu texturée et peu saturée doit scorer plus haut
        qu'une région très contrastée et colorée."""
        det = ClassicalDetector()
        fumee = np.full((48, 48, 3), 190, dtype=np.float32)
        fumee += rng.normal(0, 3, fumee.shape)
        vegetation = rng.integers(0, 255, (48, 48, 3)).astype(np.float32)
        vegetation[..., 1] += 60
        assert det.score_roi(np.clip(fumee, 0, 255)) > det.score_roi(np.clip(vegetation, 0, 255))

    def test_classical_roi_minuscule(self):
        assert 0.0 <= ClassicalDetector().score_roi(np.zeros((2, 2, 3))) <= 1.0

    def test_nnie_indisponible_leve(self):
        with pytest.raises(RuntimeError, match="introuvable"):
            get_detector("nnie", binary="/n/existe/pas")


class TestPelcoD:
    def test_longueur_et_synchro(self):
        f = pelco_d_frame(1, 0x00, 0x07, 0x00, 5)
        assert len(f) == 7
        assert f[0] == 0xFF

    def test_checksum(self):
        f = pelco_d_frame(1, 0x00, 0x07, 0x00, 5)
        assert f[6] == sum(f[1:6]) % 256

    def test_goto_preset_connu(self):
        # trame de référence : FF 01 00 07 00 01 09
        assert pelco_d_goto_preset(1, 1).hex() == "ff010007000109"

    def test_set_preset_utilise_0x03(self):
        assert pelco_d_set_preset(1, 4)[3] == 0x03

    @pytest.mark.parametrize("addr,preset", [(0, 1), (256, 1), (1, 0), (1, 256)])
    def test_plages_invalides(self, addr, preset):
        with pytest.raises(ValueError):
            pelco_d_goto_preset(addr, preset)


class TestScheduler:
    def _views(self, n=8):
        return plan_uniform_ring(IMX675, LENS_27135, n, 8_000)

    def test_cycle_coherent_avec_le_budget(self):
        s = ScanScheduler(self._views(8), dwell_s=12.0, settle_s=3.0)
        assert s.cycle_s == pytest.approx(8 * 15.0)

    def test_plan_cycle_horodate_et_ordonne(self):
        slots = ScanScheduler(self._views(4), 10.0, 2.0).plan_cycle(t0=100.0)
        assert len(slots) == 4
        assert slots[0].t_arrive_s == 100.0
        assert slots[0].t_settled_s == 102.0
        assert slots[0].t_leave_s == 112.0
        assert all(slots[i].t_arrive_s == slots[i - 1].t_leave_s for i in range(1, 4))

    def test_fenetre_danalyse_exclut_la_stabilisation(self):
        slot = ScanScheduler(self._views(2), 10.0, 4.0).plan_cycle()[0]
        start, end = slot.analysis_window_s
        assert start == 4.0 and end == 14.0

    def test_presets_uniques_et_numerotes_a_partir_de_1(self):
        slots = ScanScheduler(self._views(6), 10.0, 2.0).plan_cycle()
        assert [s.preset for s in slots] == [1, 2, 3, 4, 5, 6]

    def test_vues_prioritaires_doublent_les_visites(self):
        views = self._views(4)
        s = ScanScheduler(views, 10.0, 2.0, priority_views=[views[0].view_id])
        assert len(s.sequence) == 8
        assert s.cycle_s > ScanScheduler(views, 10.0, 2.0).cycle_s

    def test_avertissement_usure_pour_balayage_continu(self):
        warns = health_warnings(ScanScheduler(self._views(8), 12.0, 3.0))
        assert any("mouvements/an" in w for w in warns)

    def test_avertissement_cycle_trop_long(self):
        warns = health_warnings(ScanScheduler(self._views(30), 12.0, 3.0))
        assert any("latence" in w for w in warns)

    def test_avertissement_dwell_trop_court(self):
        assert any("dwell" in w for w in health_warnings(ScanScheduler(self._views(8), 5.0, 2.0)))

    def test_run_ne_traite_quapres_stabilisation(self):
        events: list[tuple[str, float]] = []
        sched = ScanScheduler(self._views(3), dwell_s=10.0, settle_s=2.0)
        backend = SimulatedPtz()

        def fake_sleep(d):
            events.append(("sleep", d))

        sched.run(backend, lambda v, p: events.append(("analyse", p)), cycles=1, sleep=fake_sleep)
        # séquence attendue par vue : sleep(settle) -> analyse -> sleep(dwell)
        assert events[0] == ("sleep", 2.0)
        assert events[1][0] == "analyse"
        assert events[2] == ("sleep", 10.0)
        assert backend.move_count == 3

    def test_scheduler_refuse_une_liste_vide(self):
        with pytest.raises(ValueError):
            ScanScheduler([], 10.0, 2.0)


class TestAlerting:
    def _alert(self, bearing=100.0, view="V00") -> Alert:
        return Alert(
            alert_id="a1", site_id="s1", view_id=view,
            timestamp=dt.datetime(2026, 8, 1, 14, 0).isoformat(),
            bearing_deg=bearing, score=0.9,
        )

    def test_deduplication_meme_secteur(self):
        store = AlertStore(bearing_tolerance_deg=3.0, silence_window_s=1800)
        assert store.submit(self._alert(100.0), t=0.0) is not None
        assert store.submit(self._alert(101.0), t=60.0) is None
        assert store.suppressed == 1

    def test_secteur_different_non_deduplique(self):
        store = AlertStore(bearing_tolerance_deg=3.0)
        store.submit(self._alert(100.0), t=0.0)
        assert store.submit(self._alert(140.0), t=60.0) is not None

    def test_fenetre_de_silence_expire(self):
        store = AlertStore(bearing_tolerance_deg=3.0, silence_window_s=600)
        store.submit(self._alert(100.0), t=0.0)
        assert store.submit(self._alert(100.0), t=1200.0) is not None

    def test_deduplication_par_vue(self):
        store = AlertStore()
        store.submit(self._alert(100.0, "V00"), t=0.0)
        assert store.submit(self._alert(100.0, "V01"), t=10.0) is not None

    def test_journalisation_jsonl(self, tmp_path):
        log = tmp_path / "alerts.jsonl"
        store = AlertStore(log_path=log)
        store.submit(self._alert(), t=0.0)
        assert log.exists()
        assert '"site_id": "s1"' in log.read_text()

    def test_stats(self):
        store = AlertStore()
        store.submit(self._alert(10.0), t=0.0)
        store.submit(self._alert(200.0), t=10.0)
        assert store.stats(observation_days=2.0)["alerts_per_day"] == 1.0

    def test_localisation_par_triangulation_prioritaire(self):
        lat, lon, mode = localize(44.0, 3.0, 0.0, 5_000.0, peer=(44.05, 2.95, 90.0))
        assert mode == "triangulated"
        assert lat is not None and lon is not None

    def test_localisation_par_mnt_si_pas_de_pair(self):
        _, _, mode = localize(44.0, 3.0, 90.0, 5_000.0, peer=None)
        assert mode == "dem_intersect"

    def test_localisation_relevement_seul(self):
        lat, lon, mode = localize(44.0, 3.0, 90.0, None, peer=None)
        assert mode == "bearing_only"
        assert lat is None and lon is None

    def test_payload_pyro(self):
        payload = self._alert().as_pyro_payload()
        assert payload["camera_id"] == "s1:V00"
        assert payload["azimuth"] == 100.0

    def test_alert_id_lisible_et_unique(self):
        """AUDIT P0-17 : l'identifiant reste lisible (site, vue, horodatage) mais
        deux alertes émises dans la même seconde ne peuvent plus se confondre."""
        ts = dt.datetime(2026, 8, 1, 14, 30, 5)
        a, b = make_alert_id("s1", "V02", ts), make_alert_id("s1", "V02", ts)
        assert a.startswith("s1-V02-20260801T143005")
        assert a != b
