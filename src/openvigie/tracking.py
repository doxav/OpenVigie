"""Suivi des candidats et extraction des features physiques.

Point central du projet : toutes les features sont exprimées en **unités
réelles** (m², m²/s, m/s) via la carte de distance au sol, jamais en pixels.
Un seuil « croissance > 15 m²/s » est transférable d'un site landais à un site
alpin ; un seuil « croissance > 40 px/s » ne l'est pas, et c'est la raison
principale pour laquelle les systèmes doivent habituellement être re-réglés à
la main sur chaque tour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .candidates import Blob
from .geometry import SensorSpec, ground_sample_m, pixel_to_bearing


@dataclass
class Observation:
    """Une observation datée d'un candidat.

    AUDIT P1-06 : ``area_m2`` est la surface **apparente** — le nombre de pixels
    multiplié par la taille au sol d'un pixel à la distance estimée. Ce n'est pas
    la surface physique du volume de fumée, qui est tridimensionnelle et dont la
    projection dépend de l'angle de vue. C'est un indicateur de croissance
    exploitable et comparable d'un site à l'autre, pas une mesure de panache.
    """

    t: float                 # secondes (monotone)
    blob: Blob
    area_m2: float          # surface APPARENTE projetée au sol (voir note ci-dessous)
    centroid_y_m: float      # hauteur apparente du centroïde, en m au sol équivalent
    distance_m: float
    contrast_loss: float
    translucency: float
    cnn_score: float = 0.0
    azimuth_deg: float | None = None   # azimut absolu du centroïde


@dataclass
class TrackFeatures:
    """Vecteur de features soumis à la fusion. Toutes bornées dans [0, 1] sauf mention."""

    persistence: float = 0.0        # nb de revisites, normalisé
    growth_m2_s: float = 0.0        # brut, m²/s
    growth_score: float = 0.0
    upward_m_s: float = 0.0         # brut, m/s
    upward_score: float = 0.0
    ground_origin: float = 0.0
    contrast_loss: float = 0.0
    translucency: float = 0.0
    wind_coherence: float = 0.0
    cnn_score: float = 0.0
    area_m2: float = 0.0
    distance_m: float = 0.0
    bearing_deg: float = 0.0

    def as_dict(self) -> dict:
        return {k: round(float(v), 4) for k, v in self.__dict__.items()}


@dataclass
class Track:
    """Cycle de vie d'un candidat : NEW -> CANDIDATE -> CONFIRMED -> ALERTED / DISMISSED."""

    track_id: int
    view_id: str
    observations: list[Observation] = field(default_factory=list)
    state: str = "NEW"
    score: float = 0.0
    misses: int = 0
    alerted: bool = False

    @property
    def last(self) -> Observation:
        return self.observations[-1]

    @property
    def age_s(self) -> float:
        if len(self.observations) < 2:
            return 0.0
        return self.observations[-1].t - self.observations[0].t

    def add(self, obs: Observation) -> None:
        self.observations.append(obs)
        self.misses = 0


def _linear_slope(ts: np.ndarray, ys: np.ndarray) -> float:
    """Pente d'une régression linéaire simple, robuste aux séries courtes."""
    if len(ts) < 2:
        return 0.0
    span = ts.max() - ts.min()
    if span < 1e-6:
        return 0.0
    t = ts - ts.mean()
    y = ys - ys.mean()
    denom = float((t * t).sum())
    return 0.0 if denom < 1e-12 else float((t * y).sum() / denom)


def _saturate(x: float, scale: float) -> float:
    """Compression douce vers [0, 1] : x/(x+scale). Pas de seuil dur à régler."""
    if x <= 0:
        return 0.0
    return float(x / (x + scale))


def compute_features(
    track: Track,
    *,
    horizon_row: int,
    wind_bearing_deg: float | None = None,
    growth_scale_m2_s: float = 10.0,
    upward_scale_m_s: float = 1.0,
    persistence_target: int = 4,
) -> TrackFeatures:
    """Calcule le vecteur de features d'une piste.

    ``wind_bearing_deg`` est la direction *vers laquelle* le vent souffle
    (convention météo inversée), typiquement issue d'AROME ou d'un anémomètre
    du site. Un panache qui dérive à contresens du vent n'est pas un panache.
    """
    obs = track.observations
    f = TrackFeatures()
    if not obs:
        return f

    last = obs[-1]
    ts = np.array([o.t for o in obs], dtype=np.float64)
    areas = np.array([o.area_m2 for o in obs], dtype=np.float64)
    tops = np.array([o.blob.bbox[1] for o in obs], dtype=np.float64)  # ligne du sommet

    f.persistence = min(1.0, len(obs) / max(1, persistence_target))
    f.growth_m2_s = _linear_slope(ts, areas)
    f.growth_score = _saturate(f.growth_m2_s, growth_scale_m2_s)

    # Le sommet du panache monte => la ligne image du sommet diminue.
    px_per_s = -_linear_slope(ts, tops)
    gsd = ground_sample_m_safe(last)
    f.upward_m_s = px_per_s * gsd
    f.upward_score = _saturate(f.upward_m_s, upward_scale_m_s)

    # Origine au sol : la base du candidat est sous l'horizon.
    f.ground_origin = 1.0 if last.blob.bottom_row > horizon_row else 0.0

    f.contrast_loss = float(np.mean([o.contrast_loss for o in obs[-3:]]))
    f.translucency = float(np.mean([o.translucency for o in obs[-3:]]))
    f.cnn_score = float(max(o.cnn_score for o in obs))
    f.area_m2 = float(last.area_m2)
    f.distance_m = float(last.distance_m)

    f.wind_coherence = _wind_coherence(obs, ts, wind_bearing_deg)
    return f


def _wind_coherence(obs, ts: np.ndarray, wind_bearing_deg: float | None) -> float:
    """Cohérence entre la dérive observée et le vent, en repère géographique.

    AUDIT P0-11 (corrigé 0.4.0). La version précédente comparait un déplacement
    exprimé en **coordonnées image** (colonnes/lignes) à un azimut **absolu** :
    la même fumée était déclarée cohérente ou incohérente selon l'orientation de
    la caméra. C'était faux pour presque toutes les vues.

    On ne mesure ici que la composante **tangentielle** — la dérive en azimut —
    parce que c'est la seule que l'image donne de façon fiable : la composante
    radiale se déduirait d'une variation de distance, or la distance médiane du
    masque augmente mécaniquement quand le panache s'élève, ce qui produirait un
    éloignement fictif.

    Conséquence assumée : quand le vent souffle le long de la ligne de visée, le
    test ne porte aucune information et renvoie 0,5 — une valeur neutre, pas une
    confirmation. Mieux vaut un critère qui se déclare muet qu'un critère qui
    invente.
    """
    if wind_bearing_deg is None or len(obs) < 3:
        return 0.0
    azimuths = [o.azimuth_deg for o in obs]
    if any(a is None for a in azimuths):
        return 0.0
    az = np.degrees(np.unwrap(np.radians(np.asarray(azimuths, dtype=float))))
    distances = np.array([o.distance_m for o in obs], dtype=float)
    if not np.all(np.isfinite(distances)) or np.any(distances <= 0):
        return 0.0

    az_rate_deg_s = _linear_slope(ts, az)
    tangential_ms = math.radians(az_rate_deg_s) * float(np.mean(distances))
    mean_az = float(np.mean(az)) % 360.0

    # Composante tangentielle attendue du vent, normalisée dans [-1, 1].
    expected = math.sin(math.radians(wind_bearing_deg - mean_az))
    observed = math.tanh(tangential_ms / 2.0)
    return float(np.clip(0.5 + 0.5 * expected * observed, 0.0, 1.0))


def ground_sample_m_safe(obs: Observation) -> float:
    """Taille au sol d'un pixel à la distance de l'observation.

    Repli prudent si la distance est infinie (candidat au-dessus de l'horizon) :
    on renvoie 0 pour ne pas fabriquer une vitesse verticale absurde.
    """
    d = obs.distance_m
    if not math.isfinite(d) or d <= 0 or not obs.blob.area_px:
        return 0.0
    return math.sqrt(obs.area_m2 / obs.blob.area_px)


class Tracker:
    """Association gloutonne par IoU, avec tolérance aux absences (occlusion, cycle sauté)."""

    def __init__(self, iou_threshold: float = 0.15, max_misses: int = 2) -> None:
        self.iou_threshold = iou_threshold
        self.max_misses = max_misses
        self.tracks: list[Track] = []
        self._next_id = 1

    def update(self, view_id: str, observations: list[Observation]) -> list[Track]:
        active = [t for t in self.tracks if t.view_id == view_id and t.state != "DISMISSED"]
        unmatched = list(observations)

        pairs: list[tuple[float, Track, Observation]] = []
        for t in active:
            for o in unmatched:
                iou = t.last.blob.iou(o.blob)
                if iou >= self.iou_threshold:
                    pairs.append((iou, t, o))
        pairs.sort(key=lambda p: p[0], reverse=True)

        used_tracks: set[int] = set()
        used_obs: set[int] = set()
        for _iou, t, o in pairs:
            if id(t) in used_tracks or id(o) in used_obs:
                continue
            t.add(o)
            used_tracks.add(id(t))
            used_obs.add(id(o))

        for t in active:
            if id(t) not in used_tracks:
                t.misses += 1
                if t.misses > self.max_misses:
                    t.state = "DISMISSED"

        for o in unmatched:
            if id(o) in used_obs:
                continue
            track = Track(track_id=self._next_id, view_id=view_id)
            self._next_id += 1
            track.add(o)
            self.tracks.append(track)

        return [t for t in self.tracks if t.view_id == view_id and t.state != "DISMISSED"]

    def prune(self) -> None:
        self.tracks = [t for t in self.tracks if t.state != "DISMISSED"]


def blob_to_observation(
    t: float,
    blob: Blob,
    distance_map: np.ndarray,
    sensor: SensorSpec,
    focal_mm: float,
    contrast_loss_value: float,
    translucency_value: float,
    cnn_score: float = 0.0,
    view_azimuth_deg: float | None = None,
    pose=None,
) -> Observation:
    """Convertit un blob en observation physique via la carte de distance."""
    dists = distance_map[blob.mask] if blob.mask is not None else np.array([np.inf])
    finite = dists[np.isfinite(dists)]
    distance_m = float(np.median(finite)) if finite.size else float("inf")
    if math.isfinite(distance_m):
        gsd = ground_sample_m(sensor, focal_mm, distance_m)
        area_m2 = blob.area_px * gsd * gsd
    else:
        area_m2 = 0.0
    azimuth = None
    if pose is not None:
        azimuth = pose.unproject(*blob.centroid)[0]
    elif view_azimuth_deg is not None:
        azimuth = pixel_to_bearing(blob.centroid[0], sensor, focal_mm, view_azimuth_deg)

    return Observation(
        t=t,
        blob=blob,
        area_m2=area_m2,
        azimuth_deg=azimuth,
        centroid_y_m=0.0,
        distance_m=distance_m,
        contrast_loss=contrast_loss_value,
        translucency=translucency_value,
        cnn_score=cnn_score,
    )


def track_bearing(track: Track, sensor: SensorSpec, focal_mm: float, view_azimuth_deg: float) -> float:
    """Azimut absolu du candidat — la seule information que le CODIS exploite vraiment."""
    return pixel_to_bearing(track.last.blob.centroid[0], sensor, focal_mm, view_azimuth_deg)
