"""Production, déduplication et export des alertes.

L'alerte utile pour un CODIS n'est pas « il y a de la fumée » : c'est un azimut,
une distance estimée, une position, une vignette et une séquence. La
déduplication est aussi importante que la détection — un feu confirmé qui
réalerte toutes les deux minutes pendant six heures détruit la confiance aussi
sûrement qu'un faux positif.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .geometry import angular_diff, bearing_range_to_latlon, triangulate


@dataclass
class Alert:
    """Une alerte, telle qu'elle part vers l'opérateur."""

    alert_id: str
    site_id: str
    view_id: str
    timestamp: str
    bearing_deg: float
    score: float
    distance_m: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    localization: str = "bearing_only"  # bearing_only | dem_intersect | triangulated
    features: dict = field(default_factory=dict)
    n_visits: int = 0
    image_path: str | None = None
    sequence_paths: list[str] = field(default_factory=list)
    model_version: str = "unknown"
    pipeline_tier: str = "unknown"

    def as_dict(self) -> dict:
        return asdict(self)

    def as_pyro_payload(self) -> dict:
        """Format proche de pyro-api (détection groupée en séquence).

        Facilite l'interopérabilité avec la plateforme Pyronear plutôt que
        d'inventer un énième schéma d'alerte.
        """
        return {
            "camera_id": f"{self.site_id}:{self.view_id}",
            "azimuth": round(self.bearing_deg, 2),
            "created_at": self.timestamp,
            "bboxes": self.features.get("bbox", []),
            "confidence": round(self.score, 4),
            "localization": self.localization,
            "lat": self.latitude,
            "lon": self.longitude,
        }


def localize(
    site_lat: float,
    site_lon: float,
    bearing_deg: float,
    distance_m: float | None,
    peer: tuple[float, float, float] | None = None,
) -> tuple[float | None, float | None, str]:
    """Localise une alerte, du meilleur au moins bon.

    1. triangulation avec une seconde tour (``peer`` = lat, lon, azimut) ;
    2. intersection du relèvement avec le MNT (distance estimée) ;
    3. relèvement seul.
    """
    if peer is not None:
        result = triangulate(site_lat, site_lon, bearing_deg, peer[0], peer[1], peer[2])
        if result is not None:
            return result[0], result[1], "triangulated"
    if distance_m is not None and math.isfinite(distance_m) and distance_m > 0:
        lat, lon = bearing_range_to_latlon(site_lat, site_lon, bearing_deg, distance_m)
        return lat, lon, "dem_intersect"
    return None, None, "bearing_only"


class AlertStore:
    """Déduplication spatio-temporelle et journalisation.

    Un candidat dans le même secteur angulaire que la dernière alerte, et dans
    la fenêtre de silence, n'est pas réémis. La journalisation intégrale
    (features + version de modèle) n'est pas un luxe : c'est ce qui permet
    d'auditer une alerte manquée six mois plus tard, et c'est aussi ce
    qu'exigera toute démarche de conformité côté déployeur.
    """

    def __init__(
        self,
        bearing_tolerance_deg: float = 3.0,
        silence_window_s: float = 1800.0,
        log_path: str | Path | None = None,
    ) -> None:
        self.bearing_tolerance_deg = bearing_tolerance_deg
        self.silence_window_s = silence_window_s
        self.log_path = Path(log_path) if log_path else None
        self._recent: list[tuple[float, float, str]] = []  # (t, bearing, view_id)
        self.emitted: list[Alert] = []
        self.suppressed: int = 0

    def is_duplicate(self, t: float, bearing_deg: float, view_id: str) -> bool:
        self._recent = [r for r in self._recent if t - r[0] <= self.silence_window_s]
        for _, prev_bearing, prev_view in self._recent:
            if prev_view == view_id and abs(angular_diff(bearing_deg, prev_bearing)) <= self.bearing_tolerance_deg:
                return True
        return False

    def submit(self, alert: Alert, t: float) -> Alert | None:
        """Enregistre l'alerte, ou ``None`` si elle est supprimée en doublon."""
        if self.is_duplicate(t, alert.bearing_deg, alert.view_id):
            self.suppressed += 1
            return None
        self._recent.append((t, alert.bearing_deg, alert.view_id))
        self.emitted.append(alert)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(alert.as_dict(), ensure_ascii=False) + "\n")
        return alert

    def stats(self, observation_days: float = 1.0) -> dict:
        return {
            "emitted": len(self.emitted),
            "suppressed_duplicates": self.suppressed,
            "alerts_per_day": round(len(self.emitted) / max(observation_days, 1e-9), 3),
        }


def make_alert_id(site_id: str, view_id: str, ts: dt.datetime) -> str:
    """Identifiant d'alerte, unique par construction.

    AUDIT P0-17 (corrigé 0.4.0) : l'identifiant était horodaté à la seconde, si
    bien que deux alertes émises dans la même seconde sur la même vue — cas
    banal au démarrage d'un feu, ou lors d'un rejeu — portaient le même
    identifiant et se confondaient en aval.
    """
    import uuid as _uuid

    return f"{site_id}-{view_id}-{ts.strftime('%Y%m%dT%H%M%S')}-{_uuid.uuid4().hex[:8]}"


class WebhookSink:  # pragma: no cover - I/O réseau
    """Envoi HTTP POST des alertes vers un SGO/CTA ou une plateforme."""

    def __init__(self, url: str, timeout_s: float = 5.0, payload: str = "native") -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("WebhookSink: `pip install requests`") from exc
        self._requests = requests
        self.url = url
        self.timeout_s = timeout_s
        self.payload = payload

    def send(self, alert: Alert) -> bool:
        body = alert.as_pyro_payload() if self.payload == "pyro" else alert.as_dict()
        try:
            r = self._requests.post(self.url, json=body, timeout=self.timeout_s)
            return 200 <= r.status_code < 300
        except Exception:
            return False
