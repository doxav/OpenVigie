"""Tests des étages vision : recalage, modèle de fond, candidats, features."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from openvigie.background import BackgroundBank, BackgroundKey, daynight_of, season_of
from openvigie.candidates import (
    CandidateConfig,
    contrast_loss,
    extract_candidates,
    robust_threshold,
    translucency,
)
from openvigie.compat import binary_close, binary_open, label_components, to_gray
from openvigie.geometry import IMX675, flat_earth_distance_map, ground_mask
from openvigie.registration import (
    align_to_reference,
    phase_correlate,
    preset_repeatability,
    shift_image,
    vibration_index,
)
from openvigie.sources import SyntheticScene
from openvigie.tracking import Observation, Track, Tracker, blob_to_observation, compute_features


# --------------------------------------------------------------------------- #
class TestCompat:
    def test_label_components_deux_blobs(self):
        m = np.zeros((20, 20), dtype=bool)
        m[2:5, 2:5] = True
        m[12:16, 12:18] = True
        labels, n = label_components(m)
        assert n == 2
        assert set(np.unique(labels)) == {0, 1, 2}

    def test_ouverture_supprime_le_bruit_isole(self):
        m = np.zeros((20, 20), dtype=bool)
        m[10, 10] = True           # pixel isolé
        m[2:8, 2:8] = True         # bloc franc
        opened = binary_open(m)
        assert not opened[10, 10]
        assert opened[4, 4]

    def test_fermeture_bouche_les_trous(self):
        m = np.ones((12, 12), dtype=bool)
        m[6, 6] = False
        assert binary_close(m)[6, 6]

    def test_to_gray_accepte_2d_et_3d(self):
        assert to_gray(np.zeros((4, 5))).shape == (4, 5)
        assert to_gray(np.zeros((4, 5, 3))).shape == (4, 5)
        with pytest.raises(ValueError):
            to_gray(np.zeros((4,)))


# --------------------------------------------------------------------------- #
class TestRegistration:
    @pytest.mark.parametrize("dy,dx", [(0, 0), (3, 0), (0, -5), (7, 4), (-6, -3)])
    def test_recuperation_du_decalage(self, rng, dy, dx):
        base = rng.normal(120, 30, (96, 128)).astype(np.float32)
        shifted = shift_image(base, dy, dx)
        al = phase_correlate(base, shifted)
        # convention : la correction renvoyée est l'opposé du glissement subi
        assert al.dy == pytest.approx(-dy, abs=0.6)
        assert al.dx == pytest.approx(-dx, abs=0.6)

    def test_align_annule_le_decalage(self, rng):
        base = rng.normal(120, 30, (96, 128)).astype(np.float32)
        shifted = shift_image(base, 4, -3)
        aligned, al = align_to_reference(base, shifted)
        residual = phase_correlate(base, aligned)
        assert residual.magnitude_px < al.magnitude_px
        assert residual.magnitude_px < 1.0

    def test_decalage_aberrant_est_rejete(self, rng):
        base = rng.normal(120, 30, (96, 128)).astype(np.float32)
        other = rng.normal(120, 30, (96, 128)).astype(np.float32)
        _, al = align_to_reference(base, other, max_shift_px=2.0)
        assert al.method == "rejected"
        assert al.confidence == 0.0

    def test_formes_incompatibles(self):
        with pytest.raises(ValueError):
            phase_correlate(np.zeros((10, 10)), np.zeros((10, 12)))

    def test_repetabilite_preset(self, rng):
        base = rng.normal(120, 30, (96, 128)).astype(np.float32)
        frames = [base] + [shift_image(base, d, d) for d in (1, -2, 3)]
        stats = preset_repeatability(frames)
        assert stats["n_samples"] == 3
        assert 0 < stats["p95_px"] < 8

    def test_repetabilite_exige_deux_images(self):
        with pytest.raises(ValueError):
            preset_repeatability([np.zeros((8, 8))])

    def test_indice_vibration_augmente_avec_lamplitude(self, rng):
        base = rng.normal(120, 30, (96, 128)).astype(np.float32)
        stable = [shift_image(base, 0.1 * i, 0) for i in range(6)]
        agite = [shift_image(base, 6 * (-1) ** i, 0) for i in range(6)]
        assert vibration_index(agite) >= vibration_index(stable)


# --------------------------------------------------------------------------- #
class TestBackground:
    def test_saisons(self):
        assert season_of(dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)) == "hiver"
        assert season_of(dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)) == "printemps"
        assert season_of(dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc)) == "ete"
        assert season_of(dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)) == "automne"

    def test_jour_nuit_crepuscule(self):
        assert daynight_of(dt.datetime(2026, 7, 1, 14, 0, tzinfo=dt.timezone.utc)) == "jour"
        assert daynight_of(dt.datetime(2026, 7, 1, 2, 0, tzinfo=dt.timezone.utc)) == "nuit"
        assert daynight_of(dt.datetime(2026, 7, 1, 7, 0, tzinfo=dt.timezone.utc)) == "crepuscule"
        assert daynight_of(dt.datetime(2026, 7, 1, 21, 0, tzinfo=dt.timezone.utc)) == "crepuscule"

    def test_cles_distinctes_par_etat(self):
        k_jour = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 14, 0, tzinfo=dt.timezone.utc))
        k_nuit = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 2, 0, tzinfo=dt.timezone.utc))
        assert k_jour.as_str() != k_nuit.as_str()

    def test_reference_none_avant_maturite(self, rng):
        bank = BackgroundBank(buffer_size=5, min_samples=3)
        key = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 14, 0, tzinfo=dt.timezone.utc))
        assert bank.reference(key) is None
        for _ in range(2):
            bank.update(key, rng.normal(100, 5, (16, 16)))
        assert bank.reference(key) is None
        bank.update(key, rng.normal(100, 5, (16, 16)))
        assert bank.reference(key) is not None
        assert bank.is_ready(key)

    def test_mediane_robuste_a_une_image_aberrante(self):
        bank = BackgroundBank(buffer_size=5, min_samples=3)
        key = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 14, 0, tzinfo=dt.timezone.utc))
        for _ in range(4):
            bank.update(key, np.full((8, 8), 100.0))
        bank.update(key, np.full((8, 8), 250.0))  # image aberrante
        assert bank.reference(key).mean() == pytest.approx(100.0, abs=1.0)

    def test_tampon_circulaire_borne(self):
        bank = BackgroundBank(buffer_size=3, min_samples=1)
        key = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 14, 0, tzinfo=dt.timezone.utc))
        for i in range(10):
            bank.update(key, np.full((4, 4), float(i)))
        assert bank.maturity()[key.as_str()] == 3

    def test_persistance_disque(self, tmp_path):
        bank = BackgroundBank(buffer_size=4, min_samples=2)
        key = BackgroundKey.build("V00", dt.datetime(2026, 7, 1, 14, 0, tzinfo=dt.timezone.utc))
        for i in range(3):
            bank.update(key, np.full((6, 6), 50.0 + i))
        path = tmp_path / "bank.npz"
        bank.save(path)
        loaded = BackgroundBank.load(path)
        assert loaded.buffer_size == 4
        np.testing.assert_allclose(loaded.reference(key), bank.reference(key))


# --------------------------------------------------------------------------- #
class TestCandidates:
    def test_seuil_robuste_suit_la_distribution(self, rng):
        d = rng.normal(10, 2, 5000)
        thr = robust_threshold(d, k=4.0)
        assert 15 < thr < 21

    def test_seuil_insensible_a_un_offset_global(self, rng):
        d = rng.normal(10, 2, 5000)
        assert robust_threshold(d + 50, 4.0) == pytest.approx(robust_threshold(d, 4.0) + 50, abs=0.5)

    def test_panache_produit_un_candidat(self):
        scene = SyntheticScene(height=160, width=240, horizon_row=80)
        ref = scene.frame(noise=0.5)
        frame = scene.with_plume(cx=120, base_row=100, width_px=26, height_px=40, noise=0.5)
        dmap = flat_earth_distance_map(IMX675, 6.0, 40.0, 0.0)
        yi = np.linspace(0, dmap.shape[0] - 1, 160).astype(int)
        xi = np.linspace(0, dmap.shape[1] - 1, 240).astype(int)
        g = ground_mask(dmap[np.ix_(yi, xi)])
        blobs = extract_candidates(frame, ref, CandidateConfig(min_area_px=20), g)
        assert blobs, "le panache doit produire au moins un candidat"
        assert blobs[0].area_px >= 20
        assert blobs[0].bbox[3] > 80  # base sous l'horizon

    def test_scene_stable_ne_produit_rien(self):
        scene = SyntheticScene(height=160, width=240, horizon_row=80)
        ref = scene.frame(noise=0.5)
        frame = scene.frame(noise=0.5)
        assert extract_candidates(frame, ref, CandidateConfig(min_area_px=25)) == []

    def test_changement_global_est_rejete(self):
        scene = SyntheticScene(height=120, width=160, horizon_row=60)
        ref = scene.frame(noise=0.5)
        frame = np.clip(ref.astype(np.float32) + 60, 0, 255).astype(np.uint8)
        assert extract_candidates(frame, ref, CandidateConfig(max_area_frac=0.25)) == []

    def test_formes_incompatibles(self):
        with pytest.raises(ValueError):
            extract_candidates(np.zeros((10, 10, 3)), np.zeros((12, 12, 3)))

    def test_perte_de_contraste_positive_sous_fumee(self):
        scene = SyntheticScene(height=160, width=240, horizon_row=80)
        ref = scene.frame(noise=0.3)
        frame = scene.with_plume(cx=120, base_row=110, width_px=40, height_px=50, opacity=0.6, noise=0.3)
        loss = contrast_loss(frame, ref, (100, 70, 140, 110))
        assert 0.0 < loss <= 1.0

    def test_perte_de_contraste_nulle_sans_changement(self):
        scene = SyntheticScene(height=120, width=160, horizon_row=60)
        ref = scene.frame(noise=0.0)
        assert contrast_loss(ref, ref, (40, 70, 90, 110)) == pytest.approx(0.0, abs=1e-6)

    def test_translucidite_elevee_pour_fumee_fine(self):
        scene = SyntheticScene(height=160, width=240, horizon_row=80)
        ref = scene.frame(noise=0.2)
        fine = scene.with_plume(cx=120, base_row=120, width_px=30, height_px=40, opacity=0.2, noise=0.2)
        opaque = scene.with_plume(cx=120, base_row=120, width_px=30, height_px=40, opacity=0.95, noise=0.2)
        mask = np.zeros((160, 240), dtype=bool)
        mask[85:120, 105:135] = True
        assert translucency(fine, ref, mask) > translucency(opaque, ref, mask)


# --------------------------------------------------------------------------- #
def _blob(x0, y0, x1, y1, area=None):
    from openvigie.candidates import Blob

    mask = np.zeros((200, 320), dtype=bool)
    mask[y0:y1, x0:x1] = True
    return Blob(
        blob_id=1,
        bbox=(x0, y0, x1, y1),
        centroid=((x0 + x1) / 2, (y0 + y1) / 2),
        area_px=area or int(mask.sum()),
        mean_delta=12.0,
        mask=mask,
    )


class TestTracking:
    def test_iou(self):
        a, b = _blob(0, 0, 10, 10), _blob(5, 5, 15, 15)
        assert a.iou(a) == pytest.approx(1.0)
        assert 0 < a.iou(b) < 1
        assert a.iou(_blob(100, 100, 110, 110)) == 0.0

    def test_association_conserve_la_piste(self):
        tr = Tracker(iou_threshold=0.1)
        for i in range(4):
            obs = Observation(
                t=float(i * 30), blob=_blob(100, 100 - 2 * i, 130 + 2 * i, 130),
                area_m2=50.0 + 10 * i, centroid_y_m=0.0, distance_m=4000.0,
                contrast_loss=0.3, translucency=0.8, cnn_score=0.7,
            )
            tracks = tr.update("V00", [obs])
        assert len(tracks) == 1
        assert len(tracks[0].observations) == 4

    def test_deux_objets_distants_donnent_deux_pistes(self):
        tr = Tracker()
        obs = [
            Observation(0.0, _blob(10, 10, 30, 30), 10, 0, 3000, 0.2, 0.7),
            Observation(0.0, _blob(200, 150, 240, 190), 20, 0, 3000, 0.2, 0.7),
        ]
        assert len(tr.update("V00", obs)) == 2

    def test_piste_abandonnee_apres_absences(self):
        tr = Tracker(max_misses=1)
        tr.update("V00", [Observation(0.0, _blob(10, 10, 30, 30), 10, 0, 3000, 0.2, 0.7)])
        tr.update("V00", [])
        tr.update("V00", [])
        assert all(t.state == "DISMISSED" for t in tr.tracks)

    def test_pistes_isolees_par_vue(self):
        tr = Tracker()
        b = _blob(10, 10, 30, 30)
        tr.update("V00", [Observation(0.0, b, 10, 0, 3000, 0.2, 0.7)])
        tracks_v01 = tr.update("V01", [Observation(0.0, b, 10, 0, 3000, 0.2, 0.7)])
        assert len(tracks_v01) == 1
        assert len(tr.tracks) == 2


class TestFeatures:
    def _growing_track(self, upward=True, growing=True, below_horizon=True):
        track = Track(track_id=1, view_id="V00")
        y_base = 140 if below_horizon else 40
        for i in range(5):
            top = y_base - 30 - (6 * i if upward else 0)
            area = 40.0 + (25.0 * i if growing else 0.0)
            track.add(
                Observation(
                    t=float(i * 30),
                    blob=_blob(100, top, 130, y_base, area=int(area * 4)),
                    area_m2=area, centroid_y_m=0.0, distance_m=4000.0,
                    contrast_loss=0.35, translucency=0.8, cnn_score=0.72,
                )
            )
        return track

    def test_croissance_positive_detectee(self):
        f = compute_features(self._growing_track(), horizon_row=100)
        assert f.growth_m2_s > 0
        assert 0 < f.growth_score < 1

    def test_croissance_nulle_si_surface_stable(self):
        f = compute_features(self._growing_track(growing=False), horizon_row=100)
        assert f.growth_m2_s == pytest.approx(0.0, abs=1e-9)
        assert f.growth_score == 0.0

    def test_ascendance_positive_quand_le_sommet_monte(self):
        f = compute_features(self._growing_track(upward=True), horizon_row=100)
        assert f.upward_m_s > 0

    def test_origine_sol_selon_lhorizon(self):
        assert compute_features(self._growing_track(below_horizon=True), horizon_row=100).ground_origin == 1.0
        assert compute_features(self._growing_track(below_horizon=False), horizon_row=100).ground_origin == 0.0

    def test_persistance_saturee_a_1(self):
        f = compute_features(self._growing_track(), horizon_row=100, persistence_target=3)
        assert f.persistence == 1.0

    def _drifting_track(self, az_step_deg: float, base_az: float = 180.0) -> Track:
        """Piste dont l'azimut dérive : la cohérence au vent se mesure en repère
        géographique, pas en coordonnées image (AUDIT P0-11)."""
        track = Track(track_id=1, view_id="V00")
        for i in range(5):
            track.add(
                Observation(
                    t=float(i * 30), blob=_blob(100 + 5 * i, 100, 130 + 5 * i, 140),
                    area_m2=50.0, centroid_y_m=0.0, distance_m=4000.0,
                    contrast_loss=0.3, translucency=0.8,
                    azimuth_deg=base_az + i * az_step_deg,
                )
            )
        return track

    def test_coherence_vent(self):
        """Une dérive vers l'ouest (azimut croissant depuis le sud) est cohérente
        avec un vent qui pousse vers l'ouest, pas vers l'est."""
        track = self._drifting_track(az_step_deg=0.05, base_az=180.0)
        vers_ouest = compute_features(track, horizon_row=90, wind_bearing_deg=270.0)
        vers_est = compute_features(track, horizon_row=90, wind_bearing_deg=90.0)
        assert vers_ouest.wind_coherence > vers_est.wind_coherence

    def test_coherence_vent_independante_de_lorientation_camera(self):
        """Le défaut corrigé : la même fumée réelle donnait des scores opposés
        selon l'azimut de la caméra, parce que la dérive était mesurée en pixels."""
        sud = compute_features(self._drifting_track(0.05, base_az=180.0),
                               horizon_row=90, wind_bearing_deg=270.0)
        nord = compute_features(self._drifting_track(0.05, base_az=0.0),
                                horizon_row=90, wind_bearing_deg=90.0)
        assert sud.wind_coherence == pytest.approx(nord.wind_coherence, abs=0.02)

    def test_vent_le_long_de_la_ligne_de_visee_est_neutre(self):
        """Quand le vent souffle dans l'axe de visée, l'image ne dit rien : le
        critère doit se déclarer muet (0,5) et non confirmer."""
        f = compute_features(self._drifting_track(0.05, base_az=180.0),
                             horizon_row=90, wind_bearing_deg=180.0)
        assert f.wind_coherence == pytest.approx(0.5, abs=0.05)

    def test_sans_azimut_pas_de_score_de_vent(self):
        track = Track(track_id=1, view_id="V00")
        for i in range(5):
            track.add(Observation(t=float(i * 30), blob=_blob(100, 100, 130, 140),
                                  area_m2=50.0, centroid_y_m=0.0, distance_m=4000.0,
                                  contrast_loss=0.3, translucency=0.8))
        assert compute_features(track, horizon_row=90, wind_bearing_deg=90.0).wind_coherence == 0.0

    def test_piste_vide(self):
        assert compute_features(Track(track_id=1, view_id="V00"), horizon_row=90).persistence == 0.0

    def test_conversion_surface_reelle_via_distance(self):
        blob = _blob(100, 100, 140, 140)
        dmap = np.full((200, 320), 5000.0)
        near = blob_to_observation(0.0, blob, np.full((200, 320), 2000.0), IMX675, 6.0, 0.3, 0.8)
        far = blob_to_observation(0.0, blob, dmap, IMX675, 6.0, 0.3, 0.8)
        assert far.area_m2 > near.area_m2
        assert far.area_m2 / near.area_m2 == pytest.approx((5000 / 2000) ** 2, rel=1e-6)

    def test_surface_nulle_au_dessus_de_lhorizon(self):
        blob = _blob(100, 20, 140, 60)
        obs = blob_to_observation(0.0, blob, np.full((200, 320), np.inf), IMX675, 6.0, 0.3, 0.8)
        assert obs.area_m2 == 0.0
        assert not np.isfinite(obs.distance_m)
