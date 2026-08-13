"""Corrélation multi-tours.

C'est le développement au meilleur rendement après le détecteur : le saut d'une
tour à deux tours apporte la triangulation, une confirmation indépendante et de
la redondance de couverture. Le saut de deux tours à vingt n'apporte
essentiellement que de la surface.

Trois fonctions :

  1. **déduplication** — deux tours qui voient le même feu ne doivent produire
     qu'un événement, pas deux alertes concurrentes dans un centre de secours ;
  2. **triangulation** — l'intersection de deux relèvements réduit l'ellipse
     d'incertitude d'un ordre de grandeur par rapport à un relèvement seul ;
  3. **sollicitation de confirmation** — quand la tour A détecte, les tours qui
     voient la zone estimée n'attendent pas leur cycle : elles y pointent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .events import (
    CONFIRMED,
    DetectionEvent,
    Uncertainty,
    bearing_uncertainty,
    triangulation_uncertainty,
)
from .geometry import angular_diff, bearing_range_to_latlon, triangulate


@dataclass
class Tower:
    """Une tour du réseau."""

    site_id: str
    latitude: float
    longitude: float
    height_m: float = 40.0
    max_range_m: float = 12_000.0
    has_ptz: bool = False
    sector_ranges: dict[float, float] = field(default_factory=dict)

    def distance_to(self, lat: float, lon: float) -> float:
        """Distance approximative en mètres (ENU local)."""
        dn = (lat - self.latitude) * 111_132.0
        de = (lon - self.longitude) * 111_320.0 * math.cos(math.radians(self.latitude))
        return math.hypot(dn, de)

    def bearing_to(self, lat: float, lon: float) -> float:
        dn = (lat - self.latitude) * 111_132.0
        de = (lon - self.longitude) * 111_320.0 * math.cos(math.radians(self.latitude))
        return math.degrees(math.atan2(de, dn)) % 360.0

    def range_in_direction(self, bearing_deg: float) -> float:
        """Portée utile dans une direction, d'après le viewshed si disponible."""
        if not self.sector_ranges:
            return self.max_range_m
        nearest = min(self.sector_ranges, key=lambda a: abs(angular_diff(a, bearing_deg)))
        return self.sector_ranges[nearest]

    def can_see(self, lat: float, lon: float, margin: float = 1.0) -> bool:
        """La tour peut-elle voir ce point, compte tenu du relief ?"""
        d = self.distance_to(lat, lon)
        return d <= self.range_in_direction(self.bearing_to(lat, lon)) * margin


@dataclass
class ConfirmationTask:
    """Demande faite à une tour de pointer vers une zone suspecte."""

    site_id: str
    bearing_deg: float
    distance_m: float
    priority: float
    reason: str
    source_event_id: str

    def as_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "bearing_deg": round(self.bearing_deg, 2),
            "distance_m": round(self.distance_m),
            "priority": round(self.priority, 3),
            "reason": self.reason,
            "source_event_id": self.source_event_id,
        }


@dataclass
class Cluster:
    """Groupe d'événements attribués au même incident."""

    cluster_id: str
    events: list[DetectionEvent] = field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    uncertainty: Uncertainty | None = None
    method: str = "single"

    @property
    def towers(self) -> list[str]:
        return sorted({e.site_id for e in self.events})

    @property
    def n_towers(self) -> int:
        return len(self.towers)

    @property
    def best_score(self) -> float:
        return max((e.fused_score for e in self.events), default=0.0)

    @property
    def confidence(self) -> float:
        """Confiance du groupe.

        Deux relèvements indépendants qui se croisent au même point sont un
        signal bien plus fort que deux scores élevés sur la même tour : la
        corroboration géométrique compte davantage que le score du réseau.
        """
        base = self.best_score
        if self.n_towers >= 2:
            base = 1.0 - (1.0 - base) * 0.35
        if any(e.ptz_confirmed for e in self.events):
            base = 1.0 - (1.0 - base) * 0.6
        return float(min(base, 0.999))

    def as_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "n_events": len(self.events),
            "towers": self.towers,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "method": self.method,
            "confidence": round(self.confidence, 4),
            "uncertainty": self.uncertainty.as_dict() if self.uncertainty else None,
            "event_ids": [e.event_id for e in self.events],
        }


def _parse_ts(value: str) -> float:
    import datetime as _dt

    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


class MultiTowerCorrelator:
    """Regroupe et localise les événements d'un réseau de tours."""

    def __init__(
        self,
        towers: dict[str, Tower],
        time_window_s: float = 600.0,
        same_tower_bearing_deg: float = 4.0,
        cross_tower_distance_m: float = 1_500.0,
        bearing_sigma_deg: float = 0.5,
    ) -> None:
        self.towers = towers
        self.time_window_s = time_window_s
        self.same_tower_bearing_deg = same_tower_bearing_deg
        self.cross_tower_distance_m = cross_tower_distance_m
        self.bearing_sigma_deg = bearing_sigma_deg

    # -- localisation ------------------------------------------------------- #
    def _estimate_position(self, event: DetectionEvent) -> tuple[float, float] | None:
        if event.latitude is not None and event.longitude is not None:
            return (event.latitude, event.longitude)
        tower = self.towers.get(event.site_id)
        if tower is None:
            return None
        d = event.distance_m or tower.max_range_m * 0.5
        return bearing_range_to_latlon(tower.latitude, tower.longitude, event.bearing_deg, d)

    def _same_incident(self, a: DetectionEvent, b: DetectionEvent) -> bool:
        if abs(_parse_ts(a.detected_at) - _parse_ts(b.detected_at)) > self.time_window_s:
            return False
        if a.site_id == b.site_id:
            return abs(angular_diff(a.bearing_deg, b.bearing_deg)) <= self.same_tower_bearing_deg
        pa, pb = self._estimate_position(a), self._estimate_position(b)
        if pa is None or pb is None:
            return False
        ta = self.towers.get(a.site_id)
        if ta is None:
            return False
        return ta.distance_to(*pb) > 0 and _haversine(pa, pb) <= self.cross_tower_distance_m

    def cluster(self, events: list[DetectionEvent]) -> list[Cluster]:
        """Regroupe les événements par incident, puis localise chaque groupe."""
        remaining = sorted(events, key=lambda e: _parse_ts(e.detected_at))
        clusters: list[Cluster] = []
        for event in remaining:
            placed = False
            for cl in clusters:
                if any(self._same_incident(event, other) for other in cl.events):
                    cl.events.append(event)
                    placed = True
                    break
            if not placed:
                clusters.append(Cluster(cluster_id=event.event_id, events=[event]))
        for cl in clusters:
            self._localize(cl)
        return clusters

    def _localize(self, cluster: Cluster) -> None:
        """Localise un groupe, par triangulation si possible."""
        by_tower: dict[str, DetectionEvent] = {}
        for e in cluster.events:
            best = by_tower.get(e.site_id)
            if best is None or e.fused_score > best.fused_score:
                by_tower[e.site_id] = e

        if len(by_tower) >= 2:
            best_pair = None
            for i, (sa, ea) in enumerate(by_tower.items()):
                for sb, eb in list(by_tower.items())[i + 1 :]:
                    ta, tb = self.towers.get(sa), self.towers.get(sb)
                    if ta is None or tb is None:
                        continue
                    result = triangulate(
                        ta.latitude, ta.longitude, ea.bearing_deg,
                        tb.latitude, tb.longitude, eb.bearing_deg,
                    )
                    if result is None:
                        continue
                    crossing = abs(angular_diff(ea.bearing_deg, eb.bearing_deg))
                    crossing = min(crossing, 180 - crossing)
                    # On retient la paire dont les relèvements se croisent le plus
                    # franchement : une intersection rasante donne une position
                    # instable même si elle est mathématiquement définie.
                    if best_pair is None or crossing > best_pair[0]:
                        best_pair = (crossing, result, ta, tb)
            if best_pair is not None and best_pair[0] >= 5.0:
                crossing, (lat, lon), ta, tb = best_pair
                cluster.latitude, cluster.longitude = lat, lon
                cluster.method = "triangulated"
                cluster.uncertainty = triangulation_uncertainty(
                    ta.distance_to(lat, lon), tb.distance_to(lat, lon),
                    crossing, self.bearing_sigma_deg,
                )
                for e in cluster.events:
                    e.tower_votes = cluster.towers
                return

        primary = max(cluster.events, key=lambda e: e.fused_score)
        pos = self._estimate_position(primary)
        if pos is not None:
            cluster.latitude, cluster.longitude = pos
            cluster.method = primary.localization_method
            if primary.distance_m:
                cluster.uncertainty = bearing_uncertainty(
                    float(primary.distance_m), self.bearing_sigma_deg
                )
        for e in cluster.events:
            e.tower_votes = cluster.towers

    # -- sollicitation de confirmation -------------------------------------- #
    def confirmation_tasks(
        self, event: DetectionEvent, exclude_source: bool = True, min_score: float = 0.5
    ) -> list[ConfirmationTask]:
        """Tours à solliciter pour confirmer un candidat.

        Chaque tour capable de voir la zone estimée reçoit un azimut et une
        distance. Elle interrompt sa ronde pour y pointer, ce qui vaut bien mieux
        qu'attendre qu'elle y arrive d'elle-même : la fenêtre utile d'un départ
        de feu se compte en minutes.
        """
        if event.fused_score < min_score:
            return []
        pos = self._estimate_position(event)
        if pos is None:
            return []
        lat, lon = pos
        tasks: list[ConfirmationTask] = []
        for site_id, tower in self.towers.items():
            if exclude_source and site_id == event.site_id:
                continue
            if not tower.can_see(lat, lon):
                continue
            d = tower.distance_to(lat, lon)
            # Une tour proche voit mieux ; un croisement franc localise mieux.
            source = self.towers.get(event.site_id)
            crossing = 90.0
            if source is not None:
                crossing = abs(angular_diff(tower.bearing_to(lat, lon), event.bearing_deg))
                crossing = min(crossing, 180 - crossing)
            priority = event.fused_score * (1.0 - d / max(tower.max_range_m, 1.0)) * (
                0.5 + 0.5 * math.sin(math.radians(min(crossing, 90)))
            )
            tasks.append(
                ConfirmationTask(
                    site_id=site_id,
                    bearing_deg=tower.bearing_to(lat, lon),
                    distance_m=d,
                    priority=max(priority, 0.0),
                    reason=f"confirmation du candidat {event.site_id}",
                    source_event_id=event.event_id,
                )
            )
        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    def promote(self, cluster: Cluster, min_towers: int = 2) -> DetectionEvent | None:
        """Fusionne un groupe en un événement unique, prêt pour l'opérateur.

        C'est ce qui évite deux alertes concurrentes pour un même feu.
        """
        if not cluster.events:
            return None
        primary = max(cluster.events, key=lambda e: e.fused_score)
        primary.latitude = cluster.latitude
        primary.longitude = cluster.longitude
        primary.localization_method = cluster.method
        primary.uncertainty = cluster.uncertainty.as_dict() if cluster.uncertainty else None
        primary.tower_votes = cluster.towers
        primary.fused_score = cluster.confidence
        if cluster.n_towers >= min_towers and primary.state != CONFIRMED:
            try:
                primary.transition(
                    CONFIRMED, reason=f"corroboré par {cluster.n_towers} tours"
                )
            except Exception:
                pass
        return primary


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance entre deux positions (lat, lon), en mètres."""
    lat1, lon1 = a
    lat2, lon2 = b
    dn = (lat2 - lat1) * 111_132.0
    de = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dn, de)
