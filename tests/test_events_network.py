"""Tests du schéma d'événement, de la connectivité et de la corrélation.

Ces trois briques décident de l'utilité opérationnelle du système autant que le
détecteur : une alerte perdue faute de réseau, dupliquée entre deux tours, ou
localisée avec une précision qu'elle n'a pas, sont trois façons de perdre la
confiance d'un centre de secours.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from openvigie.cli import main
from openvigie.correlation import Cluster, MultiTowerCorrelator, Tower
from openvigie.events import (
    ACKNOWLEDGED,
    CANDIDATE,
    CLOSED,
    CONFIRMED,
    OPERATOR_REJECTED,
    OPERATOR_VALIDATED,
    SCHEMA_VERSION,
    STATES,
    TRANSITIONS,
    DetectionEvent,
    InvalidTransition,
    bearing_uncertainty,
    can_transition,
    event_from_alert,
    new_event_id,
    triangulation_uncertainty,
    utc_now_iso,
)
from openvigie.transport import (
    FileTransport,
    HealthMonitor,
    MemoryTransport,
    Outbox,
    QueueEntry,
)


def make_event(**kw) -> DetectionEvent:
    base = {
        "event_id": new_event_id(),
        "site_id": "tour-01",
        "camera_id": "V03",
        "detected_at": "2026-08-12T14:31:07+00:00",
        "bearing_deg": 137.0,
        "distance_m": 5000.0,
        "fused_score": 0.82,
    }
    base.update(kw)
    return DetectionEvent(**base)


# --------------------------------------------------------------------------- #
class TestEventLifecycle:
    def test_etat_initial(self):
        assert make_event().state == CANDIDATE

    def test_transition_valide_journalise(self):
        e = make_event().transition(CONFIRMED, reason="persistance")
        assert e.state == CONFIRMED
        assert e.history[-1]["from"] == CANDIDATE
        assert e.history[-1]["reason"] == "persistance"

    def test_transition_invalide_refusee(self):
        with pytest.raises(InvalidTransition):
            make_event().transition(ACKNOWLEDGED)

    def test_etat_inconnu_refuse(self):
        with pytest.raises(InvalidTransition):
            make_event().transition("en_cours_de_reflexion")

    def test_etat_terminal_bloque(self):
        e = make_event().transition(CONFIRMED).record_operator_decision("fire").transition(CLOSED)
        assert e.is_terminal
        with pytest.raises(InvalidTransition):
            e.transition(CONFIRMED)

    def test_chaque_etat_a_une_regle(self):
        assert set(TRANSITIONS) == set(STATES)

    def test_parcours_nominal_complet(self):
        e = make_event()
        e.transition(CONFIRMED).mark_transmitted().mark_acknowledged()
        e.record_operator_decision("fire", "feu confirmé par le guet")
        assert e.state == OPERATOR_VALIDATED
        assert e.transmission_status == "acked"
        assert len(e.history) == 4

    def test_invalidation_produit_un_negatif(self):
        """Chaque motif de rejet doit être exploitable pour le réentraînement."""
        e = make_event().transition(CONFIRMED).record_operator_decision("prescribed_burn")
        assert e.state == OPERATOR_REJECTED
        assert e.operator_decision == "prescribed_burn"

    def test_decision_inconnue_refusee(self):
        with pytest.raises(ValueError, match="décision inconnue"):
            make_event().transition(CONFIRMED).record_operator_decision("peut-être")

    def test_is_actionable(self):
        assert not make_event().is_actionable
        assert make_event().transition(CONFIRMED).is_actionable

    def test_can_transition(self):
        assert can_transition(CANDIDATE, CONFIRMED)
        assert not can_transition(CANDIDATE, ACKNOWLEDGED)
        assert not can_transition(CLOSED, CONFIRMED)


class TestEventSerialization:
    def test_aller_retour_json(self):
        e = make_event().transition(CONFIRMED)
        assert DetectionEvent.from_json(e.to_json()).as_dict() == e.as_dict()

    def test_schema_majeur_incompatible_refuse(self):
        data = make_event().as_dict()
        data["schema_version"] = "2.0"
        with pytest.raises(ValueError, match="incompatible"):
            DetectionEvent.from_dict(data)

    def test_champ_inconnu_tolere_en_lecture(self):
        """Un site pas encore mis à jour ne doit pas tomber sur un champ ajouté
        par une version plus récente de la plateforme."""
        data = make_event().as_dict()
        data["champ_du_futur"] = 42
        assert DetectionEvent.from_dict(data).site_id == "tour-01"

    def test_geojson(self):
        f = make_event(latitude=44.01, longitude=3.02).as_geojson_feature()
        assert f["geometry"]["coordinates"] == [3.02, 44.01]
        assert f["properties"]["state"] == CANDIDATE

    def test_geojson_sans_position(self):
        assert make_event().as_geojson_feature()["geometry"] is None

    def test_summary_line_lisible(self):
        line = make_event(latitude=44.01, longitude=3.02).summary_line()
        assert "tour-01/V03" in line and "km" in line

    def test_utc_now_est_bien_en_utc(self):
        assert dt.datetime.fromisoformat(utc_now_iso()).tzinfo is not None

    def test_conversion_depuis_une_alerte(self):
        from openvigie.alerting import Alert

        alert = Alert(
            alert_id="a1", site_id="tour-01", view_id="V03",
            timestamp="2026-08-12T14:31:07+00:00", bearing_deg=137.0, score=0.9,
            distance_m=5000, latitude=44.02, longitude=3.03, localization="dem_intersect",
            features={"cnn_score": 0.7, "growth_score": 0.6, "bbox": [1, 2, 3, 4]},
            n_visits=4, model_version="m1", pipeline_tier="full",
        )
        e = event_from_alert(alert)
        assert e.bbox == [1, 2, 3, 4]
        assert "bbox" not in e.features
        assert e.uncertainty is not None
        assert e.tower_votes == ["tour-01"]


class TestUncertainty:
    def test_incertitude_croit_avec_la_distance(self):
        assert bearing_uncertainty(10_000).area_m2 > bearing_uncertainty(2_000).area_m2

    def test_erreur_transversale_suit_lazimut(self):
        u = bearing_uncertainty(10_000, bearing_sigma_deg=1.0)
        assert u.semi_minor_m == pytest.approx(10_000 * 3.14159 / 180, rel=0.01)

    def test_une_tour_seule_a_une_ellipse_tres_allongee(self):
        """L'erreur en distance domine largement : c'est exactement ce qu'une
        deuxième tour corrige."""
        u = bearing_uncertainty(8_000)
        assert u.semi_major_m > 8 * u.semi_minor_m

    def test_triangulation_bien_meilleure_quun_relevement(self):
        seule = bearing_uncertainty(8_000)
        deux = triangulation_uncertainty(8_000, 7_000, 80.0)
        assert deux.area_m2 < seule.area_m2 / 5

    def test_croisement_rasant_degrade_la_precision(self):
        franc = triangulation_uncertainty(8_000, 7_000, 85.0)
        rasant = triangulation_uncertainty(8_000, 7_000, 5.0)
        assert rasant.semi_major_m > 5 * franc.semi_major_m


# --------------------------------------------------------------------------- #
class TestOutbox:
    def test_mise_en_file_et_persistance(self, tmp_path):
        box = Outbox(tmp_path / "q")
        e = make_event()
        assert box.enqueue(e)
        assert len(box) == 1
        # relecture depuis le disque : une coupure ne doit rien perdre
        assert len(Outbox(tmp_path / "q")) == 1

    def test_idempotence(self, tmp_path):
        box = Outbox(tmp_path / "q")
        e = make_event()
        assert box.enqueue(e)
        assert not box.enqueue(e)
        assert len(box) == 1

    def test_emission_vide_la_file(self, tmp_path):
        box = Outbox(tmp_path / "q")
        box.enqueue(make_event())
        transport = MemoryTransport()
        assert box.flush(transport)["sent"] == 1
        assert len(box) == 0
        assert transport.sent[0].transmission_status == "sent"

    def test_echec_reporte_et_conserve(self, tmp_path):
        """Le cas qui compte : le réseau est tombé, l'alerte reste."""
        clock = [1000.0]
        box = Outbox(tmp_path / "q", base_backoff_s=10.0, clock=lambda: clock[0])
        box.enqueue(make_event())
        transport = MemoryTransport(fail_times=3)
        result = box.flush(transport)
        assert result == {"sent": 0, "retried": 1, "dead_lettered": 0, "remaining": 1}
        assert box.due() == []          # en attente du prochain essai
        clock[0] += 60
        assert len(box.due()) == 1

    def test_reprise_au_retour_du_reseau(self, tmp_path):
        clock = [1000.0]
        box = Outbox(tmp_path / "q", base_backoff_s=5.0, clock=lambda: clock[0])
        for _ in range(3):
            box.enqueue(make_event())
        transport = MemoryTransport(fail_times=3)
        box.flush(transport)
        clock[0] += 3600
        assert box.flush(transport)["sent"] == 3
        assert len(box) == 0

    def test_backoff_croissant_et_borne(self, tmp_path):
        box = Outbox(tmp_path / "q", base_backoff_s=10.0, max_backoff_s=100.0)
        assert box.backoff_s(1) == 10.0
        assert box.backoff_s(2) == 20.0
        assert box.backoff_s(3) == 40.0
        assert box.backoff_s(20) == 100.0

    def test_abandon_apres_trop_dechecs(self, tmp_path):
        clock = [0.0]
        box = Outbox(tmp_path / "q", max_attempts=3, base_backoff_s=1.0, clock=lambda: clock[0])
        box.enqueue(make_event())
        transport = MemoryTransport(fail_times=99)
        for _ in range(3):
            box.flush(transport)
            clock[0] += 10_000
        assert len(box) == 0
        assert len(box.dead_letters) == 1

    def test_saturation_sacrifie_les_plus_anciennes(self, tmp_path):
        """Une alerte d'il y a trois jours vaut moins qu'une alerte récente."""
        box = Outbox(tmp_path / "q", max_entries=5)
        for _ in range(9):
            box.enqueue(make_event())
        assert len(box) <= 5

    def test_entree_corrompue_ne_bloque_pas_la_file(self, tmp_path):
        box = Outbox(tmp_path / "q")
        box.enqueue(make_event())
        (tmp_path / "q" / "casse.json").write_text("{ ceci n'est pas du json", encoding="utf-8")
        assert len(box.pending()) == 1
        assert (tmp_path / "q" / "casse.corrupt").exists()

    def test_transport_qui_leve_ne_casse_rien(self, tmp_path):
        class Explosif(MemoryTransport):
            def send(self, event):
                raise RuntimeError("boum")

        box = Outbox(tmp_path / "q")
        box.enqueue(make_event())
        assert box.flush(Explosif())["retried"] == 1
        assert len(box) == 1

    def test_stats(self, tmp_path):
        box = Outbox(tmp_path / "q")
        box.enqueue(make_event())
        stats = box.stats()
        assert stats["pending"] == 1 and stats["due"] == 1

    def test_entree_serialisable(self, tmp_path):
        entry = QueueEntry(event=make_event(), attempts=2)
        assert QueueEntry.from_dict(entry.as_dict()).attempts == 2


class TestTransports:
    def test_file_transport_ecrit_du_jsonl(self, tmp_path):
        path = tmp_path / "events.jsonl"
        t = FileTransport(path)
        assert t.send(make_event())
        assert t.send(make_event())
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["site_id"] == "tour-01"

    def test_memory_transport_simule_une_panne(self):
        t = MemoryTransport(fail_times=2)
        assert not t.send(make_event())
        assert not t.send(make_event())
        assert t.send(make_event())


class TestHealth:
    def test_camera_vue_recemment_est_en_ligne(self):
        clock = [1000.0]
        hm = HealthMonitor("tour-01", offline_after_s=600, clock=lambda: clock[0])
        hm.record_frame("V00", background_ready=True)
        assert hm.camera_health()[0].online

    def test_camera_silencieuse_passe_hors_ligne(self):
        clock = [1000.0]
        hm = HealthMonitor("tour-01", offline_after_s=600, clock=lambda: clock[0])
        hm.record_frame("V00", background_ready=True)
        clock[0] += 1200
        assert not hm.camera_health()[0].online

    def test_statut_du_site(self):
        clock = [1000.0]
        hm = HealthMonitor("tour-01", clock=lambda: clock[0])
        assert hm.snapshot().status == "down"        # aucune caméra
        hm.record_frame("V00", background_ready=True)
        assert hm.snapshot().status == "ok"
        hm.record_frame("V01", image_status="fail", background_ready=True)
        assert hm.snapshot().status == "degraded"

    def test_optique_degradee_signalee(self):
        hm = HealthMonitor("tour-01")
        hm.record_frame("V00", optics_status="fail", background_ready=True)
        assert hm.camera_health()[0].degraded

    def test_fond_immature_est_une_degradation(self):
        """Un site qui n'a pas encore de référence ne protège rien : le dire."""
        hm = HealthMonitor("tour-01")
        hm.record_frame("V00", background_ready=False)
        assert hm.snapshot().status == "degraded"

    def test_battement_respecte_lintervalle(self):
        clock = [1000.0]
        hm = HealthMonitor("tour-01", heartbeat_interval_s=300, clock=lambda: clock[0])
        hm.record_frame("V00", background_ready=True)
        transport = MemoryTransport()
        assert hm.beat(transport) is not None
        assert hm.beat(transport) is None
        clock[0] += 400
        assert hm.beat(transport) is not None
        assert len(transport.health) == 2

    def test_images_par_heure_glissantes(self):
        clock = [1000.0]
        hm = HealthMonitor("tour-01", clock=lambda: clock[0])
        for _ in range(5):
            hm.record_frame("V00")
            clock[0] += 60
        clock[0] += 7200
        hm.record_frame("V00")
        assert hm.camera_health()[0].frames_last_hour == 1


# --------------------------------------------------------------------------- #
class TestCorrelation:
    def _network(self) -> dict[str, Tower]:
        return {
            "A": Tower("A", 44.000, 3.000, max_range_m=12_000, has_ptz=True),
            "B": Tower("B", 44.000, 3.100, max_range_m=12_000, has_ptz=True),
            "C": Tower("C", 44.300, 3.500, max_range_m=8_000),
        }

    def test_tower_distance_et_azimut(self):
        t = Tower("A", 44.0, 3.0)
        assert t.distance_to(44.0, 3.0) == pytest.approx(0.0)
        assert t.bearing_to(44.1, 3.0) == pytest.approx(0.0, abs=1)
        assert t.bearing_to(44.0, 3.1) == pytest.approx(90.0, abs=1)

    def test_portee_par_secteur_depuis_le_viewshed(self):
        t = Tower("A", 44.0, 3.0, max_range_m=12_000, sector_ranges={0.0: 2_000, 90.0: 12_000})
        assert t.range_in_direction(5.0) == 2_000
        assert t.range_in_direction(88.0) == 12_000

    def test_meme_tour_meme_azimut_dedupliquee(self):
        c = MultiTowerCorrelator(self._network())
        events = [
            make_event(site_id="A", bearing_deg=90.0, detected_at="2026-08-12T14:00:00+00:00"),
            make_event(site_id="A", bearing_deg=91.5, detected_at="2026-08-12T14:02:00+00:00"),
        ]
        clusters = c.cluster(events)
        assert len(clusters) == 1

    def test_azimuts_eloignes_restent_separes(self):
        c = MultiTowerCorrelator(self._network())
        events = [
            make_event(site_id="A", bearing_deg=90.0, detected_at="2026-08-12T14:00:00+00:00"),
            make_event(site_id="A", bearing_deg=200.0, detected_at="2026-08-12T14:02:00+00:00"),
        ]
        assert len(c.cluster(events)) == 2

    def test_hors_fenetre_temporelle_reste_separe(self):
        c = MultiTowerCorrelator(self._network(), time_window_s=300)
        events = [
            make_event(site_id="A", bearing_deg=90.0, detected_at="2026-08-12T14:00:00+00:00"),
            make_event(site_id="A", bearing_deg=90.0, detected_at="2026-08-12T16:00:00+00:00"),
        ]
        assert len(c.cluster(events)) == 2

    def test_deux_tours_sur_le_meme_feu_sont_triangulees(self):
        """Le cœur du gain multi-tours : un seul événement, bien localisé."""
        towers = self._network()
        c = MultiTowerCorrelator(towers)
        # Feu à mi-chemin, au nord des deux tours.
        target = (44.05, 3.05)
        ba = towers["A"].bearing_to(*target)
        bb = towers["B"].bearing_to(*target)
        events = [
            make_event(site_id="A", bearing_deg=ba, distance_m=towers["A"].distance_to(*target),
                       detected_at="2026-08-12T14:00:00+00:00"),
            make_event(site_id="B", bearing_deg=bb, distance_m=towers["B"].distance_to(*target),
                       detected_at="2026-08-12T14:01:00+00:00"),
        ]
        clusters = c.cluster(events)
        assert len(clusters) == 1
        cl = clusters[0]
        assert cl.method == "triangulated"
        assert cl.n_towers == 2
        assert cl.latitude == pytest.approx(target[0], abs=1e-3)
        assert cl.longitude == pytest.approx(target[1], abs=1e-3)

    def test_triangulation_reduit_lellipse(self):
        towers = self._network()
        c = MultiTowerCorrelator(towers)
        target = (44.05, 3.05)
        events = [
            make_event(site_id=s, bearing_deg=towers[s].bearing_to(*target),
                       distance_m=towers[s].distance_to(*target),
                       detected_at="2026-08-12T14:00:00+00:00")
            for s in ("A", "B")
        ]
        deux = c.cluster(events)[0]
        seule = c.cluster(events[:1])[0]
        assert deux.uncertainty.area_m2 < seule.uncertainty.area_m2

    def test_deux_tours_augmentent_la_confiance(self):
        towers = self._network()
        c = MultiTowerCorrelator(towers)
        target = (44.05, 3.05)
        events = [
            make_event(site_id=s, bearing_deg=towers[s].bearing_to(*target),
                       distance_m=towers[s].distance_to(*target), fused_score=0.7,
                       detected_at="2026-08-12T14:00:00+00:00")
            for s in ("A", "B")
        ]
        assert c.cluster(events)[0].confidence > c.cluster(events[:1])[0].confidence

    def test_tour_unique_reste_en_relevement(self):
        c = MultiTowerCorrelator(self._network())
        cl = c.cluster([make_event(site_id="A", bearing_deg=45.0, distance_m=6000)])[0]
        assert cl.method != "triangulated"
        assert cl.n_towers == 1

    def test_sollicitation_des_tours_qui_voient_la_zone(self):
        towers = self._network()
        c = MultiTowerCorrelator(towers)
        target = (44.03, 3.05)
        e = make_event(site_id="A", bearing_deg=towers["A"].bearing_to(*target),
                       distance_m=towers["A"].distance_to(*target), fused_score=0.8)
        tasks = c.confirmation_tasks(e)
        assert [t.site_id for t in tasks] == ["B"]      # C est trop loin
        assert 0 < tasks[0].bearing_deg < 360
        assert tasks[0].source_event_id == e.event_id

    def test_pas_de_sollicitation_sur_candidat_faible(self):
        c = MultiTowerCorrelator(self._network())
        e = make_event(site_id="A", bearing_deg=45.0, distance_m=5000, fused_score=0.2)
        assert c.confirmation_tasks(e, min_score=0.5) == []

    def test_relief_bloquant_empeche_la_sollicitation(self):
        towers = self._network()
        towers["B"].sector_ranges = dict.fromkeys(range(0, 360, 45), 500.0)
        c = MultiTowerCorrelator(towers)
        target = (44.03, 3.05)
        e = make_event(site_id="A", bearing_deg=towers["A"].bearing_to(*target),
                       distance_m=towers["A"].distance_to(*target), fused_score=0.9)
        assert c.confirmation_tasks(e) == []

    def test_promotion_fusionne_en_un_evenement(self):
        towers = self._network()
        c = MultiTowerCorrelator(towers)
        target = (44.05, 3.05)
        events = [
            make_event(site_id=s, bearing_deg=towers[s].bearing_to(*target),
                       distance_m=towers[s].distance_to(*target),
                       detected_at="2026-08-12T14:00:00+00:00")
            for s in ("A", "B")
        ]
        cl = c.cluster(events)[0]
        promoted = c.promote(cl)
        assert promoted.state == CONFIRMED
        assert promoted.tower_votes == ["A", "B"]
        assert promoted.localization_method == "triangulated"
        assert promoted.uncertainty is not None

    def test_promotion_dun_groupe_vide(self):
        c = MultiTowerCorrelator(self._network())
        assert c.promote(Cluster(cluster_id="x")) is None

    def test_tour_inconnue_ne_plante_pas(self):
        c = MultiTowerCorrelator(self._network())
        assert len(c.cluster([make_event(site_id="INCONNUE", bearing_deg=10.0)])) == 1


# --------------------------------------------------------------------------- #
class TestCliConnectivity:
    def test_schema(self, capsys):
        assert main(["schema"]) == 0
        out = capsys.readouterr().out
        assert SCHEMA_VERSION in out and "operator_validated" in out

    def test_schema_json(self, capsys):
        assert main(["schema", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["schema_version"] == SCHEMA_VERSION
        assert "example" in data

    def test_outbox_vide(self, tmp_path, capsys):
        assert main(["outbox", "--dir", str(tmp_path / "q")]) == 0
        assert "en attente     : 0" in capsys.readouterr().out

    def test_outbox_flush(self, tmp_path, capsys):
        box = Outbox(tmp_path / "q")
        box.enqueue(make_event())
        target = tmp_path / "events.jsonl"
        assert main(["outbox", "--dir", str(tmp_path / "q"), "--flush", "--to", str(target)]) == 0
        assert json.loads(capsys.readouterr().out)["sent"] == 1
        assert target.exists()

    def test_viewshed_synthetique(self, capsys):
        assert main(["viewshed", "-t", "full", "--synthetic", "--sectors", "8", "--step", "100"]) == 0
        out = capsys.readouterr().out
        assert "km" in out and "Médiane" in out
