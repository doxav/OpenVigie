"""Tests des contributions upstream nº3 et nº4.

Deux tests portent l'essentiel de la démonstration :

- ``TestRegressionDeguiseeEnProgres`` : un modèle dont le rappel **global**
  s'améliore alors qu'il perd les petites fumées lointaines — exactement le cas
  qu'un classement par F1 laisse passer ;
- ``TestCollisionInterClasses`` : un identifiant présent à la fois en « feu » et
  en « faux positif », qu'un contrôle de fuite entre splits ne voit pas.
"""

from __future__ import annotations

import pytest

from openvigie.dataintegrity import (
    BuildComparison,
    IntegrityReport,
    SplitLedger,
    assert_no_class_collision,
    compare_builds,
    file_manifest,
    find_class_collisions,
    manifest_hash,
)
from openvigie.opmetrics import (
    ConfigPoint,
    GateConfig,
    SequenceOutcome,
    compute_operational_metrics,
    detection_delays,
    fp_budget_to_max_fpr,
    fpr_to_fp_per_camera_per_day,
    frames_per_camera_per_day,
    gate_report,
    pareto_front,
    release_gate,
    select_under_budget,
    stratified_recall,
)


def fire(sid, detected=True, size=100.0, distance=2000.0, ignition=0.0, delay=60.0):
    return SequenceOutcome(
        sequence_id=sid, is_wildfire=True, ignition_at=ignition,
        first_alert_at=(ignition + delay) if detected else None,
        plume_size_px=size, distance_m=distance,
    )


def non_fire(sid, alerted=False):
    return SequenceOutcome(
        sequence_id=sid, is_wildfire=False,
        first_alert_at=0.0 if alerted else None,
    )


# =========================================================================== #
# Conversion benchmark → exploitation
# =========================================================================== #
class TestChargeOperationnelle:
    def test_images_par_jour(self):
        """Une image toutes les 30 s, 24 h sur 24 : 2 880 images par jour."""
        assert frames_per_camera_per_day(seconds_per_pose=30.0) == pytest.approx(2880.0)

    def test_un_fpr_anodin_devient_ingerable(self):
        """Le chiffre qui manque le plus souvent : 5 % de FPR sur un benchmark,
        c'est 144 fausses alertes par caméra et par jour."""
        fpd = frames_per_camera_per_day(seconds_per_pose=30.0)
        assert fpr_to_fp_per_camera_per_day(0.05, fpd) == pytest.approx(144.0)

    def test_un_fpr_de_benchmark_eleve_est_absurde(self):
        """Un FPR de 0,47 — observé sur un modèle au rappel pourtant élevé —
        représente plus de mille fausses alertes quotidiennes par caméra."""
        fpd = frames_per_camera_per_day(seconds_per_pose=30.0)
        assert fpr_to_fp_per_camera_per_day(0.47, fpd) > 1000

    def test_budget_vers_fpr_maximal(self):
        """La façon dont un seuil devrait être choisi : partir de ce que
        l'exploitant accepte."""
        fpd = frames_per_camera_per_day(seconds_per_pose=30.0)
        max_fpr = fp_budget_to_max_fpr(1.0, fpd)
        assert max_fpr == pytest.approx(1 / 2880, rel=1e-6)
        assert fpr_to_fp_per_camera_per_day(max_fpr, fpd) == pytest.approx(1.0)

    def test_conversions_inverses(self):
        fpd = frames_per_camera_per_day()
        assert fpr_to_fp_per_camera_per_day(fp_budget_to_max_fpr(5.0, fpd), fpd) == pytest.approx(5.0)

    def test_parametres_invalides(self):
        with pytest.raises(ValueError):
            frames_per_camera_per_day(seconds_per_pose=0)
        with pytest.raises(ValueError):
            fpr_to_fp_per_camera_per_day(1.5, 100)
        with pytest.raises(ValueError):
            fp_budget_to_max_fpr(1.0, 0)


# =========================================================================== #
# Délais
# =========================================================================== #
class TestDelais:
    def test_mediane_et_p90(self):
        outcomes = [fire(f"f{i}", delay=d) for i, d in enumerate([60, 120, 180, 240, 3600])]
        stats = detection_delays(outcomes)
        assert stats.median_s == pytest.approx(180.0)
        assert stats.p90_s > stats.median_s

    def test_la_queue_est_visible(self):
        """Une médiane excellente peut cacher qu'un feu sur dix met une heure :
        c'est ce que la p90 rend visible."""
        outcomes = [fire(f"f{i}", delay=60) for i in range(9)] + [fire("lent", delay=3600)]
        stats = detection_delays(outcomes)
        assert stats.median_s == pytest.approx(60.0)
        assert stats.max_s == pytest.approx(3600.0)

    def test_non_detectes_comptes_et_non_imputes(self):
        """Un feu manqué ne doit pas entrer dans la médiane avec une valeur
        arbitraire : il est compté à part."""
        outcomes = [fire("a", delay=60), fire("b", detected=False)]
        stats = detection_delays(outcomes)
        assert stats.n_undetected == 1
        assert stats.median_s == pytest.approx(60.0)

    def test_ignition_inconnue_ignoree(self):
        o = SequenceOutcome("x", is_wildfire=True, ignition_at=None, first_alert_at=100.0)
        assert o.time_to_detect_s is None

    def test_aucun_feu(self):
        stats = detection_delays([non_fire("n1")])
        assert stats.n == 0 and stats.median_s is None

    def test_serialisation(self):
        d = detection_delays([fire("a", delay=120)]).as_dict()
        assert d["median_min"] == pytest.approx(2.0)


# =========================================================================== #
# Stratification
# =========================================================================== #
class TestStratification:
    def test_rappel_par_taille(self):
        outcomes = [
            fire("petit1", detected=False, size=10.0),
            fire("petit2", detected=False, size=15.0),
            fire("gros1", detected=True, size=300.0),
            fire("gros2", detected=True, size=400.0),
        ]
        strata = stratified_recall(outcomes, by="plume_size_px")
        by_label = {s.stratum: s for s in strata}
        petit = next(s for k, s in by_label.items() if k.startswith("[0"))
        assert petit.recall == 0.0

    def test_le_rappel_global_masque_la_strate_faible(self):
        """Le cœur du problème : 80 % de rappel global, 0 % sur les petites."""
        outcomes = [fire(f"p{i}", detected=False, size=10.0) for i in range(2)]
        outcomes += [fire(f"g{i}", detected=True, size=300.0) for i in range(8)]
        m = compute_operational_metrics(outcomes, observation_days=1.0)
        assert m.recall == pytest.approx(0.8)
        assert m.worst_stratum.recall == 0.0

    def test_effectif_expose(self):
        """Un rappel de 1,0 sur deux séquences ne veut rien dire : l'effectif
        doit rester visible."""
        s = stratified_recall([fire("a", size=10.0)], by="plume_size_px")[0]
        assert s.n == 1 and s.recall == 1.0

    def test_axe_distance(self):
        outcomes = [
            fire("proche", detected=True, distance=1000.0),
            fire("lointain", detected=False, distance=12000.0),
        ]
        strata = stratified_recall(outcomes, by="distance_m", unit="m")
        assert len(strata) == 2
        assert min(s.recall for s in strata) == 0.0

    def test_valeurs_absentes_ignorees(self):
        o = SequenceOutcome("x", is_wildfire=True, plume_size_px=None, first_alert_at=1.0)
        assert stratified_recall([o], by="plume_size_px") == []

    def test_non_feux_exclus(self):
        assert stratified_recall([non_fire("n")], by="plume_size_px") == []


# =========================================================================== #
# Bilan opérationnel
# =========================================================================== #
class TestBilanOperationnel:
    def test_fp_par_camera_par_jour(self):
        outcomes = [non_fire(f"n{i}", alerted=True) for i in range(20)]
        m = compute_operational_metrics(outcomes, observation_days=10.0, n_cameras=2)
        assert m.fp_per_camera_per_day == pytest.approx(1.0)

    def test_rappel(self):
        outcomes = [fire("a"), fire("b"), fire("c", detected=False)]
        m = compute_operational_metrics(outcomes, observation_days=1.0)
        assert m.recall == pytest.approx(2 / 3)

    def test_aucun_feu_rappel_nul_sans_division_par_zero(self):
        m = compute_operational_metrics([non_fire("n")], observation_days=1.0)
        assert m.recall == 0.0

    def test_parametres_invalides(self):
        with pytest.raises(ValueError):
            compute_operational_metrics([], observation_days=0)
        with pytest.raises(ValueError):
            compute_operational_metrics([], observation_days=1, n_cameras=0)

    def test_serialisation(self):
        d = compute_operational_metrics([fire("a")], observation_days=1.0).as_dict()
        assert {"recall", "fp_per_camera_per_day", "delays", "worst_stratum"} <= set(d)


# =========================================================================== #
# LE test central : la régression déguisée en progrès
# =========================================================================== #
class TestRegressionDeguiseeEnProgres:
    """Un modèle qui améliore son rappel global tout en perdant les petites
    fumées lointaines. C'est le cas qu'un classement agrégé laisse passer, et
    la raison d'être de la garde strate par strate."""

    def _baseline(self):
        petites = [fire(f"p{i}", detected=i < 6, size=10.0, distance=12000.0) for i in range(10)]
        grosses = [fire(f"g{i}", detected=i < 6, size=300.0, distance=1000.0) for i in range(10)]
        return compute_operational_metrics(petites + grosses, observation_days=10.0)

    def _candidate(self):
        # Perd 4 petites, gagne 4 grosses : le rappel global est INCHANGÉ,
        # voire meilleur, mais la capacité utile s'est effondrée.
        petites = [fire(f"p{i}", detected=i < 2, size=10.0, distance=12000.0) for i in range(10)]
        grosses = [fire(f"g{i}", detected=True, size=300.0, distance=1000.0) for i in range(10)]
        return compute_operational_metrics(petites + grosses, observation_days=10.0)

    def test_le_rappel_global_ne_signale_rien(self):
        base, cand = self._baseline(), self._candidate()
        assert cand.recall >= base.recall, "le candidat paraît meilleur en agrégé"

    def test_la_garde_attrape_la_regression(self):
        violations = release_gate(self._baseline(), self._candidate())
        assert violations
        assert any(v.kind == "stratum_recall" for v in violations)

    def test_le_rapport_explique_pourquoi(self):
        rapport = gate_report(release_gate(self._baseline(), self._candidate()))
        assert "strate" in rapport

    def test_la_pire_strate_est_identifiee(self):
        assert self._candidate().worst_stratum.recall == pytest.approx(0.2)


class TestGarde:
    def _metrics(self, recall_detected, n=10, fp=10, days=10.0, delay=60.0):
        fires = [fire(f"f{i}", detected=i < recall_detected, size=100.0, delay=delay)
                 for i in range(n)]
        fps = [non_fire(f"n{i}", alerted=True) for i in range(fp)]
        return compute_operational_metrics(fires + fps, observation_days=days)

    def test_version_identique_passe(self):
        assert release_gate(self._metrics(8), self._metrics(8)) == []

    def test_amelioration_passe(self):
        assert release_gate(self._metrics(8), self._metrics(10)) == []

    def test_chute_de_rappel_bloquee(self):
        violations = release_gate(self._metrics(10), self._metrics(5))
        assert any(v.kind == "recall" for v in violations)

    def test_explosion_des_faux_positifs_bloquee(self):
        violations = release_gate(self._metrics(8, fp=10), self._metrics(8, fp=40))
        assert any(v.kind == "fp_per_day" for v in violations)

    def test_allongement_du_delai_bloque(self):
        violations = release_gate(self._metrics(8, delay=60), self._metrics(8, delay=600))
        assert any(v.kind == "delay" for v in violations)

    def test_petite_strate_non_opposable(self):
        """Bloquer une release sur un rappel calculé sur deux séquences ferait
        perdre confiance dans la garde elle-même."""
        base = compute_operational_metrics(
            [fire(f"a{i}", detected=True, size=10.0) for i in range(2)], observation_days=1.0)
        cand = compute_operational_metrics(
            [fire(f"a{i}", detected=False, size=10.0) for i in range(2)], observation_days=1.0)
        violations = release_gate(base, cand, GateConfig(min_stratum_size=5, max_recall_drop=1.0))
        assert not any(v.kind == "stratum_recall" for v in violations)

    def test_rapport_vide_si_conforme(self):
        assert "Aucune régression" in gate_report([])

    def test_violation_serialisable(self):
        v = release_gate(self._metrics(10), self._metrics(5))[0]
        assert {"kind", "detail", "baseline", "candidate"} <= set(v.as_dict())


# =========================================================================== #
# Pareto
# =========================================================================== #
class TestPareto:
    def test_domination(self):
        bon = ConfigPoint("bon", recall=0.9, fp_per_camera_per_day=1.0, median_delay_s=60)
        mauvais = ConfigPoint("mauvais", recall=0.8, fp_per_camera_per_day=2.0, median_delay_s=120)
        assert bon.dominates(mauvais)
        assert not mauvais.dominates(bon)

    def test_pas_de_domination_sur_compromis(self):
        a = ConfigPoint("sensible", recall=0.95, fp_per_camera_per_day=5.0, median_delay_s=60)
        b = ConfigPoint("prudent", recall=0.80, fp_per_camera_per_day=0.5, median_delay_s=60)
        assert not a.dominates(b) and not b.dominates(a)

    def test_front(self):
        points = [
            ConfigPoint("A", 0.95, 5.0, 60),
            ConfigPoint("B", 0.80, 0.5, 60),
            ConfigPoint("C", 0.70, 6.0, 120),   # dominé par A
        ]
        front = pareto_front(points)
        noms = {p.name for p in front}
        assert noms == {"A", "B"}

    def test_selection_sous_budget(self):
        """La façon opérationnelle de choisir : fixer ce qu'on accepte de
        subir, puis maximiser ce qu'on veut obtenir."""
        points = [
            ConfigPoint("sensible", 0.95, 5.0, 60),
            ConfigPoint("equilibre", 0.85, 0.9, 60),
            ConfigPoint("prudent", 0.70, 0.2, 60),
        ]
        assert select_under_budget(points, fp_per_day_budget=1.0).name == "equilibre"

    def test_aucune_config_sous_budget(self):
        points = [ConfigPoint("A", 0.9, 10.0, 60)]
        assert select_under_budget(points, fp_per_day_budget=1.0) is None

    def test_contrainte_de_delai(self):
        points = [
            ConfigPoint("rapide", 0.80, 0.5, 60),
            ConfigPoint("lent", 0.95, 0.5, 3600),
        ]
        assert select_under_budget(points, 1.0, max_median_delay_s=120).name == "rapide"

    def test_front_vide(self):
        assert pareto_front([]) == []


# =========================================================================== #
# Intégrité : collision inter-classes
# =========================================================================== #
class TestCollisionInterClasses:
    """Une séquence classée à la fois comme feu et comme faux positif.

    Aucun contrôle de fuite entre splits ne l'attrape : chaque classe est
    parfaitement cohérente prise isolément.
    """

    def test_collision_detectee(self):
        collisions = find_class_collisions({
            "wildfire": {"seq_A", "seq_B"},
            "fp": {"seq_B", "seq_C"},
        })
        assert len(collisions) == 1
        assert collisions[0].identifier == "seq_B"
        assert sorted(collisions[0].classes) == ["fp", "wildfire"]

    def test_aucune_collision(self):
        assert find_class_collisions({"wildfire": {"a"}, "fp": {"b"}}) == []

    def test_echec_immediat_et_explicite(self):
        with pytest.raises(ValueError, match="collision"):
            assert_no_class_collision({"wildfire": {"x"}, "fp": {"x"}})

    def test_le_message_dit_la_consequence(self):
        with pytest.raises(ValueError) as exc:
            assert_no_class_collision({"wildfire": {"x"}, "fp": {"x"}})
        # Le message doit expliquer la CONSÉQUENCE, pas seulement constater le fait.
        assert "ce qu'il ne faut pas détecter" in str(exc.value)

    def test_pas_dechec_si_propre(self):
        assert_no_class_collision({"wildfire": {"a"}, "fp": {"b"}})

    def test_collisions_multiples_resumees(self):
        classes = {
            "wildfire": {f"s{i}" for i in range(10)},
            "fp": {f"s{i}" for i in range(10)},
        }
        with pytest.raises(ValueError, match="autre"):
            assert_no_class_collision(classes)

    def test_orthogonal_au_split(self):
        """Une collision peut exister sans aucune fuite entre splits : les deux
        contrôles ne se recouvrent pas."""
        # seq_B est en train côté wildfire et en test côté fp : aucun split
        # ne contient deux fois la même séquence.
        ledger = SplitLedger.from_splits({"train": {"seq_A"}, "test": {"seq_C"}})
        assert ledger.counts() == {"train": 1, "test": 1}
        assert find_class_collisions({"wildfire": {"seq_A", "seq_B"}, "fp": {"seq_B", "seq_C"}})


# =========================================================================== #
# Intégrité : registre de split
# =========================================================================== #
class TestRegistreDeSplit:
    def test_jeu_stable(self):
        a = SplitLedger.from_splits({"train": {"1", "2"}, "test": {"3"}})
        b = SplitLedger.from_splits({"train": {"1", "2"}, "test": {"3"}})
        drift = a.diff(b)
        assert drift.is_stable
        assert "comparables" in drift.summary()

    def test_sequence_deplacee_est_le_cas_grave(self):
        """Une séquence passée de train à test invalide la comparaison dans les
        deux sens."""
        a = SplitLedger.from_splits({"train": {"1"}, "test": {"2"}})
        b = SplitLedger.from_splits({"train": {}, "test": {"1", "2"}})
        drift = a.diff(b)
        assert not drift.is_stable
        assert drift.moved == [("1", "train", "test")]
        assert "plus grave" in drift.summary()

    def test_ajouts_et_retraits(self):
        a = SplitLedger.from_splits({"test": {"1", "2"}})
        b = SplitLedger.from_splits({"test": {"2", "3"}})
        drift = a.diff(b)
        assert drift.added == {"test": ["3"]}
        assert drift.removed == {"test": ["1"]}
        assert drift.n_changes == 2

    def test_fuite_entre_splits_refusee(self):
        with pytest.raises(ValueError, match="plusieurs splits"):
            SplitLedger.from_splits({"train": {"1"}, "test": {"1"}})

    def test_persistance(self, tmp_path):
        a = SplitLedger.from_splits({"train": {"1"}, "test": {"2"}})
        p = tmp_path / "ledger.json"
        a.save(p)
        assert SplitLedger.load(p).assignments == a.assignments

    def test_serialisation_du_drift(self):
        a = SplitLedger.from_splits({"train": {"1"}})
        b = SplitLedger.from_splits({"test": {"1"}})
        d = a.diff(b).as_dict()
        assert d["stable"] is False and d["moved"][0]["to"] == "test"


# =========================================================================== #
# Intégrité : reproductibilité
# =========================================================================== #
class TestReproductibilite:
    def test_empreinte_independante_de_lordre(self):
        """Sans normalisation, l'empreinte dépendrait de l'ordre de parcours du
        système de fichiers — l'une des sources de non-déterminisme visées."""
        assert manifest_hash({"a": "1", "b": "2"}) == manifest_hash({"b": "2", "a": "1"})

    def test_empreinte_sensible_au_contenu(self):
        assert manifest_hash({"a": "1"}) != manifest_hash({"a": "2"})

    def test_builds_identiques(self):
        m = {"f1": "aa", "f2": "bb"}
        c = compare_builds(m, dict(m))
        assert c.identical and "reproductible" in c.summary()

    def test_contenu_different_detecte(self):
        c = compare_builds({"f1": "aa"}, {"f1": "zz"})
        assert not c.identical and c.differing == ["f1"]
        assert "NON reproductible" in c.summary()

    def test_fichier_apparu_ou_disparu(self):
        c = compare_builds({"f1": "aa"}, {"f2": "bb"})
        assert c.only_in_first == ["f1"] and c.only_in_second == ["f2"]

    def test_manifeste_de_repertoire(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("world")
        m = file_manifest(tmp_path)
        assert set(m) == {"a.txt", "sub/b.txt"}
        assert compare_builds(m, file_manifest(tmp_path)).identical

    def test_repertoire_absent(self):
        with pytest.raises(ValueError, match="introuvable"):
            file_manifest("/chemin/absent/vraiment")

    def test_comparaison_serialisable(self):
        d = compare_builds({"f1": "aa"}, {"f1": "zz"}).as_dict()
        assert d["n_differing"] == 1


class TestRapportGlobal:
    def test_tout_va_bien(self):
        r = IntegrityReport()
        assert r.ok and "vérifiée" in r.summary()

    def test_collision_bloque(self):
        r = IntegrityReport(collisions=find_class_collisions({"a": {"x"}, "b": {"x"}}))
        assert not r.ok and "BLOQUANT" in r.summary()

    def test_build_non_reproductible_bloque(self):
        r = IntegrityReport(build=BuildComparison(identical=False, differing=["f"]))
        assert not r.ok

    def test_serialisation(self):
        assert IntegrityReport().as_dict()["ok"] is True
