"""Tests des deux contributions destinées à être proposées en amont.

Le test central est ``TestRejeuIncidentProduction`` : il rejoue le scénario du
vol de détections entre deux feux, **démontre que la logique historique
échoue** et que la nouvelle réussit sur exactement les mêmes données. Sans ce
côte-à-côte, l'affirmation « c'est plus robuste » ne serait qu'une opinion.
"""

from __future__ import annotations

import numpy as np
import pytest

from openvigie.association import (
    AssociationConfig,
    TrackState,
    assign_batch,
    associate_detection,
    associate_legacy,
    box_area,
    box_center,
    box_coverage,
    box_iou,
    boxes_overlap_legacy,
    score_match,
)
from openvigie.posehealth import (
    PoseFingerprintRegistry,
    hamming_distance,
    perceptual_hash,
    similarity,
)


def track(track_id: str, boxes, t0: float = 0.0, dt: float = 30.0, window: int = 5) -> TrackState:
    tr = TrackState(track_id=track_id, history_window=window)
    for i, b in enumerate(boxes):
        tr.add(b, t0 + i * dt)
    return tr


# =========================================================================== #
# Primitives géométriques
# =========================================================================== #
class TestPrimitives:
    def test_iou_identique(self):
        assert box_iou((0, 0, 1, 1), (0, 0, 1, 1)) == pytest.approx(1.0)

    def test_iou_disjointes(self):
        assert box_iou((0, 0, 0.1, 0.1), (0.5, 0.5, 0.6, 0.6)) == 0.0

    def test_iou_partielle(self):
        assert box_iou((0, 0, 0.2, 0.2), (0.1, 0.1, 0.3, 0.3)) == pytest.approx(1 / 7, abs=0.01)

    def test_aire_et_centre(self):
        assert box_area((0.1, 0.2, 0.3, 0.5)) == pytest.approx(0.06)
        assert box_center((0, 0, 0.4, 0.2)) == pytest.approx((0.2, 0.1))

    def test_couverture_distincte_de_liou(self):
        """Une petite boîte incluse dans une grande : couverture 1, IoU faible.
        C'est exactement la signature d'une boîte géante qui en avale une autre,
        et c'est pourquoi les deux mesures sont nécessaires."""
        petite = (0.45, 0.45, 0.5, 0.5)
        geante = (0.0, 0.0, 1.0, 1.0)
        assert box_coverage(petite, geante) == pytest.approx(1.0)
        assert box_iou(petite, geante) < 0.01

    def test_couverture_disjointe(self):
        assert box_coverage((0, 0, 0.1, 0.1), (0.5, 0.5, 0.6, 0.6)) == 0.0


# =========================================================================== #
# Le test qui justifie la contribution
# =========================================================================== #
class TestRejeuIncidentProduction:
    """Deux feux sur la même pose, une boîte anormalement grande sur l'un.

    Scénario reproduit d'un incident réel : la grande boîte a été rattachée à
    la mauvaise séquence, puis a absorbé les détections de l'autre feu pendant
    2 h 30, présentant à l'opérateur les images d'un feu à la position d'un
    autre.
    """

    def _scene(self):
        # Feu A, en haut à gauche, boîtes petites et stables.
        feu_a = track("A", [(0.10, 0.10, 0.16, 0.16),
                            (0.10, 0.10, 0.17, 0.17),
                            (0.11, 0.10, 0.18, 0.17)], t0=0.0)
        # Feu B, en bas à droite, mais avec UNE boîte aberrante qui couvre
        # presque toute l'image (détecteur permissif sur une brume). B a été vu
        # une trame plus tard que A : c'est exactement ce qui le place en tête
        # de la liste triée par récence, donc examiné en premier.
        feu_b = track("B", [(0.70, 0.70, 0.78, 0.78),
                            (0.70, 0.70, 0.79, 0.79),
                            (0.02, 0.02, 0.98, 0.98)], t0=15.0)
        return feu_a, feu_b

    def test_la_logique_historique_vole_la_detection(self):
        """B a été vu en dernier et sa boîte géante chevauche tout : il gagne."""
        feu_a, feu_b = self._scene()
        nouvelle_detection_de_a = (0.11, 0.11, 0.18, 0.18)
        vole_par = associate_legacy([feu_a, feu_b], nouvelle_detection_de_a, t=90.0)
        assert vole_par == "B", "le scénario doit reproduire le vol"

    def test_la_nouvelle_logique_attribue_correctement(self):
        feu_a, feu_b = self._scene()
        nouvelle_detection_de_a = (0.11, 0.11, 0.18, 0.18)
        result = associate_detection([feu_a, feu_b], nouvelle_detection_de_a, t=90.0)
        assert result.matched_track_id == "A"

    def test_la_boite_geante_est_rejetee_avec_un_motif(self):
        """Le rejet doit être explicable, pas silencieux."""
        _feu_a, feu_b = self._scene()
        s = score_match(feu_b, (0.11, 0.11, 0.18, 0.18), t=90.0)
        assert not s.accepted
        assert "surface" in s.rejected_reason or "déplacement" in s.rejected_reason

    def test_la_mediane_protege_lidentite_de_la_piste(self):
        """Une seule boîte aberrante ne doit pas déplacer durablement la piste :
        c'est ce qui rendait le vol irréversible."""
        _feu_a, feu_b = self._scene()
        assert box_area(feu_b.last_box) > 0.9        # la dernière boîte est géante
        assert box_area(feu_b.reference_box) < 0.05  # la référence médiane ne l'est pas

    def test_la_piste_volee_se_reprend_ensuite(self):
        """Après la boîte aberrante, B redevient identifiable sur ses propres
        détections — le vol n'est plus irréversible."""
        _feu_a, feu_b = self._scene()
        s = score_match(feu_b, (0.71, 0.71, 0.79, 0.79), t=90.0)
        assert s.accepted and s.quality > 0.3


# =========================================================================== #
# Association : propriétés
# =========================================================================== #
class TestAssociation:
    def test_meilleur_match_et_non_premier(self):
        """Un candidat vu plus récemment mais moins bien placé ne doit pas
        l'emporter sur un candidat parfaitement aligné."""
        bon = track("bon", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        recent_mais_mediocre = track("recent", [(0.17, 0.17, 0.40, 0.40)], t0=50.0)
        detection = (0.10, 0.10, 0.20, 0.20)
        assert associate_legacy([bon, recent_mais_mediocre], detection, t=60.0) == "recent"
        assert associate_detection([bon, recent_mais_mediocre], detection, t=60.0).matched_track_id == "bon"

    def test_ambiguite_refusee(self):
        """Deux pistes également plausibles : ouvrir une nouvelle piste plutôt
        que trancher au hasard."""
        a = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        b = track("b", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        r = associate_detection([a, b], (0.10, 0.10, 0.20, 0.20), t=30.0)
        assert not r.matched
        assert "ambiguïté" in r.reason

    def test_aucune_piste(self):
        r = associate_detection([], (0.1, 0.1, 0.2, 0.2), t=10.0)
        assert not r.matched and "aucun candidat" in r.reason

    def test_ecart_temporel_coupe_les_episodes(self):
        """Une fenêtre trop longue permet de relier des événements sans rapport."""
        tr = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        cfg = AssociationConfig(max_gap_s=900.0)
        proche = associate_detection([tr], (0.10, 0.10, 0.20, 0.20), t=300.0, cfg=cfg)
        lointain = associate_detection([tr], (0.10, 0.10, 0.20, 0.20), t=7200.0, cfg=cfg)
        assert proche.matched
        assert not lointain.matched

    def test_detection_anterieure_refusee(self):
        tr = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=100.0)
        s = score_match(tr, (0.10, 0.10, 0.20, 0.20), t=50.0)
        assert not s.accepted and "antérieure" in s.rejected_reason

    def test_deplacement_trop_rapide_refuse(self):
        tr = track("a", [(0.05, 0.05, 0.10, 0.10)], t0=0.0)
        s = score_match(tr, (0.85, 0.85, 0.90, 0.90), t=30.0)
        assert not s.accepted

    def test_qualite_decroit_avec_leloignement(self):
        tr = track("a", [(0.40, 0.40, 0.50, 0.50)], t0=0.0)
        proche = score_match(tr, (0.41, 0.41, 0.51, 0.51), t=30.0)
        loin = score_match(tr, (0.48, 0.48, 0.58, 0.58), t=30.0)
        assert proche.quality > loin.quality

    def test_piste_vide(self):
        s = score_match(TrackState("vide"), (0.1, 0.1, 0.2, 0.2), t=0.0)
        assert not s.accepted and "sans observation" in s.rejected_reason

    def test_score_serialisable(self):
        tr = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        d = score_match(tr, (0.10, 0.10, 0.20, 0.20), t=30.0).as_dict()
        assert {"track_id", "quality", "iou", "rejected_reason"} <= set(d)

    def test_resultat_serialisable(self):
        tr = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        d = associate_detection([tr], (0.10, 0.10, 0.20, 0.20), t=30.0).as_dict()
        assert d["matched_track_id"] == "a" and len(d["scores"]) == 1

    def test_best_expose_le_meilleur_accepte(self):
        a = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        b = track("b", [(0.60, 0.60, 0.70, 0.70)], t0=0.0)
        r = associate_detection([a, b], (0.10, 0.10, 0.20, 0.20), t=30.0)
        assert r.best is not None and r.best.track_id == "a"

    def test_config_invalide(self):
        with pytest.raises(ValueError):
            AssociationConfig(w_iou=0, w_center=0, w_size=0)
        with pytest.raises(ValueError):
            AssociationConfig(min_quality=2.0)
        with pytest.raises(ValueError):
            AssociationConfig(max_area_ratio=0.5)


class TestAffectationGlobale:
    def test_pas_de_dependance_a_lordre(self):
        """Traiter les détections une par une peut donner un résultat
        dépendant de l'ordre ; l'affectation globale l'évite."""
        a = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        b = track("b", [(0.60, 0.60, 0.70, 0.70)], t0=0.0)
        detections = [((0.60, 0.60, 0.70, 0.70), 30.0), ((0.10, 0.10, 0.20, 0.20), 30.0)]
        assignment = assign_batch([a, b], detections)
        assert assignment[0] == "b"
        assert assignment[1] == "a"

    def test_une_piste_ne_prend_quune_detection(self):
        a = track("a", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        detections = [((0.10, 0.10, 0.20, 0.20), 30.0), ((0.11, 0.11, 0.21, 0.21), 30.0)]
        assignment = assign_batch([a], detections)
        assert len(assignment) == 1

    def test_lot_vide(self):
        assert assign_batch([], []) == {}


class TestCompatibiliteLegacy:
    """La logique historique est reproduite fidèlement, sinon la comparaison
    côte-à-côte ne prouverait rien."""

    def test_chevauchement_avec_tolerance(self):
        assert boxes_overlap_legacy((0.0, 0.0, 0.1, 0.1), (0.12, 0.0, 0.2, 0.1), tolerance=0.05)
        assert not boxes_overlap_legacy((0.0, 0.0, 0.1, 0.1), (0.5, 0.0, 0.6, 0.1), tolerance=0.05)

    def test_ordre_par_recence(self):
        ancien = track("ancien", [(0.10, 0.10, 0.20, 0.20)], t0=0.0)
        recent = track("recent", [(0.10, 0.10, 0.20, 0.20)], t0=100.0)
        assert associate_legacy([ancien, recent], (0.10, 0.10, 0.20, 0.20), t=200.0) == "recent"


# =========================================================================== #
# Santé sémantique PTZ
# =========================================================================== #
def scene(seed: int, height: int = 64, width: int = 96) -> np.ndarray:
    """Paysage synthétique déterministe et structuré (pas du bruit pur).

    Un bruit blanc donnerait des empreintes aléatoires, donc un test trop
    facile. On construit une structure lisible — relief, ciel — pour que le
    dHash mesure bien de la structure.
    """
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 4 * np.pi, width)
    horizon = height // 2 + (6 * np.sin(x + seed)).astype(int)
    img = np.zeros((height, width), dtype=np.float32)
    for c in range(width):
        img[: horizon[c], c] = 200 + 5 * np.sin(c / 7.0 + seed)
        img[horizon[c] :, c] = 90 + 30 * rng.random()
    return img


class TestEmpreinte:
    def test_taille_attendue(self):
        assert perceptual_hash(scene(1), hash_size=8).size == 64

    def test_image_identique_meme_empreinte(self):
        img = scene(1)
        assert hamming_distance(perceptual_hash(img), perceptual_hash(img)) == 0

    def test_insensible_a_lexposition(self):
        """Une même scène plus lumineuse doit garder son empreinte : sinon on
        signalerait une panne à chaque passage nuageux."""
        img = scene(1)
        # Gain affine sans écrêtage : le dHash compare des voisins, donc une
        # transformation monotone doit laisser l'empreinte strictement
        # inchangée. Saturer le ciel testerait l'écrêtage, pas l'exposition.
        plus_clair = img * 1.15 + 10.0
        assert plus_clair.max() < 255.0
        assert similarity(perceptual_hash(img), perceptual_hash(plus_clair)) == pytest.approx(1.0)

    def test_scenes_differentes_empreintes_eloignees(self):
        assert similarity(perceptual_hash(scene(1)), perceptual_hash(scene(7))) < 0.9

    def test_accepte_la_couleur(self):
        rgb = np.stack([scene(3)] * 3, axis=-1)
        assert perceptual_hash(rgb).size == 64

    def test_format_invalide(self):
        with pytest.raises(ValueError):
            perceptual_hash(np.zeros((5,)))

    def test_hash_size_invalide(self):
        with pytest.raises(ValueError):
            perceptual_hash(scene(1), hash_size=1)

    def test_tailles_incompatibles(self):
        with pytest.raises(ValueError):
            hamming_distance(perceptual_hash(scene(1), 8), perceptual_hash(scene(1), 4))


class TestTeteBloquee:
    def _registry(self, clock_ref):
        return PoseFingerprintRegistry("cam-1", ttl_s=900.0, clock=lambda: clock_ref[0])

    def test_tete_saine(self):
        clock = [1000.0]
        reg = self._registry(clock)
        for i in range(4):
            reg.record(f"P{i}", scene(i * 11 + 1))
        rapport = reg.report()
        assert rapport.status == "ok"
        assert not rapport.stuck
        assert rapport.distinct_poses == 4

    def test_tete_bloquee_detectee(self):
        """Le cas qui rend une zone aveugle invisible : la caméra répond, les
        images sont valides, mais toutes les poses montrent la même scène."""
        clock = [1000.0]
        reg = self._registry(clock)
        immobile = scene(5)
        for i in range(4):
            reg.record(f"P{i}", immobile)
        rapport = reg.report()
        assert rapport.stuck
        assert rapport.status == "stuck"
        assert rapport.distinct_poses == 1
        assert len(rapport.collisions) == 6      # toutes les paires

    def test_blocage_partiel_detecte(self):
        """Deux poses seulement se confondent : cela suffit à fausser des
        azimuts, donc cela doit être signalé."""
        clock = [1000.0]
        reg = self._registry(clock)
        reg.record("P0", scene(1))
        reg.record("P1", scene(20))
        bloquee = scene(40)
        reg.record("P2", bloquee)
        reg.record("P3", bloquee)
        rapport = reg.report()
        assert rapport.stuck
        assert len(rapport.collisions) == 1
        assert {rapport.collisions[0].pose_a, rapport.collisions[0].pose_b} == {"P2", "P3"}

    def test_message_explicite(self):
        clock = [1000.0]
        reg = self._registry(clock)
        immobile = scene(5)
        reg.record("P0", immobile)
        reg.record("P1", immobile)
        assert "azimuts" in reg.report().message

    def test_serialisation(self):
        clock = [1000.0]
        reg = self._registry(clock)
        reg.record("P0", scene(1))
        d = reg.report().as_dict()
        assert {"camera_id", "status", "stuck", "collisions"} <= set(d)


class TestFraicheur:
    def test_image_perimee_detectee(self):
        """Une caméra hors ligne qui resert son dernier cliché depuis un cache
        paraîtrait vivante indéfiniment sans ce test."""
        clock = [1000.0]
        reg = PoseFingerprintRegistry("cam-1", ttl_s=600.0, clock=lambda: clock[0])
        reg.record("P0", scene(1))
        assert reg.report().status == "ok"
        clock[0] += 1200
        rapport = reg.report()
        assert rapport.status == "stale"
        assert rapport.stale_poses == ["P0"]

    def test_horodatage_de_capture_prioritaire(self):
        """C'est l'instant de CAPTURE qui compte, pas celui de lecture : un
        cache renvoie une lecture récente pour une capture ancienne."""
        clock = [10_000.0]
        reg = PoseFingerprintRegistry("cam-1", ttl_s=600.0, clock=lambda: clock[0])
        reg.record("P0", scene(1), captured_at=clock[0] - 3600)
        assert reg.report().stale_poses == ["P0"]

    def test_ttl_invalide(self):
        with pytest.raises(ValueError):
            PoseFingerprintRegistry("c", ttl_s=0)

    def test_seuil_invalide(self):
        with pytest.raises(ValueError):
            PoseFingerprintRegistry("c", collision_threshold=1.5)


class TestDerive:
    def test_derive_mesurable(self):
        """Une dérive progressive du cadrage doit être détectable avant de
        devenir une collision franche."""
        reg = PoseFingerprintRegistry("cam-1")
        reg.record("P0", scene(1))
        assert reg.drift_since("P0", scene(1)) == pytest.approx(1.0)
        assert reg.drift_since("P0", scene(9)) < 1.0

    def test_pose_inconnue(self):
        assert PoseFingerprintRegistry("cam-1").drift_since("P9", scene(1)) is None

    def test_poses_connues(self):
        reg = PoseFingerprintRegistry("cam-1")
        reg.record("P1", scene(1))
        reg.record("P0", scene(2))
        assert reg.known_poses == ["P0", "P1"]
