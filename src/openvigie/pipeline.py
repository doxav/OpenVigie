"""Pipeline de détection.

Le même code sert les trois tiers ; seuls les étages activés changent. L'ordre
est toujours : recalage → fond → candidats classiques → classification des ROI
seulement → suivi → features physiques → fusion → hystérésis → alerte.

Ce n'est pas un détail d'implémentation : c'est ce qui rend le coût de calcul
proportionnel au nombre de *candidats* et non à la surface de l'image, donc ce
qui permet de faire tourner la même logique sur une carte caméra et sur un
Jetson.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from .alerting import Alert, AlertStore, localize, make_alert_id
from .background import BackgroundBank, BackgroundKey
from .candidates import contrast_loss, detect_global_change, extract_candidates, translucency
from .config import SiteConfig
from .detectors import BaseDetector, get_detector
from .events import DetectionEvent, bearing_uncertainty, event_from_alert
from .geometry import (
    flat_earth_distance_map,
    ground_mask,
    horizon_row,
    pixel_to_bearing,
)
from .masking import apply_masks
from .registration import align_to_reference
from .scoring import FusionModel, decide
from .tracking import Tracker, blob_to_observation, compute_features
from .transport import HealthMonitor, Outbox, Transport


def build_transport(cfg, base_dir: str | None = None) -> Transport | None:
    """Construit le transport décrit par la configuration.

    Le jeton d'authentification est lu dans l'environnement, jamais dans le
    fichier de configuration : un fichier de site finit toujours par être copié,
    versionné ou envoyé par message.
    """
    import os
    from pathlib import Path as _P

    from .transport import FileTransport, HttpTransport, MemoryTransport

    net = cfg.network
    root = _P(base_dir) if base_dir else _P(".")
    if net.transport == "none":
        return None
    if net.transport == "memory":
        return MemoryTransport()
    if net.transport == "file":
        return FileTransport(root / net.events_path)
    return HttpTransport(
        net.url,
        token=os.environ.get(net.token_env, ""),
        health_url=net.health_url or None,
        verify=net.verify_tls,
    )


def build_outbox(cfg, base_dir: str | None = None) -> Outbox:
    from pathlib import Path as _P

    root = _P(base_dir) if base_dir else _P(".")
    return Outbox(
        root / cfg.network.outbox_dir,
        max_attempts=cfg.network.max_attempts,
        max_entries=cfg.network.max_queue_entries,
    )


def view_maps_from_dem(
    dem,
    cfg,
    view_azimuth_deg: float,
    focal_mm: float,
    width_px: int | None = None,
    height_px: int | None = None,
    step_m: float = 25.0,
):
    """Précalcule (distance_map, horizon_rows) d'une vue à partir d'un MNT.

    À exécuter une fois à l'installation et à mettre en cache : le coût à
    l'exécution est nul, et c'est ce qui donne des coordonnées exploitables
    plutôt qu'un simple azimut.
    """
    from .dem import distance_map_from_dem, fill_distance_gaps
    from .geometry import hfov_deg, vfov_deg

    sensor = cfg.optics.sensor_spec()
    w = width_px or sensor.width_px
    h = height_px or sensor.height_px
    dmap, horizon = distance_map_from_dem(
        dem,
        cfg.latitude,
        cfg.longitude,
        cfg.optics.camera_height_m,
        view_azimuth_deg,
        hfov_deg(sensor, focal_mm),
        vfov_deg(sensor, focal_mm),
        w,
        h,
        tilt_deg=cfg.optics.tilt_deg,
        max_distance_m=cfg.optics.max_distance_m,
        step_m=step_m,
    )
    return fill_distance_gaps(dmap), horizon


@dataclass
class ViewState:
    """État persistant d'une vue (caméra fixe ou preset)."""

    view_id: str
    azimuth_deg: float
    focal_mm: float
    distance_map: np.ndarray = field(repr=False, default=None)
    ground: np.ndarray = field(repr=False, default=None)
    horizon: int = 0
    horizon_rows: np.ndarray | None = field(repr=False, default=None)
    pose: object | None = None
    from_dem: bool = False
    frame_shape: tuple[int, int] | None = None
    # AUDIT P0-08 : le cache `recent_frames` (5 images RGB float32 par vue, soit
    # jusqu'à 288 Mio à 5 MP) n'était lu nulle part. Supprimé.


@dataclass
class FrameResult:
    """Trace complète d'une image traitée — la base de la journalisation."""

    view_id: str
    t: float
    status: str                  # ok | no_reference | misaligned | global_change
    n_candidates: int = 0
    n_tracks: int = 0
    alerts: list[Alert] = field(default_factory=list)
    detail: str = ""
    alignment_px: float = 0.0
    track_scores: list[float] = field(default_factory=list)


class DetectionPipeline:
    """Orchestrateur. Sans état global : une instance par site."""

    def __init__(
        self,
        cfg: SiteConfig,
        detector: BaseDetector | None = None,
        alert_store: AlertStore | None = None,
        model_version: str = "dev",
        outbox: Outbox | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.cfg = cfg
        self.sensor = cfg.optics.sensor_spec()
        self.degraded_reason: str | None = None
        self.detector = detector or self._build_detector()
        self.background = BackgroundBank(buffer_size=9, min_samples=3)
        self.tracker = Tracker()
        self.fusion: FusionModel = cfg.fusion_model()
        self.alerts = alert_store or AlertStore()
        self.views: dict[str, ViewState] = {}
        self.model_version = model_version
        self.outbox = outbox
        self.transport = transport
        self.health = HealthMonitor(cfg.site_id, cfg.network.heartbeat_interval_s)
        # Incertitude d'azimut effectivement utilisée. Un étalonnage par trafic
        # aérien la remplace par la valeur mesurée, souvent cent fois meilleure.
        self.bearing_sigma_deg = cfg.calibration.bearing_sigma_deg
        self.events: list[DetectionEvent] = []
        self.max_retained_events = 500
        self.mode = cfg.operating.mode
        self._masks = {k: [tuple(b) for b in v] for k, v in cfg.masks.items()}
        self.stats = {
            "frames": 0, "candidates": 0, "alerts": 0, "skipped": 0, "queued": 0,
            "suppressed_by_mode": 0, "not_transmitted": 0,
        }

    def _build_detector(self) -> BaseDetector:
        requested = self.cfg.pipeline.detector_backend
        try:
            return get_detector(requested, **self.cfg.pipeline.detector_kwargs)
        except (RuntimeError, OSError, ImportError, TypeError) as exc:
            # Repli explicite : un backend indisponible ne doit jamais éteindre
            # la surveillance, mais l'exploitant doit le voir dans les logs et
            # dans le résumé — une dégradation silencieuse serait pire qu'une panne.
            self.degraded_reason = f"backend '{requested}' indisponible ({exc}) → repli sur 'classical'"
            return get_detector("classical")

    # -- gestion des vues --------------------------------------------------- #
    def register_view(
        self,
        view_id: str,
        azimuth_deg: float,
        focal_mm: float | None = None,
        distance_map: np.ndarray | None = None,
        horizon_rows: np.ndarray | None = None,
        pose=None,
    ) -> ViewState:
        """Enregistre une vue.

        Si ``distance_map`` est fourni (issu d'un ray-casting sur MNT), il
        remplace le modèle terre plate : les distances deviennent réelles et la
        ligne d'horizon suit les crêtes au lieu d'être une horizontale.
        """
        focal = focal_mm if focal_mm is not None else self.cfg.optics.focal_mm
        from_dem = distance_map is not None
        dmap = distance_map if from_dem else flat_earth_distance_map(
            self.sensor,
            focal,
            self.cfg.optics.camera_height_m,
            self.cfg.optics.tilt_deg,
            self.cfg.optics.max_distance_m,
        )
        state = ViewState(
            view_id=view_id,
            azimuth_deg=azimuth_deg,
            focal_mm=focal,
            distance_map=dmap,
            ground=ground_mask(dmap),
            horizon=horizon_row(self.sensor, focal, self.cfg.optics.tilt_deg),
            horizon_rows=horizon_rows,
            from_dem=from_dem,
            pose=pose,
        )
        self.views[view_id] = state
        return state

    def _resized_maps(self, state: ViewState, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, int]:
        """Adapte les cartes à la résolution réellement fournie (sous-échantillonnage)."""
        h, w = shape
        if state.distance_map.shape == (h, w):
            return state.distance_map, state.ground, state.horizon
        yi = np.linspace(0, state.distance_map.shape[0] - 1, h).astype(int)
        xi = np.linspace(0, state.distance_map.shape[1] - 1, w).astype(int)
        dmap = state.distance_map[np.ix_(yi, xi)]
        scale = h / state.distance_map.shape[0]
        return dmap, np.isfinite(dmap), int(round(state.horizon * scale))

    # -- traitement --------------------------------------------------------- #
    def process_frame(
        self,
        view_id: str,
        frame: np.ndarray,
        timestamp: dt.datetime,
        t_monotonic: float | None = None,
        peer_bearing: tuple[float, float, float] | None = None,
    ) -> FrameResult:
        """Traite une image d'une vue et renvoie la trace du traitement."""
        if view_id not in self.views:
            raise KeyError(f"vue non enregistrée: '{view_id}' (appeler register_view)")
        state = self.views[view_id]
        t = t_monotonic if t_monotonic is not None else timestamp.timestamp()
        self.stats["frames"] += 1

        # AUDIT P0-21 : masquage AVANT toute analyse, donc avant tout stockage et
        # toute transmission. Un masque appliqué plus tard ne protégerait rien :
        # les pixels auraient déjà servi à produire une vignette ou un fond.
        frame = apply_masks(frame, self._masks.get(view_id))

        # AUDIT P0-12 : les horodatages naïfs étaient ensuite interprétés comme
        # de l'UTC. Sur deux tours dans des fuseaux différents, ou au passage à
        # l'heure d'été, la corrélation multi-tours devenait fausse en silence.
        if timestamp.tzinfo is None:
            raise ValueError(
                f"horodatage naïf pour la vue '{view_id}' : fournir un datetime "
                "avec fuseau (UTC de préférence). Voir docs/CONNECTIVITE.md."
            )

        key = BackgroundKey.build(
            view_id, timestamp,
            sunrise_h=self.cfg.sunrise_h, sunset_h=self.cfg.sunset_h,
        )
        reference = self.background.reference(key)
        if reference is None:
            self.background.update(key, frame)
            self.stats["skipped"] += 1
            self.health.record_frame(view_id, image_status="ok", background_ready=False)
            return FrameResult(view_id, t, "no_reference", detail="modèle de fond immature")

        aligned, alignment = align_to_reference(reference, frame)
        if alignment.method == "rejected":
            self.stats["skipped"] += 1
            return FrameResult(view_id, t, "misaligned", detail="recalage non fiable, cycle ignoré")
        if alignment.magnitude_px > self.cfg.pipeline.vibration_gate_px:
            self.stats["skipped"] += 1
            return FrameResult(
                view_id, t, "misaligned",
                detail=f"vibration {alignment.magnitude_px:.1f} px > seuil",
                alignment_px=alignment.magnitude_px,
            )

        state.frame_shape = aligned.shape[:2]
        # AUDIT P0-06 : le changement global se teste AVANT l'extraction, sur des
        # statistiques globales — le masque adaptatif ne peut pas le voir.
        change = detect_global_change(aligned, reference, self.cfg.candidates)
        if change["is_global_change"]:
            # AUDIT P0-06 : un changement global est un ÉTAT, pas une absence de
            # candidat. On le nomme, on l'expose à la supervision, et surtout on
            # n'apprend pas cette image comme référence.
            self.stats["skipped"] += 1
            self.health.record_frame(
                view_id, image_status="warn", alignment_px=alignment.magnitude_px,
                background_ready=True, note=f"changement global : {change['reason']}",
            )
            return FrameResult(
                view_id, t, "global_change",
                detail=f"{change['reason']} — fond non mis à jour",
                alignment_px=alignment.magnitude_px,
            )

        dmap, gmask, hrow = self._resized_maps(state, aligned.shape[:2])
        blobs = extract_candidates(aligned, reference, self.cfg.candidates, gmask)
        blobs = blobs[: self.cfg.pipeline.max_rois_per_frame]
        self.stats["candidates"] += len(blobs)

        observations = []
        aligned_u8 = np.clip(aligned, 0, 255).astype(np.uint8)
        for blob in blobs:
            x0, y0, x1, y1 = blob.bbox
            # AUDIT P0-04 (corrigé 0.4.0) : la ROI était découpée dans l'image
            # BRUTE alors que la boîte est calculée sur l'image RECALÉE. Dès que
            # la caméra bougeait — c'est-à-dire tout le temps sur un pylône — le
            # classifieur examinait une région décalée du candidat.
            roi = aligned_u8[y0:y1, x0:x1]
            cnn = self.detector.score_roi(roi) if roi.size else 0.0
            observations.append(
                blob_to_observation(
                    t=t,
                    blob=blob,
                    distance_map=dmap,
                    sensor=self.sensor,
                    focal_mm=state.focal_mm,
                    contrast_loss_value=contrast_loss(aligned, reference, blob.bbox),
                    translucency_value=translucency(aligned, reference, blob.mask),
                    cnn_score=cnn,
                    view_azimuth_deg=state.azimuth_deg,
                    pose=state.pose,
                )
            )

        tracks = self.tracker.update(view_id, observations)
        result = FrameResult(
            view_id, t, "ok",
            n_candidates=len(blobs),
            n_tracks=len(tracks),
            alignment_px=alignment.magnitude_px,
        )

        for track in tracks:
            feats = compute_features(
                track,
                horizon_row=self._horizon_at(state, track.last.blob.centroid[0], aligned.shape),
                wind_bearing_deg=self.cfg.wind_bearing_deg,
            )
            score = self.fusion.score(feats)
            track.score = score
            result.track_scores.append(round(score, 4))
            new_state, _reason = decide(
                track.state, score, feats, len(track.observations), self.cfg.decision, self.fusion
            )
            track.state = new_state

            if new_state == "CONFIRMED" and not track.alerted:
                alert = self._emit(track, state, feats, score, timestamp, t, peer_bearing)
                if alert is not None:
                    track.alerted = True
                    track.state = "ALERTED"
                    result.alerts.append(alert)

        # AUDIT P0-05/P0-06/P0-07 (corrigé 0.4.0). Trois défauts cumulés :
        #  - le fond était alimenté avec l'image BRUTE, donc il apprenait la
        #    vibration du mât et accumulait des contours fantômes ;
        #  - un changement global (brouillard, bascule WDR) ne renvoyait aucun
        #    candidat mais était quand même appris comme nouveau fond ;
        #  - seules les pistes CONFIRMED gelaient l'apprentissage, si bien qu'un
        #    panache lent pouvait être absorbé avant d'atteindre le seuil.
        if result.status == "ok" and not any(
            t_.state in ("CANDIDATE", "CONFIRMED", "ALERTED") for t_ in tracks
        ):
            self.background.update(key, aligned)

        self.health.record_frame(
            view_id,
            image_status="ok",
            alignment_px=alignment.magnitude_px,
            background_ready=True,
        )
        # AUDIT P1-08/P1-09 : sans purge, les pistes rejetées et les événements
        # s'accumulaient sans borne — une fuite lente, invisible en test unitaire
        # et fatale sur un fonctionnement de plusieurs mois.
        self.tracker.prune()
        if len(self.events) > self.max_retained_events:
            self.events = self.events[-self.max_retained_events :]
        return result

    def _emit(self, track, state: ViewState, feats, score, timestamp, t, peer_bearing) -> Alert | None:
        # La colonne est ramenée en coordonnées capteur : les images peuvent être
        # sous-échantillonnées (levier de performance n°1 sur carte caméra), et un
        # azimut faux est pire qu'une absence d'azimut pour un CODIS.
        frame_w = state.frame_shape[1] if state.frame_shape else self.sensor.width_px
        if state.pose is not None:
            # Pose étalonnée : elle intègre lacet, assiette, roulis et focale
            # réels, là où pixel_to_bearing suppose un montage parfait.
            scale = state.pose.width_px / max(frame_w, 1)
            bearing = state.pose.unproject(
                track.last.blob.centroid[0] * scale,
                track.last.blob.centroid[1] * (state.pose.height_px / max(state.frame_shape[0], 1)),
            )[0]
        else:
            col_sensor = track.last.blob.centroid[0] * self.sensor.width_px / max(frame_w, 1)
            bearing = pixel_to_bearing(col_sensor, self.sensor, state.focal_mm, state.azimuth_deg)
        peer = None
        if self.cfg.pipeline.use_triangulation and peer_bearing is not None:
            peer = peer_bearing
        lat, lon, mode = localize(
            self.cfg.latitude, self.cfg.longitude, bearing,
            track.last.distance_m if np.isfinite(track.last.distance_m) else None,
            peer,
        )
        alert = Alert(
            alert_id=make_alert_id(self.cfg.site_id, state.view_id, timestamp),
            site_id=self.cfg.site_id,
            view_id=state.view_id,
            timestamp=timestamp.isoformat(),
            bearing_deg=round(bearing, 2),
            score=round(score, 4),
            distance_m=(round(track.last.distance_m) if np.isfinite(track.last.distance_m) else None),
            latitude=lat,
            longitude=lon,
            localization=mode,
            features={**feats.as_dict(), "bbox": list(track.last.blob.bbox)},
            n_visits=len(track.observations),
            model_version=self.model_version,
            pipeline_tier=self.cfg.tier,
        )
        # AUDIT P0-06 : verrou de mode. En `measure`, aucun événement n'est
        # produit ; en `shadow`, il est produit et journalisé mais jamais mis en
        # file ; en `alert`, il faut de surcroît un modèle de fusion calibré.
        if self.mode == "measure":
            self.stats["suppressed_by_mode"] += 1
            return None

        emitted = self.alerts.submit(alert, t)
        if emitted is None:
            return None
        self.stats["alerts"] += 1

        # Un événement canonique est produit systématiquement, puis mis en file.
        # La détection ne dépend jamais de la disponibilité du réseau.
        event = event_from_alert(emitted, software_version=self.model_version)
        if emitted.distance_m:
            event.uncertainty = bearing_uncertainty(
                float(emitted.distance_m), bearing_sigma_deg=self.bearing_sigma_deg
            ).as_dict()
        event.transition("confirmed", reason="seuil franchi avec persistance suffisante")
        event.features["operating_mode"] = self.mode
        if not self.fusion.fitted:
            event.features["fusion_calibrated"] = False
        self.events.append(event)

        if self.mode == "alert" and self.can_transmit and self.outbox is not None:
            if self.outbox.enqueue(event):
                self.stats["queued"] += 1
        else:
            self.stats["not_transmitted"] += 1
        return emitted

    def _horizon_at(self, state: ViewState, col: float, shape: tuple[int, ...]) -> int:
        """Ligne d'horizon à une colonne donnée.

        Avec un MNT, l'horizon est une ligne de crête, pas une horizontale : le
        test d'origine au sol devient nettement plus discriminant en relief.
        """
        _, _, hrow = self._resized_maps(state, shape[:2])
        if state.horizon_rows is None or len(state.horizon_rows) == 0:
            return hrow
        idx = int(round(col * (len(state.horizon_rows) - 1) / max(shape[1] - 1, 1)))
        idx = max(0, min(len(state.horizon_rows) - 1, idx))
        value = int(state.horizon_rows[idx])
        if value < 0:
            return hrow
        scale = shape[0] / max(state.distance_map.shape[0], 1)
        return int(round(value * scale)) if state.distance_map.shape[0] != shape[0] else value

    @property
    def can_transmit(self) -> bool:
        """Le site est-il autorisé à transmettre une alerte ?

        AUDIT P0-06 : la phase de mesure recommandée par la documentation est
        désormais un verrou. Un modèle de fusion aux poids provisoires ne peut
        pas alerter sans dérogation explicite, tracée dans le résumé.
        """
        if self.mode != "alert":
            return False
        if self.fusion.fitted:
            return True
        return self.cfg.operating.allow_uncalibrated_alerts

    def transmission_blocker(self) -> str | None:
        """Motif lisible du blocage, ou ``None`` si la transmission est permise."""
        if self.mode == "measure":
            return "mode 'measure' : détection exécutée, aucun événement produit"
        if self.mode == "shadow":
            return "mode 'shadow' : événements journalisés localement, non transmis"
        if not self.fusion.fitted and not self.cfg.operating.allow_uncalibrated_alerts:
            return (
                "modèle de fusion non calibré sur les négatifs du site : "
                "transmission refusée (operating.allow_uncalibrated_alerts pour déroger)"
            )
        return None

    def apply_calibration(self, view_id: str, result) -> None:
        """Applique un étalonnage à une vue : pose et incertitude d'azimut."""
        if view_id not in self.views:
            raise KeyError(f"vue non enregistrée : '{view_id}'")
        if result.quality == "insufficient":
            raise ValueError("étalonnage insuffisant : ne pas l'appliquer")
        self.views[view_id].pose = result.pose
        self.views[view_id].azimuth_deg = result.pose.yaw_deg
        self.bearing_sigma_deg = result.bearing_sigma_deg

    def flush(self) -> dict:
        """Vide la file d'attente vers le transport, si les deux sont présents."""
        if self.outbox is None or self.transport is None:
            return {"sent": 0, "retried": 0, "dead_lettered": 0, "remaining": 0}
        return self.outbox.flush(self.transport)

    def heartbeat(self) -> dict | None:
        """Émet un battement de cœur si l'intervalle est écoulé."""
        if self.transport is None:
            return None
        return self.health.beat(
            self.transport,
            software_version=self.model_version,
            pipeline_tier=self.cfg.tier,
            detector_backend=self.detector.name,
            degraded_reason=self.degraded_reason,
            outbox=self.outbox.stats() if self.outbox else {},
        )

    def summary(self) -> dict:
        return {
            **self.stats,
            "tier": self.cfg.tier,
            "detector": self.detector.name,
            "views": sorted(self.views),
            "alert_store": self.alerts.stats(),
            "degraded": self.degraded_reason,
            "events": len(self.events),
            "outbox": self.outbox.stats() if self.outbox else None,
            "health": self.health.snapshot().status,
            "geolocation": "dem" if any(v.from_dem for v in self.views.values()) else "flat_earth",
            "mode": self.mode,
            "can_transmit": self.can_transmit,
            "transmission_blocker": self.transmission_blocker(),
            "fusion_calibrated": self.fusion.fitted,
            "masked_views": sorted(self._masks),
            "bearing_sigma_deg": round(self.bearing_sigma_deg, 4),
            "calibrated_views": sorted(k for k, v in self.views.items() if v.pose is not None),
        }
