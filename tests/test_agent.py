"""Tests de l'agent continu, de sa configuration et de son cycle de vie."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest

from openvigie.agent import ContinuousAgent, build_source, validate_agent_config
from openvigie.cli import main
from openvigie.config import (
    AgentCameraConfig,
    AgentConfig,
    AgentViewConfig,
    SiteConfig,
    save_site_config,
    site_config_from_dict,
    tier_defaults,
)
from openvigie.detectors import get_detector
from openvigie.pipeline import DetectionPipeline
from openvigie.ptz import PtzBackend
from openvigie.sources import FileSequenceSource, FrameSource, SyntheticScene, SyntheticSource
from openvigie.transport import MemoryTransport, Outbox

UTC = dt.timezone.utc


class FakeClock:
    """Horloge monotone contrôlée par les attentes des tests."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class AdvancingStop:
    """Événement qui avance l'horloge au lieu de dormir réellement."""

    def __init__(self, clock: FakeClock, stop_on_wait: bool = False) -> None:
        self.clock = clock
        self.stop_on_wait = stop_on_wait
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float | None = None) -> bool:
        delay = timeout or 0.0
        self.waits.append(delay)
        self.clock.value += delay
        if self.stop_on_wait:
            self.stopped = True
        return self.stopped


class SequenceSource(FrameSource):
    """Source injectable finie ou réseau, avec erreurs déterministes."""

    def __init__(self, items: list[object], *, finite: bool = False) -> None:
        self.items = list(items)
        self.finite = finite
        self.closed = False
        self.reads = 0

    def read(self) -> tuple[np.ndarray, dt.datetime] | None:
        self.reads += 1
        if not self.items:
            return None
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        assert item is None or isinstance(item, tuple)
        return item

    def close(self) -> None:
        self.closed = True


class FakePtz(PtzBackend):
    """Pilote PTZ enregistrant l'ordre des presets et sa fermeture."""

    name = "fake"

    def __init__(self, accepted: bool = True, error: Exception | None = None) -> None:
        self.accepted = accepted
        self.error = error
        self.commanded: list[int] = []
        self.closed = False

    def goto_preset(self, preset: int) -> bool:
        self.commanded.append(preset)
        if self.error is not None:
            raise self.error
        return self.accepted

    def close(self) -> None:
        self.closed = True


def frame(at_s: float = 0.0, *, aware: bool = True) -> tuple[np.ndarray, dt.datetime]:
    """Produit une petite image et un horodatage de test."""
    timestamp = dt.datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC if aware else None)
    return np.zeros((24, 32, 3), dtype=np.uint8), timestamp + dt.timedelta(seconds=at_s)


def fixed_config(*, cameras: int = 1, interval_s: float = 1.0) -> SiteConfig:
    """Configuration fixe minimale avec autant de sources que demandé."""
    cfg = tier_defaults("medium")
    cfg.scan.mode = "fixed"
    cfg.network.transport = "memory"
    cfg.network.heartbeat_interval_s = 100.0
    cfg.agent = AgentConfig(
        flush_interval_s=100.0,
        retry_initial_s=2.0,
        retry_max_s=8.0,
        status_interval_s=100.0,
        cameras=[
            AgentCameraConfig(
                camera_id=f"cam-{index}",
                source="snapshot",
                url=f"http://camera-{index}.lan/image.jpg",
                frame_interval_s=interval_s,
                views=[AgentViewConfig(view_id=f"V{index:02d}", azimuth_deg=index * 45.0)],
            )
            for index in range(cameras)
        ],
    )
    return cfg


def ptz_config(*, views: int = 2) -> SiteConfig:
    """Configuration PTZ minimale avec presets explicites."""
    cfg = tier_defaults("minimal")
    cfg.scan.mode = "ptz"
    cfg.scan.settle_s = 0.5
    cfg.scan.dwell_s = 2.0
    cfg.network.transport = "memory"
    cfg.network.heartbeat_interval_s = 100.0
    cfg.agent = AgentConfig(
        flush_interval_s=100.0,
        retry_initial_s=2.0,
        retry_max_s=8.0,
        status_interval_s=100.0,
        cameras=[AgentCameraConfig(
            camera_id="ptz-0",
            source="snapshot",
            url="http://ptz.lan/image.jpg",
            ptz_backend="simulated",
            views=[
                AgentViewConfig(
                    view_id=f"V{index:02d}",
                    azimuth_deg=index * 45.0,
                    preset=index + 2,
                )
                for index in range(views)
            ],
        )],
    )
    return cfg


def pipeline_for(cfg: SiteConfig, tmp_path: Path) -> DetectionPipeline:
    """Pipeline réel avec transport mémoire et outbox temporaire."""
    return DetectionPipeline(
        cfg,
        detector=get_detector("classical"),
        outbox=Outbox(tmp_path / "outbox"),
        transport=MemoryTransport(),
    )


class TestAgentConfiguration:
    def test_yaml_imbrique_est_strictement_converti(self) -> None:
        cfg = site_config_from_dict({
            "site_id": "tour-1",
            "tier": "medium",
            "scan": {"mode": "fixed"},
            "agent": {
                "cameras": [{
                    "camera_id": "cam-1",
                    "source": "snapshot",
                    "url": "https://cam.local/image.jpg",
                    "views": [{"view_id": "V00", "azimuth_deg": 90.0}],
                }],
            },
        })
        assert isinstance(cfg.agent.cameras[0], AgentCameraConfig)
        assert isinstance(cfg.agent.cameras[0].views[0], AgentViewConfig)
        validate_agent_config(cfg)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("retry_initial_s", 0.0, "retry_initial_s"),
            ("flush_interval_s", -1.0, "flush_interval_s"),
            ("status_interval_s", 0.0, "status_interval_s"),
            ("alert_log_path", "", "alert_log_path"),
            ("retry_max_s", float("nan"), "retry_max_s"),
        ],
    )
    def test_parametres_agent_invalides_refuses(
        self,
        field: str,
        value: float | str,
        message: str,
    ) -> None:
        kwargs = {field: value}
        with pytest.raises(ValueError, match=message):
            AgentConfig(**kwargs)

    def test_backoff_max_inferieur_refuse(self) -> None:
        with pytest.raises(ValueError, match="retry_max_s"):
            AgentConfig(retry_initial_s=4.0, retry_max_s=2.0)

    def test_url_avec_secret_refusee(self) -> None:
        with pytest.raises(ValueError, match="ne doit pas contenir"):
            AgentCameraConfig(
                camera_id="cam",
                url="http://admin:secret@camera.local/image.jpg",
                views=[AgentViewConfig(view_id="V00")],
            )

    def test_source_fichier_exige_un_repertoire(self) -> None:
        with pytest.raises(ValueError, match="directory"):
            AgentCameraConfig(
                camera_id="cam",
                source="files",
                views=[AgentViewConfig(view_id="V00")],
            )

    def test_ids_de_vue_globaux_uniques(self) -> None:
        cameras = [
            AgentCameraConfig(
                camera_id=f"cam-{index}",
                url=f"http://cam-{index}.lan/image.jpg",
                views=[AgentViewConfig(view_id="V00")],
            )
            for index in range(2)
        ]
        with pytest.raises(ValueError, match="view_id dupliqués"):
            AgentConfig(cameras=cameras)

    def test_variable_de_secret_absente_refusee(self) -> None:
        cfg = fixed_config()
        cfg.agent.cameras[0].password_env = "OPENVIGIE_MISSING_TEST_SECRET"
        with pytest.raises(ValueError, match="absente"):
            validate_agent_config(cfg, environ={})

    def test_topologie_fixe_refuse_plusieurs_vues(self) -> None:
        cfg = fixed_config()
        cfg.agent.cameras[0].views.append(AgentViewConfig(view_id="V01", azimuth_deg=45.0))
        with pytest.raises(ValueError, match="exactement une vue"):
            validate_agent_config(cfg)

    def test_topologie_ptz_exige_des_presets_uniques(self) -> None:
        cfg = ptz_config()
        cfg.agent.cameras[0].views[1].preset = cfg.agent.cameras[0].views[0].preset
        with pytest.raises(ValueError, match="presets dupliqués"):
            validate_agent_config(cfg)

    def test_capacite_agent_devient_disponible_avec_topologie(self) -> None:
        from openvigie.compat import HAS_CV2
        from openvigie.hwcheck import capabilities

        available, detail = capabilities(fixed_config())["agent continu"]
        assert available is HAS_CV2
        if HAS_CV2:
            assert "openvigie run" in detail
        else:
            assert "OpenCV absent" in detail


class TestSourceFactory:
    def test_snapshot_recoit_timeout_et_secret_depuis_environnement(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import openvigie.agent as agent_module
        import openvigie.compat as compat_module

        captured: dict[str, object] = {}
        marker = SequenceSource([])

        def fake_snapshot(url: str, **kwargs: object) -> FrameSource:
            captured.update(url=url, **kwargs)
            return marker

        monkeypatch.setattr(compat_module, "HAS_CV2", True)
        monkeypatch.setattr(agent_module, "SnapshotHttpSource", fake_snapshot)
        monkeypatch.setenv("OPENVIGIE_TEST_CAMERA_PASSWORD", "secret-test")
        camera = AgentCameraConfig(
            camera_id="cam",
            url="https://camera.local/image.jpg",
            user="operator",
            password_env="OPENVIGIE_TEST_CAMERA_PASSWORD",
            timeout_s=4.5,
            views=[AgentViewConfig(view_id="V00")],
        )

        assert build_source(camera, tmp_path) is marker
        assert captured == {
            "url": camera.url,
            "user": "operator",
            "password": "secret-test",
            "period_s": 30.0,
            "timeout_s": 4.5,
        }

    def test_rtsp_encode_le_secret_uniquement_en_memoire(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import openvigie.agent as agent_module
        import openvigie.compat as compat_module

        captured: list[str] = []
        marker = SequenceSource([])

        def fake_rtsp(url: str) -> FrameSource:
            captured.append(url)
            return marker

        monkeypatch.setattr(compat_module, "HAS_CV2", True)
        monkeypatch.setattr(agent_module, "RtspSource", fake_rtsp)
        monkeypatch.setenv("OPENVIGIE_TEST_RTSP_PASSWORD", "p@ss word")
        camera = AgentCameraConfig(
            camera_id="cam",
            source="rtsp",
            url="rtsp://camera.local:8554/stream0",
            user="user name",
            password_env="OPENVIGIE_TEST_RTSP_PASSWORD",
            views=[AgentViewConfig(view_id="V00")],
        )

        assert build_source(camera, tmp_path) is marker
        assert captured == ["rtsp://user%20name:p%40ss%20word@camera.local:8554/stream0"]
        assert "p@ss word" not in captured[0]

    def test_repertoire_fichier_est_relatif_a_la_configuration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import openvigie.compat as compat_module

        directory = tmp_path / "replay"
        directory.mkdir()
        monkeypatch.setattr(compat_module, "HAS_CV2", True)
        camera = AgentCameraConfig(
            camera_id="cam",
            source="files",
            directory="replay",
            pattern="*.jpeg",
            frame_interval_s=7.0,
            views=[AgentViewConfig(view_id="V00")],
        )

        source = build_source(camera, tmp_path)
        assert isinstance(source, FileSequenceSource)
        assert source.period_s == 7.0 and source.paths == []


class TestContinuousAgent:
    def test_pipeline_par_defaut_journalise_dans_la_base_du_site(self, tmp_path: Path) -> None:
        cfg = fixed_config()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            source_factory=lambda _camera, _base: SequenceSource([frame()]),
        )

        assert agent.pipeline.alerts.log_path == tmp_path / "data" / "alerts.jsonl"

    def test_une_passe_multi_cameras_traite_et_ferme(self, tmp_path: Path) -> None:
        cfg = fixed_config(cameras=2)
        sources = {
            camera.camera_id: SequenceSource([frame(index)])
            for index, camera in enumerate(cfg.agent.cameras)
        }
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda camera, _base: sources[camera.camera_id],
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), once=True)

        assert summary["reason"] == "once"
        assert summary["frames_processed"] == 2
        assert summary["frame_statuses"] == {"no_reference": 2}
        assert all(source.closed for source in sources.values())
        assert summary["flushes"] >= 1 and summary["heartbeats"] >= 1
        json.dumps(summary)

    def test_source_recree_apres_erreur_avec_backoff(self, tmp_path: Path) -> None:
        cfg = fixed_config()
        first = SequenceSource([TimeoutError("caméra muette")])
        second = SequenceSource([frame()])
        created = [first, second]
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: created.pop(0),
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), max_frames=1)

        assert summary["frames_processed"] == 1
        assert summary["source_failures"] == 1
        assert summary["source_restarts"] == 1
        assert clock.value >= 2.0
        assert first.closed and second.closed

    def test_panne_dune_camera_nempeche_pas_les_autres(self, tmp_path: Path) -> None:
        cfg = fixed_config(cameras=2)
        sources = {
            "cam-0": SequenceSource([TimeoutError("hors ligne")]),
            "cam-1": SequenceSource([frame()]),
        }
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda camera, _base: sources[camera.camera_id],
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), once=True)

        assert summary["frames_processed"] == 1
        assert summary["source_failures"] == 1
        assert sources["cam-1"].reads == 1

    def test_backoff_est_exponentiel_borne(self, tmp_path: Path) -> None:
        cfg = fixed_config()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: SequenceSource([frame()]),
        )
        assert [agent.backoff_s(n) for n in range(1, 6)] == [2.0, 4.0, 8.0, 8.0, 8.0]
        assert agent.backoff_s(10_000) == 8.0

    def test_erreur_pipeline_ne_tue_pas_la_boucle(self, tmp_path: Path) -> None:
        cfg = fixed_config()
        source = SequenceSource([frame(aware=False), frame(1.0)])
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: source,
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), max_frames=1)

        assert summary["processing_failures"] == 1
        assert summary["frames_processed"] == 1
        assert source.reads == 2

    def test_source_finie_termine_sans_rejeu(self, tmp_path: Path) -> None:
        cfg = fixed_config()
        source = SequenceSource([], finite=True)
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: source,
        )
        summary = agent.run()

        assert summary["reason"] == "sources_exhausted"
        assert summary["frames_processed"] == 0
        assert source.reads == 1 and source.closed

    def test_ptz_respecte_presets_et_stabilisation(self, tmp_path: Path) -> None:
        cfg = ptz_config()
        source = SequenceSource([frame(), frame(1.0)])
        ptz = FakePtz()
        clock = FakeClock()
        stopper = AdvancingStop(clock)
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: source,
            ptz_factory=lambda _camera: ptz,
            clock=clock,
        )
        summary = agent.run(stop_event=stopper, once=True)

        assert summary["frames_processed"] == 2
        assert ptz.commanded == [2, 3]
        assert stopper.waits.count(cfg.scan.settle_s) == 2
        assert ptz.closed

    def test_refus_ptz_nanalyse_aucune_image(self, tmp_path: Path) -> None:
        cfg = ptz_config(views=1)
        source = SequenceSource([frame()])
        ptz = FakePtz(accepted=False)
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: source,
            ptz_factory=lambda _camera: ptz,
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), once=True)

        assert summary["frames_processed"] == 0
        assert summary["ptz_failures"] == 1
        assert source.reads == 0

    def test_exception_ptz_est_bornee_au_cycle(self, tmp_path: Path) -> None:
        cfg = ptz_config(views=1)
        source = SequenceSource([frame()])
        ptz = FakePtz(error=OSError("liaison RS485 perdue"))
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: source,
            ptz_factory=lambda _camera: ptz,
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), once=True)

        assert summary["reason"] == "once"
        assert summary["ptz_failures"] == 1
        assert summary["frames_processed"] == 0

    def test_arret_pendant_stabilisation_ferme_tout(self, tmp_path: Path) -> None:
        cfg = ptz_config(views=1)
        source = SequenceSource([frame()])
        ptz = FakePtz()
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: source,
            ptz_factory=lambda _camera: ptz,
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock, stop_on_wait=True))

        assert summary["reason"] == "signal"
        assert summary["frames_processed"] == 0
        assert source.closed and ptz.closed

    def test_alerte_reste_en_outbox_si_transport_en_panne(self, tmp_path: Path) -> None:
        cfg = fixed_config(interval_s=1.0)
        cfg.operating.mode = "alert"
        cfg.operating.allow_uncalibrated_alerts = True
        cfg.agent.flush_interval_s = 1.0
        scene = SyntheticScene(height=180, width=320, horizon_row=90)
        source = SyntheticSource(scene=scene, n_background=6, n_plume=8, period_s=30.0)
        transport = MemoryTransport(fail_times=100)
        outbox = Outbox(tmp_path / "outbox")
        pipe = DetectionPipeline(
            cfg,
            detector=get_detector("classical"),
            outbox=outbox,
            transport=transport,
        )
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipe,
            source_factory=lambda _camera, _base: source,
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), max_frames=14)

        assert summary["pipeline"]["alerts"] >= 1
        assert len(outbox) >= 1
        assert transport.attempts >= 1

    def test_mode_shadow_ecrit_un_journal_local_durable(self, tmp_path: Path) -> None:
        cfg = fixed_config(interval_s=1.0)
        cfg.operating.mode = "shadow"
        scene = SyntheticScene(height=180, width=320, horizon_row=90)
        source = SyntheticSource(scene=scene, n_background=6, n_plume=8, period_s=30.0)
        clock = FakeClock()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            source_factory=lambda _camera, _base: source,
            clock=clock,
        )
        summary = agent.run(stop_event=AdvancingStop(clock), max_frames=14)

        log_path = tmp_path / cfg.agent.alert_log_path
        assert summary["pipeline"]["alerts"] >= 1
        assert log_path.is_file()
        assert json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])["view_id"] == "V00"

    def test_limite_de_frames_invalide(self, tmp_path: Path) -> None:
        cfg = fixed_config()
        agent = ContinuousAgent(
            cfg,
            tmp_path,
            pipeline=pipeline_for(cfg, tmp_path),
            source_factory=lambda _camera, _base: SequenceSource([frame()]),
        )
        with pytest.raises(ValueError, match="max_frames"):
            agent.run(max_frames=0)


class TestRunCli:
    def test_dry_run_valide_sans_ouvrir_la_camera(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = fixed_config()
        path = tmp_path / "site.yaml"
        save_site_config(cfg, path)

        assert main(["run", "-c", str(path), "--dry-run", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["site_id"] == cfg.site_id
        assert payload["cameras"][0]["views"] == ["V00"]
        assert not (tmp_path / "data").exists()

    def test_dry_run_refuse_configuration_sans_camera(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = tier_defaults("medium")
        path = tmp_path / "site.yaml"
        save_site_config(cfg, path)

        assert main(["run", "-c", str(path), "--dry-run"]) == 2
        assert "agent.cameras est vide" in capsys.readouterr().err
