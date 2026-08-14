"""Agent de site continu : acquisition, ordonnancement et cycle de vie."""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from .alerting import AlertStore
from .config import AgentCameraConfig, AgentViewConfig, SiteConfig
from .pipeline import DetectionPipeline, build_outbox, build_transport
from .ptz import CgiPtz, PtzBackend, SerialPelcoPtz, SimulatedPtz
from .sources import FileSequenceSource, FrameSource, RtspSource, SnapshotHttpSource


class StopSignal(Protocol):
    """Sous-ensemble de ``threading.Event`` requis par la boucle."""

    def is_set(self) -> bool:
        ...

    def wait(self, timeout: float | None = None) -> bool:
        ...


SourceFactory = Callable[[AgentCameraConfig, Path], FrameSource]
PtzFactory = Callable[[AgentCameraConfig], PtzBackend | None]


def _path_from_base(value: str, base_dir: Path) -> Path:
    """Résout un chemin de configuration relativement au fichier de site."""
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _rtsp_url_with_auth(url: str, user: str, password: str) -> str:
    """Ajoute les identifiants en mémoire à une URL RTSP validée sans secret."""
    if not user and not password:
        return url
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    credentials = quote(user, safe="")
    if password:
        credentials += f":{quote(password, safe='')}"
    return urlunsplit((parsed.scheme, f"{credentials}@{host}", parsed.path, parsed.query, parsed.fragment))


def build_source(camera: AgentCameraConfig, base_dir: Path) -> FrameSource:
    """Construit la source déclarée avec ses chemins et secrets résolus."""
    from .compat import HAS_CV2

    if not HAS_CV2:
        raise RuntimeError(
            f"source '{camera.source}' de {camera.camera_id}: OpenCV requis "
            "(`pip install openvigie[agent]`)"
        )
    password = os.environ.get(camera.password_env, "") if camera.password_env else ""
    if camera.source == "snapshot":
        return SnapshotHttpSource(
            camera.url,
            user=camera.user,
            password=password,
            period_s=camera.frame_interval_s,
            timeout_s=camera.timeout_s,
        )
    if camera.source == "rtsp":
        return RtspSource(_rtsp_url_with_auth(camera.url, camera.user, password))
    directory = _path_from_base(camera.directory, base_dir)
    if not directory.is_dir():
        raise ValueError(f"répertoire source introuvable pour {camera.camera_id}: {directory}")
    return FileSequenceSource(
        directory,
        pattern=camera.pattern,
        period_s=camera.frame_interval_s,
    )


def build_ptz(camera: AgentCameraConfig) -> PtzBackend | None:
    """Construit le pilote PTZ optionnel d'une caméra."""
    if camera.ptz_backend == "none":
        return None
    if camera.ptz_backend == "simulated":
        return SimulatedPtz()
    if camera.ptz_backend == "pelco_d":
        return SerialPelcoPtz(
            port=camera.serial_port,
            baudrate=camera.baudrate,
            address=camera.address,
        )
    password = os.environ.get(camera.password_env, "") if camera.password_env else ""
    return CgiPtz(
        camera.ptz_url,
        user=camera.user,
        password=password,
        timeout_s=camera.timeout_s,
    )


def validate_agent_config(
    cfg: SiteConfig,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Valide les règles croisées nécessaires avant toute ouverture matérielle."""
    if not cfg.agent.cameras:
        raise ValueError(
            "agent.cameras est vide : déclarer au moins une caméra et une vue "
            "(voir docs/AGENT_CONTINU.md)"
        )
    if cfg.network.heartbeat_interval_s <= 0:
        raise ValueError("network.heartbeat_interval_s doit être > 0 pour openvigie run")
    environment = os.environ if environ is None else environ
    for camera in cfg.agent.cameras:
        if camera.password_env and camera.password_env not in environment:
            raise ValueError(
                f"variable d'environnement {camera.password_env!r} absente "
                f"pour la caméra {camera.camera_id!r}"
            )
        if cfg.scan.mode == "fixed":
            if len(camera.views) != 1:
                raise ValueError(
                    f"caméra fixe {camera.camera_id!r}: exactement une vue est requise"
                )
            if camera.ptz_backend != "none" or camera.views[0].preset is not None:
                raise ValueError(
                    f"caméra fixe {camera.camera_id!r}: backend PTZ et preset sont interdits"
                )
            continue
        if camera.ptz_backend == "none":
            raise ValueError(f"caméra PTZ {camera.camera_id!r}: ptz_backend est requis")
        presets = [view.preset for view in camera.views]
        if any(preset is None for preset in presets):
            raise ValueError(f"caméra PTZ {camera.camera_id!r}: chaque vue exige un preset")
        if len(presets) != len(set(presets)):
            raise ValueError(f"caméra PTZ {camera.camera_id!r}: presets dupliqués")


@dataclass
class _CameraRuntime:
    """État borné d'une caméra pendant l'exécution."""

    config: AgentCameraConfig
    source: FrameSource | None = None
    ptz: PtzBackend | None = None
    next_due: float = 0.0
    consecutive_failures: int = 0
    view_index: int = 0
    exhausted: bool = False

    @property
    def view(self) -> AgentViewConfig:
        return self.config.views[self.view_index]

    def advance_view(self) -> None:
        """Passe à la vue suivante sans faire croître la mémoire."""
        self.view_index = (self.view_index + 1) % len(self.config.views)


class ContinuousAgent:
    """Exécute le pipeline en continu avec reprise indépendante par caméra."""

    def __init__(
        self,
        cfg: SiteConfig,
        base_dir: str | Path = ".",
        *,
        pipeline: DetectionPipeline | None = None,
        source_factory: SourceFactory = build_source,
        ptz_factory: PtzFactory = build_ptz,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        validate_agent_config(cfg)
        self.cfg = cfg
        self.base_dir = Path(base_dir)
        self.clock = clock
        self.logger = logger or logging.getLogger("openvigie.agent")
        self.source_factory = source_factory
        self.ptz_factory = ptz_factory
        self.pipeline = pipeline or DetectionPipeline(
            cfg,
            alert_store=AlertStore(
                log_path=_path_from_base(cfg.agent.alert_log_path, self.base_dir)
            ),
            outbox=build_outbox(cfg, str(self.base_dir)),
            transport=build_transport(cfg, str(self.base_dir)),
        )
        self.runtimes = [_CameraRuntime(camera) for camera in cfg.agent.cameras]
        for runtime in self.runtimes:
            for view in runtime.config.views:
                self.pipeline.register_view(
                    view.view_id,
                    azimuth_deg=view.azimuth_deg,
                    focal_mm=view.focal_mm,
                )
        self._started_at = 0.0
        self._next_flush = 0.0
        self._next_heartbeat = 0.0
        self._next_status = 0.0
        self._attempted_views: set[tuple[str, str]] = set()
        self.stats: dict[str, object] = {
            "frames_processed": 0,
            "source_failures": 0,
            "source_restarts": 0,
            "processing_failures": 0,
            "ptz_failures": 0,
            "flushes": 0,
            "events_sent": 0,
            "heartbeats": 0,
            "maintenance_failures": 0,
            "shutdown_errors": 0,
            "frame_statuses": {},
        }

    def backoff_s(self, consecutive_failures: int) -> float:
        """Calcule le délai exponentiel borné d'une caméra."""
        ratio = self.cfg.agent.retry_max_s / self.cfg.agent.retry_initial_s
        max_exponent = max(0, math.ceil(math.log2(ratio))) if ratio > 1.0 else 0
        exponent = max(0, consecutive_failures - 1)
        if exponent >= max_exponent:
            return self.cfg.agent.retry_max_s
        return self.cfg.agent.retry_initial_s * (2**exponent)

    def _start_resources(self) -> None:
        """Ouvre toutes les ressources ou fait échouer le démarrage clairement."""
        for runtime in self.runtimes:
            runtime.source = self.source_factory(runtime.config, self.base_dir)
            runtime.ptz = self.ptz_factory(runtime.config)

    def _close_source(self, runtime: _CameraRuntime) -> None:
        if runtime.source is None:
            return
        try:
            runtime.source.close()
        except Exception as exc:
            self.stats["shutdown_errors"] = int(self.stats["shutdown_errors"]) + 1
            self.logger.warning(
                "fermeture source %s impossible: %s", runtime.config.camera_id, exc
            )
        finally:
            runtime.source = None

    def _close_resources(self) -> None:
        """Ferme chaque ressource une fois, même après une erreur partielle."""
        for runtime in self.runtimes:
            self._close_source(runtime)
            if runtime.ptz is not None:
                try:
                    runtime.ptz.close()
                except Exception as exc:
                    self.stats["shutdown_errors"] = int(self.stats["shutdown_errors"]) + 1
                    self.logger.warning(
                        "fermeture PTZ %s impossible: %s", runtime.config.camera_id, exc
                    )
                finally:
                    runtime.ptz = None
        transport = self.pipeline.transport
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                self.stats["shutdown_errors"] = int(self.stats["shutdown_errors"]) + 1
                self.logger.warning("fermeture transport impossible: %s", exc)

    def _record_health_failure(
        self,
        runtime: _CameraRuntime,
        note: str,
        view: AgentViewConfig | None = None,
    ) -> None:
        views = [view] if view is not None else runtime.config.views
        for affected in views:
            self.pipeline.health.record_failure(affected.view_id, note)

    def _schedule_failure(
        self,
        runtime: _CameraRuntime,
        kind: str,
        note: str,
        *,
        close_source: bool,
        view: AgentViewConfig | None = None,
    ) -> None:
        runtime.consecutive_failures += 1
        delay = self.backoff_s(runtime.consecutive_failures)
        runtime.next_due = self.clock() + delay
        key = f"{kind}_failures"
        self.stats[key] = int(self.stats[key]) + 1
        self._record_health_failure(runtime, note, view)
        if close_source:
            self._close_source(runtime)
        self.logger.warning(
            "caméra %s: %s; nouvelle tentative dans %.1f s",
            runtime.config.camera_id,
            note,
            delay,
        )

    def _ensure_source(self, runtime: _CameraRuntime) -> bool:
        if runtime.source is not None:
            return True
        try:
            runtime.source = self.source_factory(runtime.config, self.base_dir)
            self.stats["source_restarts"] = int(self.stats["source_restarts"]) + 1
            return True
        except Exception as exc:
            self._schedule_failure(
                runtime,
                "source",
                f"recréation de source impossible ({type(exc).__name__}: {exc})",
                close_source=False,
            )
            return False

    def _schedule_success(self, runtime: _CameraRuntime, attempt_started_at: float) -> None:
        runtime.consecutive_failures = 0
        if self.cfg.scan.mode == "ptz":
            runtime.next_due = self.clock() + self.cfg.scan.dwell_s
            return
        runtime.next_due = max(
            attempt_started_at + runtime.config.frame_interval_s,
            self.clock(),
        )

    def _process_view(
        self,
        runtime: _CameraRuntime,
        stop_event: StopSignal,
    ) -> bool:
        """Tente une visite et renvoie vrai si une image a été traitée."""
        view = runtime.view
        self._attempted_views.add((runtime.config.camera_id, view.view_id))
        attempt_started_at = self.clock()

        if self.cfg.scan.mode == "ptz":
            moved = False
            ptz_error = ""
            try:
                moved = (
                    runtime.ptz is not None
                    and view.preset is not None
                    and runtime.ptz.goto_preset(view.preset)
                )
            except Exception as exc:
                ptz_error = f" ({type(exc).__name__}: {exc})"
            if not moved:
                self._schedule_failure(
                    runtime,
                    "ptz",
                    f"preset {view.preset} refusé{ptz_error}",
                    close_source=False,
                    view=view,
                )
                runtime.advance_view()
                return False
            if stop_event.wait(self.cfg.scan.settle_s):
                return False

        if not self._ensure_source(runtime):
            runtime.advance_view()
            return False

        try:
            item = runtime.source.read() if runtime.source is not None else None
        except Exception as exc:
            self._schedule_failure(
                runtime,
                "source",
                f"acquisition impossible ({type(exc).__name__}: {exc})",
                close_source=True,
            )
            runtime.advance_view()
            return False

        if item is None:
            if runtime.source is not None and runtime.source.finite:
                runtime.exhausted = True
                runtime.next_due = float("inf")
                self.logger.info("source finie: %s", runtime.config.camera_id)
            else:
                self._schedule_failure(
                    runtime,
                    "source",
                    "aucune image reçue",
                    close_source=True,
                )
            runtime.advance_view()
            return False

        frame, timestamp = item
        try:
            result = self.pipeline.process_frame(
                view.view_id,
                frame,
                timestamp,
                t_monotonic=self.clock(),
            )
        except Exception as exc:
            self.stats["processing_failures"] = int(self.stats["processing_failures"]) + 1
            note = f"traitement impossible ({type(exc).__name__}: {exc})"
            self._record_health_failure(runtime, note, view)
            self.logger.warning("vue %s: %s", view.view_id, note)
            self._schedule_success(runtime, attempt_started_at)
            runtime.advance_view()
            return False

        self.stats["frames_processed"] = int(self.stats["frames_processed"]) + 1
        statuses = self.stats["frame_statuses"]
        if isinstance(statuses, dict):
            statuses[result.status] = int(statuses.get(result.status, 0)) + 1
        self._schedule_success(runtime, attempt_started_at)
        runtime.advance_view()
        return True

    def _maintenance(self, now: float, *, force_flush: bool = False) -> None:
        if force_flush or now >= self._next_flush:
            try:
                report = self.pipeline.flush()
                self.stats["flushes"] = int(self.stats["flushes"]) + 1
                self.stats["events_sent"] = int(self.stats["events_sent"]) + int(report["sent"])
            except Exception as exc:
                self.stats["maintenance_failures"] = int(self.stats["maintenance_failures"]) + 1
                self.logger.warning("flush outbox impossible: %s", exc)
            self._next_flush = now + self.cfg.agent.flush_interval_s
        if now >= self._next_heartbeat:
            try:
                if self.pipeline.heartbeat() is not None:
                    self.stats["heartbeats"] = int(self.stats["heartbeats"]) + 1
            except Exception as exc:
                self.stats["maintenance_failures"] = int(self.stats["maintenance_failures"]) + 1
                self.logger.warning("heartbeat impossible: %s", exc)
            self._next_heartbeat = now + self.cfg.network.heartbeat_interval_s
        if now >= self._next_status:
            try:
                pending = len(self.pipeline.outbox) if self.pipeline.outbox is not None else 0
                self.logger.info(
                    "état: %s images, %s erreurs source, %s alertes, %s en attente",
                    self.stats["frames_processed"],
                    self.stats["source_failures"],
                    self.pipeline.stats["alerts"],
                    pending,
                )
            except Exception as exc:
                self.stats["maintenance_failures"] = int(self.stats["maintenance_failures"]) + 1
                self.logger.warning("résumé périodique impossible: %s", exc)
            self._next_status = now + self.cfg.agent.status_interval_s

    def _next_wakeup(self) -> float:
        camera_due = min(
            (runtime.next_due for runtime in self.runtimes if not runtime.exhausted),
            default=float("inf"),
        )
        return min(camera_due, self._next_flush, self._next_heartbeat, self._next_status)

    def summary(self, reason: str) -> dict[str, object]:
        """Produit un résumé JSON-sérialisable et borné de l'exécution."""
        try:
            pipeline_summary: dict[str, object] = self.pipeline.summary()
        except Exception as exc:
            self.stats["maintenance_failures"] = int(self.stats["maintenance_failures"]) + 1
            self.logger.warning("résumé final du pipeline impossible: %s", exc)
            pipeline_summary = {"summary_error": type(exc).__name__}
        return {
            "reason": reason,
            "uptime_s": round(max(0.0, self.clock() - self._started_at), 3),
            **self.stats,
            "pipeline": pipeline_summary,
        }

    def run(
        self,
        *,
        stop_event: StopSignal | None = None,
        once: bool = False,
        max_frames: int | None = None,
    ) -> dict[str, object]:
        """Tourne jusqu'au signal, à la fin des sources ou à la limite demandée."""
        if max_frames is not None and max_frames <= 0:
            raise ValueError("max_frames doit être > 0")
        stopper = stop_event or Event()
        total_views = sum(len(runtime.config.views) for runtime in self.runtimes)
        reason = "signal"
        self._started_at = self.clock()
        self._next_flush = self._started_at
        self._next_heartbeat = self._started_at
        self._next_status = self._started_at
        for runtime in self.runtimes:
            runtime.next_due = self._started_at

        try:
            self._start_resources()
            self.logger.info(
                "agent démarré: site=%s mode=%s scan=%s caméras=%s vues=%s",
                self.cfg.site_id,
                self.cfg.operating.mode,
                self.cfg.scan.mode,
                len(self.runtimes),
                total_views,
            )
            while not stopper.is_set():
                now = self.clock()
                for runtime in self.runtimes:
                    if runtime.exhausted or runtime.next_due > now or stopper.is_set():
                        continue
                    self._process_view(runtime, stopper)
                    now = self.clock()
                    if max_frames is not None and int(self.stats["frames_processed"]) >= max_frames:
                        reason = "max_frames"
                        break

                self._maintenance(self.clock())
                if reason == "max_frames":
                    break
                if once and len(self._attempted_views) >= total_views:
                    reason = "once"
                    break
                if all(runtime.exhausted for runtime in self.runtimes):
                    reason = "sources_exhausted"
                    break
                delay = max(0.0, self._next_wakeup() - self.clock())
                if stopper.wait(delay):
                    reason = "signal"
                    break
        finally:
            self._maintenance(self.clock(), force_flush=True)
            self._close_resources()
        self.logger.info("agent arrêté: raison=%s images=%s", reason, self.stats["frames_processed"])
        return self.summary(reason)
