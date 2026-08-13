"""Second lot de tests de couverture, après la première passe de
``test_rename_and_coverage.py``. Couverture combinée passée de 94 % à 96 % ;
ce fichier vise les gains restants les plus significatifs :

- les fabriques ``build_transport``/``build_outbox`` n'étaient exercées par
  aucun test — pourtant c'est le point d'entrée réel pour tout site qui lit
  sa configuration réseau depuis un fichier YAML ;
- le chemin combiné pose étalonnée + triangulation multi-tours n'était jamais
  atteint en une seule fois (chaque brique était testée isolément) ;
- plusieurs seuils « warn » des contrôles matériel n'étaient vérifiés que par
  une assertion large (``status in ("warn", "fail")``), ce qui aurait laissé
  passer une inversion de seuil.
"""

from __future__ import annotations

import datetime as dt
import socket

import numpy as np
import pytest

from openvigie.config import tier_defaults
from openvigie.correlation import ConfirmationTask, MultiTowerCorrelator, Tower
from openvigie.detectors import get_detector
from openvigie.events import DetectionEvent, new_event_id
from openvigie.hwcheck import check_frame_sanity, check_host_reachable
from openvigie.pipeline import DetectionPipeline, build_outbox, build_transport
from openvigie.sources import SyntheticScene, SyntheticSource
from openvigie.transport import FileTransport, HttpTransport, MemoryTransport, Outbox, free_disk_mb

UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
class TestBuildTransportEtOutbox:
    """Fabriques utilisées par tout site réel lisant sa config YAML — jamais
    exercées directement jusqu'ici (les tests construisaient les transports
    à la main)."""

    def test_transport_none(self):
        cfg = tier_defaults("medium")
        cfg.network.transport = "none"
        assert build_transport(cfg) is None

    def test_transport_memory(self):
        cfg = tier_defaults("medium")
        cfg.network.transport = "memory"
        assert isinstance(build_transport(cfg), MemoryTransport)

    def test_transport_file_relatif_a_base_dir(self, tmp_path):
        cfg = tier_defaults("medium")
        cfg.network.transport = "file"
        cfg.network.events_path = "sub/events.jsonl"
        t = build_transport(cfg, base_dir=str(tmp_path))
        assert isinstance(t, FileTransport)
        assert t.path == tmp_path / "sub" / "events.jsonl"

    def test_transport_http_lit_le_jeton_dans_lenvironnement(self, monkeypatch):
        cfg = tier_defaults("medium")
        cfg.network.transport = "http"
        cfg.network.url = "https://example.org/events"
        cfg.network.token_env = "OPENVIGIE_TOKEN"
        monkeypatch.setenv("OPENVIGIE_TOKEN", "secret-123")
        t = build_transport(cfg)
        assert isinstance(t, HttpTransport)
        assert t.headers["Authorization"] == "Bearer secret-123"

    def test_transport_http_sans_jeton_dans_lenvironnement(self, monkeypatch):
        cfg = tier_defaults("medium")
        cfg.network.transport = "http"
        cfg.network.url = "https://example.org/events"
        monkeypatch.delenv(cfg.network.token_env, raising=False)
        t = build_transport(cfg)
        assert "Authorization" not in t.headers

    def test_outbox_relatif_a_base_dir(self, tmp_path):
        cfg = tier_defaults("medium")
        cfg.network.outbox_dir = "data/outbox"
        box = build_outbox(cfg, base_dir=str(tmp_path))
        assert isinstance(box, Outbox)
        assert box.dir == tmp_path / "data" / "outbox"

    def test_pipeline_avec_fabriques_completes(self, tmp_path):
        """Bout en bout : configuration → transport → outbox → pipeline, comme
        le ferait réellement un agent de site lisant un fichier YAML."""
        cfg = tier_defaults("medium")
        cfg.operating.mode = "alert"
        cfg.operating.allow_uncalibrated_alerts = True
        cfg.network.transport = "file"
        transport = build_transport(cfg, base_dir=str(tmp_path))
        outbox = build_outbox(cfg, base_dir=str(tmp_path))
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"),
                                 outbox=outbox, transport=transport)
        assert pipe.can_transmit


# --------------------------------------------------------------------------- #
class TestPoseEtTriangulationCombinees:
    """Le chemin le plus complexe de ``_emit`` : une vue calibrée (pose réelle,
    pas seulement azimut+focale) ET une alerte triangulée depuis une tour
    voisine. Chaque brique est testée isolément ailleurs ; jamais ensemble."""

    def test_alerte_avec_pose_et_pair_de_triangulation(self):
        from openvigie.calibration import CameraPose

        cfg = tier_defaults("full")
        cfg.operating.mode = "shadow"
        cfg.pipeline.use_triangulation = True
        pipe = DetectionPipeline(cfg, detector=get_detector("classical"))

        sensor = cfg.optics.sensor_spec()
        pose = CameraPose(yaw_deg=118.0, pitch_deg=-cfg.optics.tilt_deg, roll_deg=0.3,
                          focal_mm=cfg.optics.focal_mm, sensor=sensor, width_px=320, height_px=180)
        pipe.register_view("V00", azimuth_deg=118.0, focal_mm=cfg.optics.focal_mm, pose=pose)

        from openvigie.geometry import horizon_row
        hr = int(round(horizon_row(sensor, cfg.optics.focal_mm, cfg.optics.tilt_deg) * 180 / sensor.height_px))
        scene = SyntheticScene(height=180, width=320, horizon_row=max(10, min(150, hr)))
        src = SyntheticSource(scene=scene, mode="plume", n_background=6, n_plume=8)

        t = 0.0
        ts = dt.datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        peer = (44.02, 3.05, 210.0)  # (lat, lon, azimut) d'une tour voisine
        while True:
            item = src.read()
            if item is None:
                break
            pipe.process_frame("V00", item[0], ts + dt.timedelta(seconds=t), t_monotonic=t,
                               peer_bearing=peer)
            t += 30.0

        assert pipe.stats["alerts"] >= 1
        alert = pipe.alerts.emitted[0]
        # avec pose calibrée : le relèvement vient de pose.unproject, pas de
        # pixel_to_bearing — et la localisation doit être triangulée puisque
        # peer_bearing est fourni et la triangulation activée.
        assert alert.localization == "triangulated"
        assert alert.latitude is not None


# --------------------------------------------------------------------------- #
class TestHwcheckSeuilsWarn:
    """Épingle les seuils « warn », auparavant vérifiés par des assertions trop
    larges (``in ("warn", "fail")``) qui n'auraient pas détecté une inversion
    de seuil entre les branches ok/warn/fail."""

    def test_host_reachable_ok_sur_un_port_local_reel(self):
        """Ouvre un vrai socket en boucle locale plutôt que de dépendre d'un
        service externe — rapide, déterministe, sans réseau."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            r = check_host_reachable("127.0.0.1", port=port, timeout_s=1.0)
            assert r.status == "ok"
            assert r.value is not None and r.value < 200
        finally:
            srv.close()

    def test_frame_sanity_warn_image_sombre_distinct_de_surexposee(self):
        rng = np.random.default_rng(3)
        sombre = np.clip(rng.normal(5.0, 2.0, (64, 64, 3)), 0, 255).astype(np.float32)
        r = check_frame_sanity(sombre)
        assert r.status == "warn"
        assert "sombre" in r.message


# --------------------------------------------------------------------------- #
class TestCorrelationSerialisation:
    def test_confirmation_task_as_dict(self):
        task = ConfirmationTask(site_id="B", bearing_deg=45.678, distance_m=4200.4,
                                priority=0.812345, reason="confirmation", source_event_id="e1")
        d = task.as_dict()
        assert d["site_id"] == "B"
        assert d["bearing_deg"] == pytest.approx(45.68, abs=0.01)
        assert d["source_event_id"] == "e1"

    def test_cluster_as_dict(self):
        towers = {"A": Tower("A", 44.0, 3.0), "B": Tower("B", 44.0, 3.1)}
        corr = MultiTowerCorrelator(towers)
        e = DetectionEvent(event_id=new_event_id(), site_id="A", camera_id="V00",
                           detected_at="2026-08-01T14:00:00+00:00", bearing_deg=45.0,
                           fused_score=0.7)
        cluster = corr.cluster([e])[0]
        d = cluster.as_dict()
        assert d["n_events"] == 1
        assert d["towers"] == ["A"]

    def test_confiance_augmente_avec_confirmation_ptz(self):
        towers = {"A": Tower("A", 44.0, 3.0)}
        corr = MultiTowerCorrelator(towers)
        e = DetectionEvent(event_id=new_event_id(), site_id="A", camera_id="V00",
                           detected_at="2026-08-01T14:00:00+00:00", bearing_deg=45.0,
                           fused_score=0.6, ptz_confirmed=True)
        cluster = corr.cluster([e])[0]
        assert cluster.confidence > 0.6

    def test_date_horodatage_malforme_ne_leve_pas(self):
        """``_parse_ts`` doit absorber un horodatage malformé plutôt que de
        faire planter tout le regroupement multi-tours sur un seul événement
        corrompu — une seule tour défaillante ne doit pas priver les autres
        de corrélation."""
        towers = {"A": Tower("A", 44.0, 3.0)}
        corr = MultiTowerCorrelator(towers)
        e = DetectionEvent(event_id=new_event_id(), site_id="A", camera_id="V00",
                           detected_at="pas-une-date", bearing_deg=45.0, fused_score=0.6)
        clusters = corr.cluster([e])
        assert len(clusters) == 1  # ne plante pas, traite l'événement isolément


# --------------------------------------------------------------------------- #
class TestTransportUtilitaires:
    def test_espace_disque_libre_positif(self):
        v = free_disk_mb(".")
        assert v is None or v > 0

    def test_espace_disque_chemin_invalide(self):
        assert free_disk_mb("/chemin/qui/nexiste/vraiment/pas/du/tout") is None

    def test_file_transport_echoue_proprement_sur_chemin_impossible(self):
        """Écrire dans un répertoire qui ne peut pas être créé (chemin passant
        par un fichier existant) doit renvoyer False, pas lever."""
        t = FileTransport("/etc/passwd/impossible/events.jsonl")
        ev = DetectionEvent(event_id=new_event_id(), site_id="s", camera_id="V00",
                            detected_at="2026-08-01T14:00:00+00:00")
        assert t.send(ev) is False

    def test_beat_absorbe_une_exception_du_transport(self):
        from openvigie.transport import HealthMonitor

        class Explosif(MemoryTransport):
            def send_health(self, snapshot):
                raise RuntimeError("panne de transport")

        hm = HealthMonitor("site-1", heartbeat_interval_s=0.0)
        hm.record_frame("V00", background_ready=True)
        # ne doit pas lever, même si le transport explose
        result = hm.beat(Explosif())
        assert result is not None
