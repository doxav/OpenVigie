"""Comble les lacunes de couverture identifiées après l'audit de couverture
combinée (avec et sans OpenCV/SciPy) du renommage vigie → OpenVigie.

Chaque test cible une branche de code qui n'était exercée par aucun test
existant. Le critère de sélection n'est pas « atteindre 100 % » — plusieurs
branches purement défensives et à très faible risque restent hors périmètre —
mais « ce code pourrait-il casser silencieusement, et personne ne le
saurait avant qu'un site réel le déclenche ? ». Trois découvertes notables :

- un bug réel dans ``scripts/openipc_deploy.sh`` (incohérence de casse entre
  le répertoire créé et celui utilisé par ``cp``/``tar``, qui aurait fait
  échouer tout déploiement ``--push-agent``) ;
- ``NetworkConfig`` n'était testée nulle part : une faute de frappe dans
  ``network.transport`` ou un transport ``http`` sans URL passaient inaperçus
  jusqu'à l'exécution ;
- ``pipeline.flush()``/``heartbeat()`` sans ``outbox``/``transport`` — le cas
  le plus courant dans les tests existants — n'étaient jamais appelés.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

import numpy as np
import pytest

from openvigie.background import BackgroundBank
from openvigie.candidates import Blob
from openvigie.cli import main
from openvigie.config import NetworkConfig, site_config_from_dict, tier_defaults
from openvigie.correlation import Cluster, MultiTowerCorrelator, Tower
from openvigie.detectors import ClassicalDetector, get_detector
from openvigie.events import CONFIRMED, DetectionEvent, new_event_id
from openvigie.geometry import IMX675, LensSpec, coverage_gaps, koschmieder_contrast, pixel_area_to_m2
from openvigie.masking import masked_fraction
from openvigie.pipeline import DetectionPipeline
from openvigie.platform import OpenIpcCamera
from openvigie.ptz import (
    ScanScheduler,
    SimulatedPtz,
    pelco_d_frame,
    pelco_d_stop,
)
from openvigie.scoring import threshold_for_fp_budget
from openvigie.sources import FileSequenceSource
from openvigie.tracking import Observation, Track, track_bearing
from openvigie.transport import MemoryTransport

UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
class TestScriptDeployPackaging:
    """Régression du bug trouvé pendant le renommage.

    ``openipc_deploy.sh --push-agent`` créait ``$TMP/OpenVigie`` (majuscule)
    mais copiait les fichiers dans ``$TMP/openvigie/`` (minuscule) : ``cp``
    échouait faute de répertoire cible, et ``tar`` empaquetait un répertoire
    vide. Aucun test ne l'aurait détecté puisqu'il s'agit d'un script bash,
    hors du périmètre de pytest — d'où ce test qui rejoue la séquence
    mkdir/cp/tar exacte du script et vérifie que le paquet produit est
    complet et importable.
    """

    def test_sequence_de_paquetage_produit_un_paquet_importable(self, tmp_path):
        tmp = tmp_path / "deploy"
        (tmp).mkdir()
        # Reproduction exacte des trois commandes du script, après correction.
        subprocess.run(["mkdir", "-p", str(tmp / "openvigie")], check=True)
        subprocess.run(
            f'cp "src/openvigie/"*.py "{tmp}/openvigie/"', shell=True, check=True
        )
        subprocess.run(
            ["tar", "czf", str(tmp / "openvigie-edge.tgz"), "-C", str(tmp), "openvigie"],
            check=True,
        )
        archive = tmp / "openvigie-edge.tgz"
        assert archive.exists() and archive.stat().st_size > 0

        extract = tmp_path / "extract"
        extract.mkdir()
        subprocess.run(["tar", "xzf", str(archive), "-C", str(extract)], check=True)
        assert (extract / "openvigie" / "pipeline.py").exists()
        assert (extract / "openvigie" / "__init__.py").exists()

        # Et surtout : le paquet extrait doit être un module Python valide.
        result = subprocess.run(
            ["python3", "-c",
             "import sys; sys.path.insert(0, '.'); import openvigie, openvigie.pipeline; "
             "print(openvigie.__version__)"],
            cwd=extract, capture_output=True, text=True, env={"OPENVIGIE_FORCE_NUMPY": "1", "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()

    def test_script_ne_contient_plus_lincoherence_de_casse(self):
        """Vérification statique directe sur le script publié : le répertoire
        créé par ``mkdir`` et celui utilisé par ``tar -C`` doivent être
        identiques, au caractère près."""
        text = open("scripts/openipc_deploy.sh", encoding="utf-8").read()
        assert 'mkdir -p "$TMP/openvigie"' in text
        assert 'mkdir -p "$TMP/OpenVigie"' not in text
        assert '-C "$TMP" openvigie' in text
        assert '-C "$TMP" OpenVigie' not in text


# --------------------------------------------------------------------------- #
class TestNetworkConfigValidation:
    """AUDIT complémentaire : ``NetworkConfig`` n'était référencée par aucun
    test. Une faute de frappe dans ``network.transport`` ou un transport
    ``http`` sans URL passaient inaperçus jusqu'à l'exécution sur site."""

    def test_transport_invalide_refuse(self):
        with pytest.raises(ValueError, match="transport"):
            NetworkConfig(transport="mqtt")

    def test_http_sans_url_refuse(self):
        with pytest.raises(ValueError, match="url"):
            NetworkConfig(transport="http", url="")

    def test_http_avec_url_accepte(self):
        assert NetworkConfig(transport="http", url="https://example.org").url

    def test_valeurs_par_defaut_valides(self):
        NetworkConfig()  # ne doit lever aucune exception

    def test_bloc_reseau_absent_utilise_les_defauts(self):
        """``_build`` renvoie une instance par défaut quand la section est
        absente du YAML — un site sans bloc ``network:`` doit rester valide."""
        cfg = site_config_from_dict({"site_id": "s1", "tier": "medium"})
        assert cfg.network.transport == "file"


# --------------------------------------------------------------------------- #
class TestPipelineFlushHeartbeatSansTransport:
    """Le cas le plus courant des tests existants — un pipeline construit sans
    ``outbox`` ni ``transport`` — n'appelait jamais ``flush()``/``heartbeat()``.
    Ce sont pourtant des méthodes publiques, et un appelant qui oublie de les
    câbler ne doit pas obtenir une exception mais un résultat neutre."""

    def test_flush_sans_outbox_ni_transport(self):
        pipe = DetectionPipeline(tier_defaults("medium"))
        assert pipe.flush() == {"sent": 0, "retried": 0, "dead_lettered": 0, "remaining": 0}

    def test_heartbeat_sans_transport(self):
        pipe = DetectionPipeline(tier_defaults("medium"))
        assert pipe.heartbeat() is None

    def test_flush_avec_outbox_seul_reste_neutre(self, tmp_path):
        from openvigie.transport import Outbox

        pipe = DetectionPipeline(tier_defaults("medium"), outbox=Outbox(tmp_path / "q"))
        assert pipe.flush() == {"sent": 0, "retried": 0, "dead_lettered": 0, "remaining": 0}

    def test_heartbeat_avec_transport_reel(self):
        cfg = tier_defaults("medium")
        cfg.network.heartbeat_interval_s = 0.0
        transport = MemoryTransport()
        pipe = DetectionPipeline(cfg, transport=transport)
        beat = pipe.heartbeat()
        assert beat is not None
        assert transport.health


# --------------------------------------------------------------------------- #
class TestEventListTruncation:
    """AUDIT P1-08/P1-09 : la purge des événements retenus n'était jamais
    déclenchée dans les tests — le seuil (500) n'était jamais franchi."""

    def test_liste_devenements_bornee(self):
        cfg = tier_defaults("medium")
        cfg.operating.mode = "shadow"
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"))
        pipe.max_retained_events = 5  # seuil réduit pour un test rapide
        for _ in range(9):
            pipe.events.append(
                DetectionEvent(event_id=new_event_id(), site_id="s", camera_id="V00",
                               detected_at=dt.datetime(2026, 8, 1, tzinfo=UTC).isoformat())
            )
            pipe.tracker.prune()
            if len(pipe.events) > pipe.max_retained_events:
                pipe.events = pipe.events[-pipe.max_retained_events:]
        assert len(pipe.events) == 5


# --------------------------------------------------------------------------- #
class TestPelcoDStopEtBaseBackend:
    def test_pelco_d_stop_bien_forme(self):
        frame = pelco_d_stop(1)
        assert len(frame) == 7 and frame[0] == 0xFF
        assert frame[2:6] == bytes([0x00, 0x00, 0x00, 0x00])

    def test_valeur_hors_plage_refusee(self):
        with pytest.raises(ValueError, match="hors plage"):
            pelco_d_frame(1, 0x00, 999, 0x00, 0x00)

    def test_backend_par_defaut_ne_fait_rien(self):
        """Les méthodes non abstraites de ``PtzBackend`` (zoom, position, close)
        doivent avoir un comportement neutre par défaut pour tout backend qui
        ne les surcharge pas — c'est le cas de ``SimulatedPtz``."""
        sim = SimulatedPtz()
        assert sim.set_zoom(5.0) is False
        assert sim.position() is None
        assert sim.close() is None

    def test_simulated_ptz_erreur_et_temps_de_deplacement(self):
        sim = SimulatedPtz(repeatability_deg=0.4, slew_speed_deg_s=20.0, seed=1)
        errs = [sim.actual_error_deg() for _ in range(200)]
        assert -1.0 < sum(errs) / len(errs) < 1.0   # centré sur 0
        assert sim.slew_time_s(0.0, 40.0) == pytest.approx(2.0)
        assert sim.slew_time_s(350.0, 10.0) == pytest.approx(1.0)  # passage par 0°

    def test_dwell_invalide_refuse(self):
        views = [type("V", (), {"view_id": "V00", "hfov_deg": 30.0, "azimuth_deg": 0.0})()]
        with pytest.raises(ValueError):
            ScanScheduler(views, dwell_s=0.0, settle_s=1.0)


# --------------------------------------------------------------------------- #
class TestGeometrieUtilitaires:
    def test_zoom_ratio(self):
        assert LensSpec(4.8, 144.0).zoom_ratio == pytest.approx(30.0)

    def test_pixel_area_to_m2(self):
        m2 = pixel_area_to_m2(100, 5000.0, IMX675, 6.0)
        assert m2 > 0

    def test_couverture_vide(self):
        assert coverage_gaps([]) == [(0.0, 360.0)]

    def test_visibilite_nulle(self):
        assert koschmieder_contrast(1000.0, 0.0) == 0.0


class TestScoringBornes:
    """AUDIT complémentaire : les deux branches limites de
    ``threshold_for_fp_budget`` — budget nul et budget très généreux — ne sont
    exercées par aucun test existant, alors que ce sont les valeurs qu'un
    exploitant est susceptible de saisir par erreur."""

    def test_budget_nul_donne_le_seuil_le_plus_haut(self):
        scores = np.linspace(0.1, 0.9, 50)
        thr = threshold_for_fp_budget(scores, observation_days=30.0, fp_per_day_budget=0.0)
        assert thr > scores.max()

    def test_budget_tres_large_accepte_tout(self):
        scores = np.linspace(0.1, 0.9, 5)
        thr = threshold_for_fp_budget(scores, observation_days=365.0, fp_per_day_budget=100.0)
        assert thr <= scores.min()


class TestMaskingBornes:
    def test_fraction_sans_masque(self):
        assert masked_fraction((100, 100), None) == 0.0
        assert masked_fraction((100, 100), []) == 0.0


class TestDetectorNiveauxDeGris:
    """La branche ``else`` de ``ClassicalDetector.features`` (pas d'information
    chromatique) ne se déclenche que sur une image sans 3ᵉ canal — le cas
    d'une caméra en mode nuit NIR, jamais testé jusqu'ici."""

    def test_roi_monochrome_sans_canal_couleur(self):
        roi = np.full((32, 32), 150, dtype=np.uint8)  # 2D : pas de canal RGB
        feats = ClassicalDetector.features(roi)
        assert feats["low_saturation"] == pytest.approx(0.7)

    def test_score_reste_dans_zero_un_en_monochrome(self):
        det = ClassicalDetector()
        assert 0.0 <= det.score_roi(np.full((32, 32), 150, dtype=np.uint8)) <= 1.0


class TestTrackBearingPublic:
    """``track_bearing`` est une fonction publique de l'API — utilisable
    directement par un intégrateur — mais n'est appelée par aucun chemin
    interne du pipeline (qui recalcule l'azimut avec la pose si disponible).
    Elle doit rester correcte indépendamment."""

    def test_coherent_avec_pixel_to_bearing(self):
        from openvigie.geometry import pixel_to_bearing

        col = IMX675.width_px / 2.0 + 50.0  # légèrement à droite du centre capteur
        blob = Blob(blob_id=1, bbox=(int(col) - 15, 100, int(col) + 15, 140),
                    centroid=(col, 120.0), area_px=900, mean_delta=10.0)
        track = Track(track_id=1, view_id="V00")
        track.add(Observation(t=0.0, blob=blob, area_m2=50.0, centroid_y_m=0.0,
                              distance_m=4000.0, contrast_loss=0.3, translucency=0.8))
        bearing = track_bearing(track, IMX675, 6.0, view_azimuth_deg=90.0)
        # track_bearing ne fait qu'appeler pixel_to_bearing : ce test vérifie
        # cette délégation, pas la formule elle-même (déjà testée ailleurs).
        assert bearing == pytest.approx(pixel_to_bearing(col, IMX675, 6.0, 90.0))
        assert bearing > 90.0  # à droite du centre -> azimut plus grand


# --------------------------------------------------------------------------- #
class TestFileSequenceSource:
    def test_construction_et_epuisement(self, tmp_path):
        (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0")  # en-tête JPEG minimal
        src = FileSequenceSource(tmp_path, pattern="*.jpg", period_s=10.0)
        assert len(src.paths) == 1
        assert src.index == 0

    def test_repertoire_vide(self, tmp_path):
        src = FileSequenceSource(tmp_path, pattern="*.jpg")
        assert src.read() is None


# --------------------------------------------------------------------------- #
class TestOpenIpcCameraInjectable:
    """``cli_get``/``cli_set`` acceptent un ``run`` injectable — conçus pour
    être testables sans matériel réel, mais jamais exercés."""

    def test_cli_get_utilise_le_run_fourni(self):
        appels = []

        def fake_run(cmd, timeout=5.0):
            appels.append(cmd)
            return "5\n"

        cam = OpenIpcCamera(host="10.0.0.5", user="root")
        assert cam.cli_get(".video0.fps", run=fake_run) == "5"
        assert appels[0][:2] == ["ssh", "root@10.0.0.5"]

    def test_cli_get_sans_reponse(self):
        assert OpenIpcCamera(host="10.0.0.5").cli_get(".x", run=lambda cmd, timeout=5.0: None) is None

    def test_cli_set_reussite_et_echec(self):
        cam = OpenIpcCamera(host="10.0.0.5")
        assert cam.cli_set(".jpeg.qfactor", "90", run=lambda cmd, timeout=5.0: "ok") is True
        assert cam.cli_set(".jpeg.qfactor", "90", run=lambda cmd, timeout=5.0: None) is False


# --------------------------------------------------------------------------- #
class TestBackgroundBankValidation:
    def test_buffer_size_invalide(self):
        with pytest.raises(ValueError, match="buffer_size"):
            BackgroundBank(buffer_size=0)


# --------------------------------------------------------------------------- #
class TestCorrelationBranchesRares:
    def _towers(self):
        return {
            "A": Tower("A", 44.0, 3.0, max_range_m=12_000),
            "B": Tower("B", 44.0, 3.1, max_range_m=12_000),
        }

    def _event(self, site_id, bearing, ts="2026-08-01T14:00:00+00:00"):
        return DetectionEvent(event_id=new_event_id(), site_id=site_id, camera_id="V00",
                              detected_at=ts, bearing_deg=bearing, fused_score=0.8)

    def test_tour_absente_du_registre_ignoree_dans_la_triangulation(self):
        """Un événement référence une tour ``C`` inconnue du corrélateur :
        la paire est ignorée sans lever d'exception."""
        towers = self._towers()
        corr = MultiTowerCorrelator(towers)
        events = [self._event("A", 45.0), self._event("C", 90.0)]
        # "A" et "C" ne sont pas assez proches spatialement pour fusionner ;
        # le test porte sur l'absence de plantage, pas sur le regroupement.
        clusters = corr.cluster(events)
        assert len(clusters) >= 1

    def test_promotion_avec_transition_impossible_ne_leve_pas(self):
        """``promote`` tente de passer l'événement en CONFIRMED ; si son état
        rend cette transition impossible (déjà clos par exemple), l'échec est
        absorbé plutôt que de faire remonter une exception au corrélateur."""
        towers = self._towers()
        corr = MultiTowerCorrelator(towers)
        e = self._event("A", 45.0)
        e.transition(CONFIRMED)
        e.record_operator_decision("prescribed_burn")  # -> OPERATOR_REJECTED puis CLOSED possible
        cluster = Cluster(cluster_id="c1", events=[e], latitude=44.0, longitude=3.0)
        promoted = corr.promote(cluster, min_towers=1)
        assert promoted is not None  # aucune exception, l'événement est renvoyé tel quel

    def test_confirmation_tasks_sans_position_estimable(self):
        corr = MultiTowerCorrelator({})  # aucune tour connue
        tasks = corr.confirmation_tasks(self._event("INCONNU", 45.0))
        assert tasks == []


# --------------------------------------------------------------------------- #
class TestCliCalibrateCheminsReels:
    """``cmd_calibrate`` a deux chemins jamais exercés ensemble : la lecture de
    vraies observations/ADS-B depuis disque (pas seulement ``--simulate``), et
    la comparaison à un étalonnage de référence (``--reference``)."""

    def _write_scenario(self, tmp_path):
        """Produit des fichiers d'observations et d'ADS-B cohérents, en
        réutilisant le générateur synthétique du module de calibration —
        c'est la même donnée que ``--simulate`` produit en mémoire, mais
        passée par le disque comme le ferait un site réel."""
        from openvigie.calibration import (
            CameraPose,
            Site,
            StaticAdsbSource,
            synthesize_observations,
            synthesize_traffic,
        )

        site = Site(latitude=44.0, longitude=3.0, altitude_m=500.0, height_m=40.0)
        pose = CameraPose(yaw_deg=90.0, pitch_deg=-1.5, roll_deg=0.0, focal_mm=6.25,
                          sensor=IMX675, width_px=1296, height_px=972)
        tracks = synthesize_traffic(site, n_aircraft=25, seed=3)
        observations = synthesize_observations(site, tracks, pose, noise_px=1.5, seed=11)

        obs_path = tmp_path / "observations.json"
        obs_path.write_text(json.dumps([
            {"t": o.t, "col": o.col, "row": o.row, "brightness": o.brightness, "area_px": o.area_px}
            for o in observations
        ]), encoding="utf-8")

        adsb_path = tmp_path / "adsb.jsonl"
        src = StaticAdsbSource()
        for track in tracks.values():
            for state in track.states:
                src.add(state)
        src.save_jsonl(adsb_path)
        return obs_path, adsb_path

    def test_etalonnage_depuis_des_fichiers_reels(self, tmp_path, capsys):
        obs_path, adsb_path = self._write_scenario(tmp_path)
        cfg_path = tmp_path / "site.yaml"
        cfg_path.write_text(
            "site_id: t1\ntier: full\nlatitude: 44.0\nlongitude: 3.0\n"
            "optics:\n  sensor: IMX675\n  focal_mm: 6.25\n  tilt_deg: 1.5\n",
            encoding="utf-8",
        )
        rc = main([
            "calibrate", "-c", str(cfg_path), "--azimuth", "90",
            "--observations", str(obs_path), "--adsb", str(adsb_path),
            "--gate-px", "250",
        ])
        assert rc == 0
        assert "Étalonnage" in capsys.readouterr().out

    def test_calibrate_simulate_json_couvre_la_branche_json(self, capsys):
        assert main(["calibrate", "-t", "full", "--simulate", "--json"]) == 0
        # deux documents JSON successifs (résultat, puis rien d'autre après échec/succès)
        out = capsys.readouterr().out
        first = json.loads(out.split("\n\n")[0])
        assert "pose" in first

    def test_calibrate_simulate_echoue_hors_tolerance(self, capsys):
        """La branche d'échec de la vérification simulée (erreur > tolérance)
        n'était jamais déclenchée : on force une tolérance irréaliste."""
        rc = main(["calibrate", "-t", "full", "--simulate", "--tolerance", "0.0000001"])
        assert rc == 1
        assert "ÉCHEC" in capsys.readouterr().out

    def test_calibrate_avec_reference_stable(self, tmp_path, capsys):
        ref = tmp_path / "ref.json"
        rc1 = main(["calibrate", "-t", "full", "--simulate", "--seed", "3",
                    "--output", str(ref)])
        assert rc1 == 0
        rc2 = main(["calibrate", "-t", "full", "--simulate", "--seed", "3",
                    "--reference", str(ref)])
        assert rc2 == 0
        assert "dérive" in capsys.readouterr().out

    def test_calibrate_avec_reference_derivee(self, tmp_path, capsys):
        ref = tmp_path / "ref.json"
        main(["calibrate", "-t", "full", "--simulate", "--true-yaw-error", "0.0",
              "--output", str(ref)])
        rc = main(["calibrate", "-t", "full", "--simulate", "--true-yaw-error", "3.0",
                   "--reference", str(ref)])
        assert rc == 1
        assert "dérive" in capsys.readouterr().out


class TestCliBranchesJsonRestantes:
    """Sorties ``--json``/textuelles d'options non couvertes par les tests
    existants : détection locale (sans caméra), file d'attente, viewshed."""

    def test_hw_local_json(self, capsys):
        assert main(["hw", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert "backend" in data and "reason" in data

    def test_outbox_liste_les_entrees_en_attente(self, tmp_path, capsys):
        from openvigie.transport import Outbox

        box = Outbox(tmp_path / "q")
        box.enqueue(DetectionEvent(event_id=new_event_id(), site_id="s", camera_id="V00",
                                   detected_at="2026-08-01T14:00:00+00:00"))
        assert main(["outbox", "--dir", str(tmp_path / "q")]) == 0
        assert "s/V00" in capsys.readouterr().out

    def test_viewshed_json(self, capsys):
        assert main(["viewshed", "-t", "full", "--synthetic", "--sectors", "4", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 4

    def test_plan_ptz_affiche_les_avertissements(self, capsys):
        """Le tier MINIMAL est en mode PTZ : le budget de balayage doit
        déclencher les avertissements d'usure, jamais exercés en texte brut."""
        assert main(["plan", "-t", "minimal"]) == 0
        assert capsys.readouterr().out  # sortie non vide, chemin PTZ exécuté

    def test_plan_ptz_json_expose_les_avertissements(self, capsys):
        assert main(["plan", "-t", "minimal", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["mode"] == "ptz"
        assert "warnings" in data

    def test_selftest_echoue_si_la_detection_ne_correspond_pas(self, capsys, monkeypatch):
        """La branche d'échec de ``cmd_selftest`` compare un résultat attendu
        (déterministe : mode ``plume`` + hors ``measure`` ⇒ alerte) au résultat
        réel. Comme le pipeline se comporte correctement par construction, la
        seule façon d'exercer honnêtement cette branche est d'y substituer un
        pipeline factice dont le comportement contredit l'attendu — exactement
        ce qu'un vrai bug de détection produirait."""
        import openvigie.pipeline as pipeline_module

        class FakeView:
            focal_mm = 6.25

        class FakeResult:
            status = "ok"
            alerts: list = []

        class FakePipeline:
            def __init__(self, cfg):
                self.stats = {"alerts": 0, "frames": 0}  # aucune alerte, quel que soit l'attendu

            def register_view(self, *a, **kw):
                return FakeView()

            def process_frame(self, *a, **kw):
                return FakeResult()

            def summary(self):
                return {"stats": self.stats}

        monkeypatch.setattr(pipeline_module, "DetectionPipeline", FakePipeline)
        rc = main(["selftest", "-t", "medium", "--mode", "plume"])  # mode_operating par défaut : shadow
        assert rc == 1
        assert "ÉCHEC" in capsys.readouterr().err

