"""Non-régression des corrections issues de l'audit de la version 0.3.0.

Chaque test porte l'identifiant du constat d'audit. Ces défauts partagent une
propriété désagréable : ils ne provoquaient **aucune erreur visible**. Ils
produisaient des résultats plausibles et faux — une localisation convaincante
mais décalée, un fond qui se dégrade lentement, une alerte émise avec des poids
que la documentation elle-même qualifiait de provisoires. C'est précisément le
genre de défaut qu'un test synthétique bien choisi attrape et qu'une
démonstration réussie masque.
"""

from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import pytest

from openvigie.background import BackgroundBank, BackgroundKey
from openvigie.candidates import CandidateConfig, extract_candidates
from openvigie.config import OperatingConfig, tier_defaults
from openvigie.dem import GeoTransform, synthetic_dem, terrain_profile
from openvigie.detectors import get_detector
from openvigie.geometry import (
    IMX675,
    LENS_27135,
    flat_earth_distance_map,
    hfov_deg,
    horizon_row,
    pixel_to_bearing,
    plan_uniform_ring,
)
from openvigie.masking import apply_masks, masked_fraction, validate_masks
from openvigie.pipeline import DetectionPipeline
from openvigie.platform import board_readiness, sensor_megapixels
from openvigie.ptz import ScanScheduler
from openvigie.sources import SyntheticScene, SyntheticSource
from openvigie.transport import MemoryTransport, Outbox

UTC = dt.timezone.utc


def _pipe(mode: str = "shadow", tier: str = "medium", **over):
    cfg = tier_defaults(tier)
    cfg.operating.mode = mode
    for k, v in over.items():
        setattr(cfg, k, v)
    pipe = DetectionPipeline(cfg, detector=get_detector("classical"))
    state = pipe.register_view("V00", azimuth_deg=90.0, focal_mm=cfg.optics.focal_mm)
    sensor = cfg.optics.sensor_spec()
    hr = int(round(horizon_row(sensor, state.focal_mm, cfg.optics.tilt_deg) * 180 / sensor.height_px))
    scene = SyntheticScene(height=180, width=320, horizon_row=max(10, min(150, hr)))
    return pipe, scene


def _brouillard(scene: SyntheticScene) -> np.ndarray:
    """Brouillard réaliste : perte de contraste, pas décalage uniforme.

    Un simple offset constant est absorbé par le seuillage MAD — c'est le
    comportement voulu. Le changement global qui compte est celui qui modifie
    les pixels *différemment* : c'est celui-là qui doit être nommé et qui ne doit
    pas être appris comme nouveau fond.
    """
    return np.clip(scene.frame().astype(np.float32) * 0.35 + 150.0, 0, 255).astype(np.uint8)


def _run(pipe, source, view="V00"):
    out = []
    t = 0.0
    while True:
        item = source.read()
        if item is None:
            break
        out.append(pipe.process_frame(view, item[0], item[1], t_monotonic=t))
        t += 30.0
    return out


# --------------------------------------------------------------------------- #
class TestP0_10_ProjectionRectilineaire:
    """La carte de distance et le relèvement doivent suivre le MÊME modèle
    optique. Ils divergeaient : l'un rectilinéaire, l'autre équirectangulaire."""

    def test_horizon_coherent_avec_le_modele_rectilineaire(self):
        for f in (2.8, 5.2, 13.5):
            for tilt in (0.0, 1.5, 4.0):
                focal_px = f / (IMX675.pixel_um * 1e-3)
                attendu = (IMX675.height_px - 1) / 2 - math.tan(math.radians(tilt)) * focal_px
                assert horizon_row(IMX675, f, tilt) == pytest.approx(attendu, abs=1)

    def test_carte_de_distance_coherente_avec_le_relevement(self):
        """Le test de fond : la ligne d'horizon de la carte de distance doit
        tomber exactement là où le modèle de relèvement la place."""
        for f in (2.8, 5.2, 13.5):
            dmap = flat_earth_distance_map(IMX675, f, 40.0, tilt_deg=2.0)
            col = dmap[:, IMX675.width_px // 2]
            premiere_ligne_sol = int(np.argmax(np.isfinite(col)))
            assert premiere_ligne_sol == pytest.approx(horizon_row(IMX675, f, 2.0), abs=2)

    def test_ecart_grand_angle_avant_correction(self):
        """Chiffre le défaut corrigé : au grand-angle, le modèle linéaire
        s'écartait de plus de 2° du modèle correct — 200 m à 5 km."""
        f = 2.8
        col = IMX675.width_px * 0.75
        rectilineaire = pixel_to_bearing(col, IMX675, f, 0.0)
        lineaire = (col - (IMX675.width_px - 1) / 2) / (IMX675.width_px - 1) * hfov_deg(IMX675, f)
        assert abs(rectilineaire - lineaire) > 2.0

    def test_focale_longue_peu_sensible(self):
        """À 13,5 mm l'écart devient négligeable : c'est pourquoi le défaut est
        resté invisible sur les configurations à champ étroit."""
        f = 13.5
        col = IMX675.width_px * 0.75
        rect = pixel_to_bearing(col, IMX675, f, 0.0)
        lin = (col - (IMX675.width_px - 1) / 2) / (IMX675.width_px - 1) * hfov_deg(IMX675, f)
        assert abs(rect - lin) < 0.1


class TestP0_04_RoiRecalee:
    """La ROI était découpée dans l'image brute alors que la boîte venait de
    l'image recalée : dès que la caméra bougeait, le classifieur regardait
    ailleurs."""

    def test_roi_suit_le_recalage(self):
        from openvigie.registration import shift_image

        pipe, scene = _pipe()
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)

        vues: list[np.ndarray] = []

        class Espion:
            name = "espion"

            def score_roi(self, roi):
                vues.append(np.asarray(roi))
                return 0.5

            def warmup(self):
                return None

        pipe.detector = Espion()
        panache = scene.with_plume(cx=160, base_row=scene.horizon_row + 20, width_px=30, height_px=45)
        decale = shift_image(panache.astype(np.float32), 3, 2).astype(np.uint8)
        pipe.process_frame("V00", decale, ts + dt.timedelta(seconds=210), t_monotonic=210.0)

        # Les ROI examinées doivent contenir le panache (clair) et non le fond.
        assert vues
        assert max(float(np.asarray(v).mean()) for v in vues) > 100


class TestP0_05_06_07_HygieneDuFond:
    def test_changement_global_est_un_etat_nomme(self):
        """AUDIT P0-06 : le brouillard renvoyait « aucun candidat », ce qui est
        indistinguable d'une scène calme — et l'image était apprise comme fond."""
        pipe, scene = _pipe()
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        res = pipe.process_frame("V00", _brouillard(scene), ts + dt.timedelta(seconds=210), t_monotonic=210.0)
        assert res.status == "global_change"
        assert "fond non mis à jour" in res.detail

    def test_le_fond_nest_pas_pollue_par_un_changement_global(self):
        pipe, scene = _pipe()
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        key = BackgroundKey.build("V00", ts, sunrise_h=pipe.cfg.sunrise_h, sunset_h=pipe.cfg.sunset_h)
        avant = pipe.background.reference(key).copy()
        for i in range(6, 12):
            pipe.process_frame("V00", _brouillard(scene), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        np.testing.assert_allclose(pipe.background.reference(key), avant)

    def test_un_candidat_nest_pas_absorbe_dans_le_fond(self):
        """AUDIT P0-07 : seules les pistes CONFIRMED gelaient l'apprentissage,
        si bien qu'un panache lent pouvait être appris avant d'être confirmé."""
        pipe, scene = _pipe()
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        key = BackgroundKey.build("V00", ts, sunrise_h=pipe.cfg.sunrise_h, sunset_h=pipe.cfg.sunset_h)
        avant = pipe.background.reference(key).copy()
        lent = scene.with_plume(cx=160, base_row=scene.horizon_row + 20, width_px=22, height_px=30)
        pipe.process_frame("V00", lent, ts + dt.timedelta(seconds=210), t_monotonic=210.0)
        if any(t.state in ("CANDIDATE", "CONFIRMED", "ALERTED") for t in pipe.tracker.tracks):
            np.testing.assert_allclose(pipe.background.reference(key), avant)

    def test_changement_global_mesure_sur_statistiques_globales(self):
        """Défaut trouvé en écrivant ce test, au-delà de l'audit : le garde-fou
        ``max_area_frac`` ne se déclenchait pratiquement jamais, parce que le
        seuil MAD s'adapte à la dispersion de la différence — quand toute
        l'image change, le seuil monte avec elle et le masque reste vide."""
        from openvigie.candidates import detect_global_change

        scene = SyntheticScene(height=120, width=160, horizon_row=60)
        ref = scene.frame(noise=0.5)
        calme = detect_global_change(scene.frame(noise=0.5), ref)
        brouillard = detect_global_change(_brouillard(scene), ref)
        assert not calme["is_global_change"]
        assert brouillard["is_global_change"]
        assert brouillard["contrast_ratio"] < 0.6

        # et l'ancien garde-fou, lui, ne voyait rien
        _, fraction = extract_candidates(
            _brouillard(scene), ref, CandidateConfig(), return_change_fraction=True
        )
        assert fraction < CandidateConfig().max_area_frac


class TestP0_11_VentEnRepereGeographique:
    def test_score_identique_quelle_que_soit_lorientation(self):
        """Le défaut : la même fumée réelle recevait des scores opposés selon
        l'azimut de la caméra, la dérive étant mesurée en pixels."""
        from openvigie.tracking import Observation, Track, compute_features

        def piste(base_az):
            tr = Track(track_id=1, view_id="V00")
            for i in range(5):
                blob = type("B", (), {"bbox": (100, 100, 130, 140), "centroid": (115.0, 120.0),
                                      "area_px": 400, "bottom_row": 139})()
                tr.add(Observation(t=float(i * 30), blob=blob, area_m2=50.0, centroid_y_m=0.0,
                                   distance_m=4000.0, contrast_loss=0.3, translucency=0.8,
                                   azimuth_deg=base_az + i * 0.05))
            return tr

        a = compute_features(piste(10.0), horizon_row=90, wind_bearing_deg=100.0)
        b = compute_features(piste(250.0), horizon_row=90, wind_bearing_deg=340.0)
        assert a.wind_coherence == pytest.approx(b.wind_coherence, abs=0.02)


class TestP0_12_HorodatageUtc:
    def test_date_naive_refusee(self):
        pipe, scene = _pipe()
        with pytest.raises(ValueError, match="naïf"):
            pipe.process_frame("V00", scene.frame(), dt.datetime(2026, 8, 1, 14, 0))

    def test_source_synthetique_produit_de_lutc(self):
        src = SyntheticSource(scene=SyntheticScene(), n_background=1, n_plume=0)
        assert src.read()[1].tzinfo is not None


class TestP0_09_MntProjete:
    def test_dalle_projetee_refusee(self):
        """Une dalle Lambert-93 interrogée en lat/lon ne renvoie pas une erreur :
        elle renvoie un terrain arbitraire mais plausible."""
        dem = synthetic_dem(size=40)
        dem.transform = GeoTransform(700_000, 6_600_000, 25.0, -25.0, crs="projected")
        with pytest.raises(ValueError, match="projetées"):
            terrain_profile(dem, 44.0, 3.0, 40.0, 90.0)


class TestP0_13_AltitudeDuSite:
    def test_altitude_terrain_prise_en_compte(self):
        cfg = tier_defaults("full")
        cfg.site_altitude_m = 850.0
        site = cfg.site()
        assert site.altitude_m == 850.0
        assert site.camera_alt_m == 850.0 + cfg.optics.camera_height_m

    def test_altitude_par_defaut_signalee(self):
        """Une altitude nulle sur une tour de montagne fausse l'étalonnage ADS-B
        de plusieurs dixièmes de degré : le défaut doit être visible."""
        cfg = tier_defaults("full")
        assert cfg.site_altitude_m == 0.0
        assert "altitude" in " ".join(cfg.readiness_notes()).lower()


class TestP0_06_ModeExploitation:
    def test_mode_mesure_nemet_rien(self):
        pipe, scene = _pipe(mode="measure")
        _run(pipe, SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8))
        assert pipe.stats["alerts"] == 0
        assert pipe.stats["suppressed_by_mode"] > 0
        assert not pipe.can_transmit

    def test_mode_shadow_journalise_sans_transmettre(self):
        pipe, scene = _pipe(mode="shadow")
        _run(pipe, SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8))
        assert pipe.stats["alerts"] >= 1
        assert pipe.stats["queued"] == 0
        assert not pipe.can_transmit

    def test_mode_alert_refuse_un_modele_non_calibre(self):
        """Le verrou central : les poids de fusion par défaut sont explicitement
        provisoires, ils ne doivent pas pouvoir alerter."""
        pipe, _ = _pipe(mode="alert")
        assert not pipe.fusion.fitted
        assert not pipe.can_transmit
        assert "non calibré" in pipe.transmission_blocker()

    def test_derogation_explicite_et_tracee(self):
        cfg = tier_defaults("medium")
        cfg.operating = OperatingConfig(mode="alert", allow_uncalibrated_alerts=True)
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"))
        assert pipe.can_transmit
        assert pipe.summary()["fusion_calibrated"] is False

    def test_mode_alert_avec_modele_calibre(self):
        cfg = tier_defaults("medium")
        cfg.operating = OperatingConfig(mode="alert")
        cfg.fusion = {**cfg.fusion, "fitted": True}
        assert DetectionPipeline(cfg, detector=get_detector("classical")).can_transmit

    def test_mode_invalide(self):
        with pytest.raises(ValueError):
            OperatingConfig(mode="production")

    def test_tier_minimal_est_une_campagne_de_mesure(self):
        assert tier_defaults("minimal").operating.mode == "measure"


class TestP0_21_MasquesConfidentialite:
    def test_masque_applique(self):
        img = np.full((100, 200, 3), 200, dtype=np.uint8)
        out = apply_masks(img, [(50, 10, 150, 40)])
        assert out[20, 100].max() == 0
        assert out[80, 100].max() == 200

    def test_sans_masque_image_inchangee(self):
        img = np.full((10, 10), 5, dtype=np.uint8)
        assert apply_masks(img, None) is img

    def test_boite_hors_cadre_toleree(self):
        img = np.full((50, 50), 200, dtype=np.uint8)
        assert apply_masks(img, [(-10, -10, 500, 500)]).max() == 0

    def test_fraction_masquee(self):
        assert masked_fraction((100, 100), [(0, 0, 50, 100)]) == pytest.approx(0.5)

    def test_validation_des_boites(self):
        assert validate_masks({"V00": [[10, 10, 5, 5]]})
        assert validate_masks({"V00": [[1, 2, 3]]})
        assert validate_masks({"V00": [[0, 0, 10, 10]]}) == []

    def test_pixels_masques_ne_sortent_pas_du_site(self):
        """Le masquage a lieu à l'acquisition : ni le fond, ni les candidats, ni
        les vignettes ne peuvent contenir la zone protégée."""
        cfg = tier_defaults("medium")
        cfg.operating.mode = "shadow"
        cfg.masks = {"V00": [[0, 0, 320, 60]]}
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"))
        pipe.register_view("V00", 90.0, cfg.optics.focal_mm)
        scene = SyntheticScene(height=180, width=320, horizon_row=90)
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        for i in range(6):
            pipe.process_frame("V00", scene.frame(), ts + dt.timedelta(seconds=30 * i), t_monotonic=30.0 * i)
        key = BackgroundKey.build("V00", ts, sunrise_h=cfg.sunrise_h, sunset_h=cfg.sunset_h)
        assert float(pipe.background.reference(key)[:60].max()) == 0.0
        assert "V00" in pipe.summary()["masked_views"]


class TestP0_19_20_FileDurable:
    def _event(self):
        from openvigie.events import DetectionEvent, new_event_id

        return DetectionEvent(event_id=new_event_id(), site_id="s", camera_id="V00",
                              detected_at="2026-08-01T14:00:00+00:00")

    def test_echecs_definitifs_survivent_au_redemarrage(self):
        """AUDIT P0-20 : les dead letters n'existaient qu'en mémoire."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            clock = [0.0]
            box = Outbox(d, max_attempts=2, base_backoff_s=1.0, clock=lambda: clock[0])
            box.enqueue(self._event())
            transport = MemoryTransport(fail_times=99)
            for _ in range(2):
                box.flush(transport)
                clock[0] += 10_000
            assert len(Outbox(d).dead_letters) == 1

    def test_saturation_archive_avant_de_supprimer(self):
        """AUDIT P0-19 : la saturation supprimait sans laisser de trace."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            box = Outbox(d, max_entries=3)
            for _ in range(7):
                box.enqueue(self._event())
            assert box.dropped_on_overflow > 0
            assert box.stats()["dropped_on_overflow"] > 0
            assert len(box.dead_letters) == box.dropped_on_overflow

    def test_rejeu_manuel(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            clock = [0.0]
            box = Outbox(d, max_attempts=1, base_backoff_s=1.0, clock=lambda: clock[0])
            box.enqueue(self._event())
            box.flush(MemoryTransport(fail_times=99))
            assert len(box) == 0
            assert box.replay_dead_letters() == 1
            assert len(box) == 1
            assert box.flush(MemoryTransport())["sent"] == 1


class TestP0_14_PresetsPtz:
    def test_une_vue_un_seul_preset(self):
        """Le preset était déduit du rang dans la séquence de visite, où les vues
        prioritaires sont dupliquées : une même vue commandait plusieurs presets
        physiques, dont certains n'avaient jamais été enregistrés."""
        views = plan_uniform_ring(IMX675, LENS_27135, 5, 6_000)
        sched = ScanScheduler(views, 10.0, 2.0, priority_views=[views[0].view_id])
        slots = sched.plan_cycle()
        assert len(slots) == 10
        par_vue: dict[str, set[int]] = {}
        for s in slots:
            par_vue.setdefault(s.view.view_id, set()).add(s.preset)
        assert all(len(p) == 1 for p in par_vue.values())

    def test_presets_uniques_et_stables(self):
        views = plan_uniform_ring(IMX675, LENS_27135, 6, 6_000)
        sched = ScanScheduler(views, 10.0, 2.0)
        presets = sched.preset_map
        assert sorted(presets.values()) == [1, 2, 3, 4, 5, 6]
        assert sched.preset_map == presets


class TestP0_08_Memoire:
    def test_fond_stocke_en_uint8(self):
        """Le commentaire annonçait uint8, l'implémentation stockait float32 —
        quatre fois l'empreinte annoncée, soit 173 Mio pour une seule clé."""
        bank = BackgroundBank(buffer_size=3, min_samples=1)
        key = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 14, 0, tzinfo=UTC))
        bank.update(key, np.full((32, 32), 120.0))
        assert bank._buffers[key.as_str()][0].dtype == np.uint8

    def test_nombre_de_cles_borne(self):
        """Une clé par vue × créneau × saison × jour/nuit : plusieurs centaines
        sur une année, sans aucune éviction auparavant."""
        bank = BackgroundBank(buffer_size=2, min_samples=1, max_keys=4)
        for hour in range(0, 24, 2):
            k = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, hour, 0, tzinfo=UTC))
            bank.update(k, np.zeros((8, 8)))
        assert len(bank.maturity()) <= 4

    def test_max_keys_invalide(self):
        with pytest.raises(ValueError):
            BackgroundBank(max_keys=0)

    def test_persistance_conserve_le_format(self, tmp_path):
        bank = BackgroundBank(buffer_size=3, min_samples=1)
        key = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 14, 0, tzinfo=UTC))
        for v in (10.0, 20.0, 30.0):
            bank.update(key, np.full((8, 8), v))
        path = tmp_path / "b.npz"
        bank.save(path)
        np.testing.assert_allclose(BackgroundBank.load(path).reference(key), bank.reference(key))

    def test_pistes_purgees(self):
        pipe, scene = _pipe()
        _run(pipe, SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8))
        assert all(t.state != "DISMISSED" for t in pipe.tracker.tracks)


class TestP1_18_JourNuitConfigurable:
    def test_bornes_du_site_respectees(self):
        tot = dt.datetime(2026, 6, 21, 5, 30, tzinfo=UTC)
        assert BackgroundKey.build("V00", tot, sunrise_h=5.0, sunset_h=22.0).daynight == "crepuscule"
        assert BackgroundKey.build("V00", tot, sunrise_h=9.0, sunset_h=17.0).daynight == "nuit"

    def test_pipeline_transmet_les_bornes(self):
        cfg = tier_defaults("medium")
        cfg.sunrise_h, cfg.sunset_h = 5.0, 22.0
        cfg.operating.mode = "shadow"
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"))
        pipe.register_view("V00", 90.0, cfg.optics.focal_mm)
        scene = SyntheticScene(height=120, width=160, horizon_row=60)
        pipe.process_frame("V00", scene.frame(), dt.datetime(2026, 6, 21, 5, 30, tzinfo=UTC))
        assert any("crepuscule" in k for k in pipe.background.maturity())


class TestP0_04_MatriceMaterielle:
    def test_resolution_excessive_rejetee(self):
        """gk7605v100 (5 MP) + IMX415 (8,5 MP) était annoncé « prêt »."""
        r = board_readiness("gk7605v100", "IMX415")
        assert r["status"] == "resolution_exceeded"
        assert r["sensor_mp"] > r["max_sensor_mp"]

    def test_combinaison_dans_les_limites_acceptee(self):
        assert board_readiness("gk7605v100", "IMX335")["status"] == "ready"

    def test_resolutions_connues(self):
        assert sensor_megapixels("IMX335") == pytest.approx(5.0)
        assert sensor_megapixels("IMX999") is None

    def test_verdict_expose_les_deux_chiffres(self):
        r = board_readiness("hi3516av300", "IMX675")
        assert r["sensor_mp"] and r["max_sensor_mp"]


class TestP0_03_Capacites:
    def test_les_fonctions_non_raccordees_sont_declarees_absentes(self):
        from openvigie.hwcheck import capabilities

        caps = capabilities(tier_defaults("full"))
        for absent in ("modèle temporel", "segmentation du candidat",
                       "confirmation PTZ exécutée"):
            assert caps[absent][0] is False, absent

    def test_agent_continu_est_implemente_mais_exige_une_topologie(self):
        from openvigie.hwcheck import capabilities

        available, detail = capabilities(tier_defaults("full"))["agent continu"]
        assert available is False
        assert "agent.cameras" in detail

    def test_le_backend_effectif_est_annonce(self):
        from openvigie.hwcheck import capabilities

        assert capabilities(tier_defaults("medium"))["classification apprise"][0] is False

    def test_cli_capabilities(self, capsys):
        from openvigie.cli import main

        assert main(["capabilities", "-t", "full"]) == 0
        out = capsys.readouterr().out
        agent_line = next(line for line in out.splitlines() if "agent continu" in line)
        assert "✗" in agent_line and "agent.cameras" in agent_line

    def test_cli_capabilities_json(self, capsys):
        from openvigie.cli import main

        assert main(["capabilities", "-t", "full", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["agent continu"]["available"] is False


class TestP0_17_IdentifiantsUniques:
    def test_deux_alertes_dans_la_meme_seconde(self):
        from openvigie.alerting import make_alert_id

        ts = dt.datetime(2026, 8, 1, 14, 30, 5, tzinfo=UTC)
        ids = {make_alert_id("s", "V00", ts) for _ in range(200)}
        assert len(ids) == 200
