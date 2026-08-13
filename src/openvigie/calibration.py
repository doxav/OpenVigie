"""Étalonnage géométrique par trafic aérien (ADS-B).

## Pourquoi ça marche

Une caméra de guet regarde l'horizon. Un avion en croisière à 10 700 m se
trouve, vu depuis une tour, entre **19° au-dessus de l'horizon à 30 km et 2° à
200 km** — c'est-à-dire exactement dans la partie haute de l'image. Et il
diffuse en clair sa position tridimensionnelle, horodatée.

Chaque avion vu est donc une **mire de calibration gratuite, à position connue**.
Quelques dizaines de passages suffisent à résoudre l'orientation de la caméra à
mieux que 0,02°, là où une boussole sur un pylône treillis donne ±2°.

## Ce que ça apporte réellement

Deux effets, l'un beaucoup plus important que l'autre :

1. **Le tilt (l'assiette), et donc la portée estimée.** C'est le gain majeur, et
   il est contre-intuitif. Pour une caméra dominant son terrain de 100 m, une
   erreur de tilt de 0,5° produit **44 % d'erreur de distance à 5 km** ; ramenée
   à 0,05°, elle tombe à 4 %. Comme l'ellipse d'incertitude d'une tour seule est
   dominée par le terme de distance, c'est là que se joue l'essentiel.

2. **L'azimut, et donc la triangulation.** L'ellipse triangulée est
   *entièrement* proportionnelle à l'incertitude d'azimut : passer de 2° à 0,05°
   la divise par quarante.

Et un troisième, moins spectaculaire mais peut-être le plus utile en
exploitation : **la détection de dérive**. Un preset qui a glissé, un bras qui a
plié, un technicien qui a heurté la platine — l'étalonnage tourne en continu et
le voit, au lieu qu'on s'en aperçoive sur une alerte mal localisée.

## Les trois pièges, tous traités ici

- **Le temps.** Un avion à 50 km défile à 0,29°/s : *une seconde d'erreur
  d'horloge vaut une erreur d'azimut de 0,29°*, soit autant que ce qu'on cherche
  à corriger. Le décalage d'horloge est donc estimé comme paramètre, ce qui
  transforme le piège en mesure — mais seulement si les avions observés volent
  dans des directions variées (voir ``identifiability``).
- **L'altitude barométrique.** Elle est référencée à 1013,25 hPa et peut
  s'écarter de plusieurs centaines de mètres de l'altitude vraie ; 300 m d'erreur
  à 50 km, c'est 0,33° d'élévation. On privilégie l'altitude GNSS, et à défaut on
  estime un biais d'altitude commun.
- **La sphéricité.** À 100 km, la convergence des méridiens fausse un azimut
  calculé en plan de près de 0,4°. Les azimuts sont donc calculés en
  orthodromie, pas en ENU plat.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .geometry import EARTH_RADIUS_M, SensorSpec, angular_diff

# Même rayon effectif que le module MNT : la réfraction troposphérique standard
# est modélisée par un rayon terrestre majoré de 7/6. Garder le même modèle
# partout évite qu'un étalonnage compense un biais introduit ailleurs.
EFFECTIVE_EARTH_RADIUS_M = EARTH_RADIUS_M * 7.0 / 6.0

PARAM_NAMES = ("yaw_deg", "pitch_deg", "roll_deg", "focal_mm", "clock_offset_s", "altitude_offset_m")


# --------------------------------------------------------------------------- #
# Géodésie
# --------------------------------------------------------------------------- #
def great_circle(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Distance (m) et azimut initial (deg) d'un point à un autre, sur la sphère.

    Le calcul en plan tangent, suffisant à 10 km, produit à 100 km une erreur
    d'azimut de l'ordre de 0,4° — soit vingt fois ce que l'étalonnage cherche à
    atteindre. L'orthodromie est donc obligatoire dès qu'on observe des avions.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    sin_p1, cos_p1 = math.sin(p1), math.cos(p1)
    sin_p2, cos_p2 = math.sin(p2), math.cos(p2)

    central = math.acos(max(-1.0, min(1.0, sin_p1 * sin_p2 + cos_p1 * cos_p2 * math.cos(dl))))
    distance = central * EARTH_RADIUS_M
    bearing = math.degrees(
        math.atan2(math.sin(dl) * cos_p2, cos_p1 * sin_p2 - sin_p1 * cos_p2 * math.cos(dl))
    ) % 360.0
    return distance, bearing


def apparent_elevation_deg(
    target_alt_m: float, camera_alt_m: float, ground_distance_m: float
) -> float:
    """Élévation apparente d'un point élevé, courbure et réfraction comprises."""
    if ground_distance_m <= 0:
        return 90.0 if target_alt_m > camera_alt_m else -90.0
    drop = ground_distance_m**2 / (2.0 * EFFECTIVE_EARTH_RADIUS_M)
    return math.degrees(math.atan2(target_alt_m - drop - camera_alt_m, ground_distance_m))


# --------------------------------------------------------------------------- #
# Modèle de caméra
# --------------------------------------------------------------------------- #
@dataclass
class CameraPose:
    """Pose et focale d'une vue.

    ``pitch_deg`` est positif vers le haut ; la configuration du site exprime au
    contraire un ``tilt_deg`` positif vers le bas. ``from_tilt`` fait la
    conversion pour éviter l'erreur de signe, qui est silencieuse et coûteuse.
    """

    yaw_deg: float
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    focal_mm: float = 5.2
    sensor: SensorSpec | None = None
    width_px: int = 0
    height_px: int = 0

    def __post_init__(self) -> None:
        if self.sensor is not None:
            self.width_px = self.width_px or self.sensor.width_px
            self.height_px = self.height_px or self.sensor.height_px
        if self.width_px < 1 or self.height_px < 1:
            raise ValueError("dimensions d'image inconnues : fournir sensor ou width/height")
        if self.focal_mm <= 0:
            raise ValueError("focale invalide")

    @classmethod
    def from_tilt(cls, yaw_deg: float, tilt_deg: float, **kw) -> CameraPose:
        return cls(yaw_deg=yaw_deg, pitch_deg=-tilt_deg, **kw)

    @property
    def tilt_deg(self) -> float:
        return -self.pitch_deg

    @property
    def pixel_mm(self) -> float:
        """Taille d'un pixel dans le plan image, en mm.

        Tient compte d'un éventuel sous-échantillonnage : la pose est définie sur
        la résolution réellement analysée, pas sur celle du capteur.
        """
        if self.sensor is None:
            raise ValueError("pixel_mm requiert un capteur")
        return self.sensor.width_mm / self.width_px

    @property
    def center(self) -> tuple[float, float]:
        return ((self.width_px - 1) / 2.0, (self.height_px - 1) / 2.0)

    def _basis(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Repère caméra (avant, droite, haut) exprimé en (nord, est, haut)."""
        y, p = math.radians(self.yaw_deg), math.radians(self.pitch_deg)
        forward = np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)])
        right = np.array([-math.sin(y), math.cos(y), 0.0])
        up = np.array([-math.sin(p) * math.cos(y), -math.sin(p) * math.sin(y), math.cos(p)])
        return forward, right, up

    def project(self, azimuth_deg: float, elevation_deg: float) -> tuple[float, float] | None:
        """Position image d'une direction. ``None`` si elle est derrière la caméra."""
        az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
        v = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
        forward, right, up = self._basis()
        zc = float(v @ forward)
        if zc <= 1e-9:
            return None
        xc, yc = float(v @ right), float(v @ up)
        r = math.radians(self.roll_deg)
        xr = math.cos(r) * xc + math.sin(r) * yc
        yr = -math.sin(r) * xc + math.cos(r) * yc
        scale = self.focal_mm / self.pixel_mm
        cx, cy = self.center
        return (cx + scale * xr / zc, cy - scale * yr / zc)

    def unproject(self, col: float, row: float) -> tuple[float, float]:
        """Direction (azimut, élévation) d'un pixel."""
        cx, cy = self.center
        scale = self.focal_mm / self.pixel_mm
        xr, yr = (col - cx) / scale, -(row - cy) / scale
        r = math.radians(self.roll_deg)
        xc = math.cos(r) * xr - math.sin(r) * yr
        yc = math.sin(r) * xr + math.cos(r) * yr
        forward, right, up = self._basis()
        v = xc * right + yc * up + forward
        v = v / np.linalg.norm(v)
        return (math.degrees(math.atan2(v[1], v[0])) % 360.0, math.degrees(math.asin(np.clip(v[2], -1, 1))))

    def bearing_of_column(self, col: float) -> float:
        """Azimut d'une colonne au niveau de l'horizon — l'usage courant du pipeline."""
        return self.unproject(col, self.center[1])[0]

    def as_dict(self) -> dict:
        return {
            "yaw_deg": round(self.yaw_deg, 4),
            "pitch_deg": round(self.pitch_deg, 4),
            "roll_deg": round(self.roll_deg, 4),
            "focal_mm": round(self.focal_mm, 4),
            "width_px": self.width_px,
            "height_px": self.height_px,
            "sensor": self.sensor.name if self.sensor else None,
        }

    def copy_with(self, **kw) -> CameraPose:
        data = {
            "yaw_deg": self.yaw_deg, "pitch_deg": self.pitch_deg, "roll_deg": self.roll_deg,
            "focal_mm": self.focal_mm, "sensor": self.sensor,
            "width_px": self.width_px, "height_px": self.height_px,
        }
        data.update(kw)
        return CameraPose(**data)


# --------------------------------------------------------------------------- #
# Aéronefs
# --------------------------------------------------------------------------- #
@dataclass
class AircraftState:
    """Un report ADS-B."""

    icao24: str
    t: float                       # epoch UTC, secondes
    latitude: float
    longitude: float
    altitude_m: float
    geometric_altitude: bool = True   # False = altitude barométrique (moins fiable)
    track_deg: float | None = None    # route vraie
    ground_speed_ms: float | None = None
    callsign: str = ""
    on_ground: bool = False


@dataclass
class AircraftTrack:
    """Trajectoire d'un aéronef, interpolable dans le temps."""

    icao24: str
    states: list[AircraftState] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.states.sort(key=lambda s: s.t)

    @property
    def times(self) -> list[float]:
        return [s.t for s in self.states]

    @property
    def geometric_fraction(self) -> float:
        if not self.states:
            return 0.0
        return sum(1 for s in self.states if s.geometric_altitude) / len(self.states)

    def state_at(self, t: float, max_gap_s: float = 30.0) -> AircraftState | None:
        """Interpole la position à l'instant ``t``.

        L'interpolation est linéaire : en croisière un avion vole droit, et
        l'erreur d'une interpolation linéaire sur quelques dizaines de secondes
        se compte en mètres. En revanche on **refuse d'extrapoler** au-delà de
        ``max_gap_s`` — un trou de données pendant un virage produirait une mire
        fausse, et une mire fausse est pire qu'une mire absente.
        """
        if not self.states:
            return None
        times = self.times
        if t < times[0] - 1e-9 or t > times[-1] + 1e-9:
            return None
        i = bisect.bisect_left(times, t)
        if i < len(times) and abs(times[i] - t) < 1e-9:
            return self.states[i]
        if i == 0 or i >= len(times):
            return None
        a, b = self.states[i - 1], self.states[i]
        if b.t - a.t > max_gap_s:
            return None
        w = (t - a.t) / (b.t - a.t)
        return AircraftState(
            icao24=self.icao24,
            t=t,
            latitude=a.latitude + w * (b.latitude - a.latitude),
            longitude=a.longitude + w * (b.longitude - a.longitude),
            altitude_m=a.altitude_m + w * (b.altitude_m - a.altitude_m),
            geometric_altitude=a.geometric_altitude and b.geometric_altitude,
            track_deg=a.track_deg,
            ground_speed_ms=a.ground_speed_ms,
            callsign=a.callsign or b.callsign,
            on_ground=a.on_ground or b.on_ground,
        )


@dataclass
class Site:
    """Emplacement de la caméra."""

    latitude: float
    longitude: float
    altitude_m: float          # altitude du sol
    height_m: float = 40.0     # hauteur au-dessus du sol

    @property
    def camera_alt_m(self) -> float:
        return self.altitude_m + self.height_m


def look_angles(site: Site, state: AircraftState, altitude_offset_m: float = 0.0) -> tuple[float, float, float]:
    """(azimut, élévation, distance au sol) d'un aéronef vu depuis le site."""
    distance, bearing = great_circle(site.latitude, site.longitude, state.latitude, state.longitude)
    elevation = apparent_elevation_deg(
        state.altitude_m + altitude_offset_m, site.camera_alt_m, distance
    )
    return bearing, elevation, distance


# --------------------------------------------------------------------------- #
# Observations et appariement
# --------------------------------------------------------------------------- #
@dataclass
class SkyObservation:
    """Point lumineux détecté au-dessus de l'horizon."""

    t: float
    col: float
    row: float
    brightness: float = 0.0
    area_px: int = 1


@dataclass
class Correspondence:
    """Association entre un point détecté et un aéronef connu."""

    observation: SkyObservation
    icao24: str
    track: AircraftTrack
    predicted: tuple[float, float]
    residual_px: float
    distance_m: float
    elevation_deg: float
    azimuth_deg: float
    heading_deg: float | None = None
    geometric_altitude: bool = True


def predict_pixel(
    site: Site,
    track: AircraftTrack,
    pose: CameraPose,
    t: float,
    clock_offset_s: float = 0.0,
    altitude_offset_m: float = 0.0,
    max_gap_s: float = 30.0,
) -> tuple[tuple[float, float], AircraftState, float, float, float] | None:
    """Position image attendue d'un aéronef à l'instant d'exposition ``t``."""
    state = track.state_at(t + clock_offset_s, max_gap_s=max_gap_s)
    if state is None or state.on_ground:
        return None
    az, el, dist = look_angles(site, state, altitude_offset_m)
    pixel = pose.project(az, el)
    if pixel is None:
        return None
    return pixel, state, az, el, dist


def associate(
    site: Site,
    observations: list[SkyObservation],
    tracks: dict[str, AircraftTrack],
    pose: CameraPose,
    gate_px: float = 80.0,
    clock_offset_s: float = 0.0,
    altitude_offset_m: float = 0.0,
    require_unambiguous: bool = True,
    ambiguity_px: float = 8.0,
    min_elevation_deg: float = 1.0,
) -> list[Correspondence]:
    """Apparie les points détectés aux aéronefs.

    ``require_unambiguous`` écarte les observations pour lesquelles plusieurs
    aéronefs tombent dans la fenêtre : dans un couloir aérien chargé, deux avions
    peuvent se superposer, et une association fausse tire l'ajustement bien plus
    qu'une observation manquante ne le prive d'information.
    """
    out: list[Correspondence] = []
    for obs in observations:
        candidates: list[Correspondence] = []
        for icao, track in tracks.items():
            got = predict_pixel(
                site, track, pose, obs.t, clock_offset_s, altitude_offset_m
            )
            if got is None:
                continue
            (pcol, prow), state, az, el, dist = got
            if el < min_elevation_deg:
                continue
            residual = math.hypot(pcol - obs.col, prow - obs.row)
            if residual > gate_px:
                continue
            candidates.append(
                Correspondence(
                    observation=obs, icao24=icao, track=track, predicted=(pcol, prow),
                    residual_px=residual, distance_m=dist, elevation_deg=el, azimuth_deg=az,
                    heading_deg=state.track_deg, geometric_altitude=state.geometric_altitude,
                )
            )
        if not candidates:
            continue
        candidates.sort(key=lambda c: c.residual_px)
        if require_unambiguous and len(candidates) > 1:
            best, second = candidates[0].residual_px, candidates[1].residual_px
            # Le critère doit rester valable quand le meilleur candidat tombe
            # exactement sur l'observation : un seuil purement relatif se réduit
            # alors à zéro et ne détecte plus aucune ambiguïté.
            if second < max(2.0 * best, best + ambiguity_px):
                continue
        out.append(candidates[0])
    return out


# --------------------------------------------------------------------------- #
# Identifiabilité
# --------------------------------------------------------------------------- #
def _angular_spread(values: list[float]) -> float:
    """Dispersion d'un jeu d'angles, en degrés, insensible au passage par 0."""
    if len(values) < 2:
        return 0.0
    rad = np.radians(np.asarray(values, dtype=float))
    # Longueur du vecteur résultant : 1 = tous alignés, 0 = uniformément répartis.
    r = math.hypot(float(np.cos(rad).mean()), float(np.sin(rad).mean()))
    return math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(r, 1e-9)))))


def identifiability(correspondences: list[Correspondence]) -> dict:
    """Détermine quels paramètres les données peuvent réellement contraindre.

    C'est le garde-fou central de ce module. Ajuster un paramètre que les
    observations ne contraignent pas ne produit pas une erreur : cela produit une
    valeur plausible et fausse, qui se propage ensuite dans toutes les alertes.
    Mieux vaut geler le paramètre et le dire.

    - **décalage d'horloge** : un retard déplace chaque avion le long de sa
      route. Si tous suivent le même couloir, ce déplacement est indiscernable
      d'une erreur d'azimut. Il faut de la **diversité de cap**.
    - **biais d'altitude** : son effet sur l'élévation varie en 1/distance, alors
      que le tilt agit uniformément. Il faut de la **diversité de distance**.
    - **focale** : elle dilate l'image autour du centre. Sans étalement en
      azimut et en élévation, elle se confond avec le tilt et le lacet.
    """
    n = len(correspondences)
    headings = [c.heading_deg for c in correspondences if c.heading_deg is not None]
    distances = [c.distance_m for c in correspondences]
    azimuths = [c.azimuth_deg for c in correspondences]
    elevations = [c.elevation_deg for c in correspondences]

    heading_spread = _angular_spread(headings)
    azimuth_spread = _angular_spread(azimuths)
    elevation_range = (max(elevations) - min(elevations)) if elevations else 0.0
    distance_ratio = (max(distances) / max(min(distances), 1.0)) if distances else 1.0
    geometric_fraction = (
        sum(1 for c in correspondences if c.geometric_altitude) / n if n else 0.0
    )

    return {
        "n": n,
        "heading_spread_deg": round(heading_spread, 1),
        "azimuth_spread_deg": round(azimuth_spread, 1),
        "elevation_range_deg": round(elevation_range, 2),
        "distance_ratio": round(distance_ratio, 2),
        "geometric_altitude_fraction": round(geometric_fraction, 3),
        "can_fit": {
            "yaw_deg": n >= 4,
            "pitch_deg": n >= 4,
            "roll_deg": n >= 8 and azimuth_spread >= 3.0,
            "focal_mm": n >= 12 and azimuth_spread >= 5.0 and elevation_range >= 3.0,
            "clock_offset_s": n >= 12 and heading_spread >= 25.0,
            "altitude_offset_m": n >= 12 and distance_ratio >= 2.0 and geometric_fraction < 0.9,
        },
    }


# --------------------------------------------------------------------------- #
# Ajustement
# --------------------------------------------------------------------------- #
@dataclass
class CalibrationResult:
    """Résultat d'un ajustement de pose."""

    pose: CameraPose
    clock_offset_s: float = 0.0
    altitude_offset_m: float = 0.0
    fitted: tuple[str, ...] = ()
    frozen: tuple[str, ...] = ()
    n_used: int = 0
    n_rejected: int = 0
    rms_px: float = 0.0
    max_residual_px: float = 0.0
    sigma: dict[str, float] = field(default_factory=dict)
    identifiability: dict = field(default_factory=dict)
    converged: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def rms_deg(self) -> float:
        """Résidu angulaire moyen — la grandeur qui parle vraiment."""
        if self.pose.sensor is None:
            return 0.0
        ifov = math.degrees(math.atan(self.pose.pixel_mm / self.pose.focal_mm))
        return self.rms_px * ifov

    @property
    def bearing_sigma_deg(self) -> float:
        """Incertitude d'azimut à reporter dans les alertes.

        Volontairement conservatrice : on combine l'incertitude formelle du lacet
        et le résidu angulaire par observation, et on impose un plancher. Une
        incertitude sous-estimée est plus nuisible qu'une incertitude large,
        parce qu'elle donne à un opérateur une confiance qu'il n'a pas.
        """
        formal = self.sigma.get("yaw_deg", 0.0)
        return max(0.02, math.hypot(formal, 0.3 * self.rms_deg))

    @property
    def quality(self) -> str:
        if not self.converged or self.n_used < 8:
            return "insufficient"
        if self.rms_px <= 3.0 and self.n_used >= 25:
            return "good"
        if self.rms_px <= 8.0:
            return "usable"
        return "poor"

    def as_dict(self) -> dict:
        return {
            "pose": self.pose.as_dict(),
            "clock_offset_s": round(self.clock_offset_s, 3),
            "altitude_offset_m": round(self.altitude_offset_m, 1),
            "fitted": list(self.fitted),
            "frozen": list(self.frozen),
            "n_used": self.n_used,
            "n_rejected": self.n_rejected,
            "rms_px": round(self.rms_px, 2),
            "rms_deg": round(self.rms_deg, 4),
            "max_residual_px": round(self.max_residual_px, 2),
            "bearing_sigma_deg": round(self.bearing_sigma_deg, 4),
            "sigma": {k: round(v, 5) for k, v in self.sigma.items()},
            "identifiability": self.identifiability,
            "quality": self.quality,
            "converged": self.converged,
            "notes": self.notes,
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def _residuals(
    site: Site,
    correspondences: list[Correspondence],
    pose: CameraPose,
    clock_offset_s: float,
    altitude_offset_m: float,
) -> np.ndarray:
    """Vecteur de résidus (dcol, drow) empilés. ``nan`` pour les points perdus."""
    out = np.full(2 * len(correspondences), np.nan)
    for i, c in enumerate(correspondences):
        got = predict_pixel(site, c.track, pose, c.observation.t, clock_offset_s, altitude_offset_m)
        if got is None:
            continue
        (pcol, prow), *_ = got
        out[2 * i] = pcol - c.observation.col
        out[2 * i + 1] = prow - c.observation.row
    return out


def _apply(pose: CameraPose, params: np.ndarray) -> tuple[CameraPose, float, float]:
    return (
        pose.copy_with(
            yaw_deg=float(params[0]), pitch_deg=float(params[1]),
            roll_deg=float(params[2]), focal_mm=float(params[3]),
        ),
        float(params[4]),
        float(params[5]),
    )


def fit_pose(
    site: Site,
    correspondences: list[Correspondence],
    initial: CameraPose,
    fit: tuple[str, ...] | None = None,
    initial_clock_offset_s: float = 0.0,
    initial_altitude_offset_m: float = 0.0,
    max_iterations: int = 40,
    huber_px: float = 4.0,
    reject_px: float = 25.0,
    auto_freeze: bool = True,
) -> CalibrationResult:
    """Ajuste la pose par moindres carrés robustes (Gauss-Newton amorti).

    Robuste par construction, parce que les données ne le sont pas : une
    association fausse, un satellite, un oiseau proche ou un pixel chaud
    produisent des points aberrants. Poids de Huber, puis rejet des résidus
    au-delà de ``reject_px``, et réajustement.

    ``auto_freeze`` gèle les paramètres que l'analyse d'identifiabilité déclare
    non contraints — c'est ce qui évite de « mesurer » un décalage d'horloge sur
    un unique couloir aérien.
    """
    ident = identifiability(correspondences)
    requested = tuple(fit) if fit is not None else ("yaw_deg", "pitch_deg", "roll_deg", "focal_mm")
    notes: list[str] = []

    if auto_freeze:
        kept = []
        for name in requested:
            if ident["can_fit"].get(name, False):
                kept.append(name)
            else:
                notes.append(f"'{name}' gelé : les observations ne le contraignent pas")
        requested = tuple(kept)

    frozen = tuple(n for n in PARAM_NAMES if n not in requested)
    if not requested or len(correspondences) < 3:
        return CalibrationResult(
            pose=initial, clock_offset_s=initial_clock_offset_s,
            altitude_offset_m=initial_altitude_offset_m,
            frozen=PARAM_NAMES, identifiability=ident, converged=False,
            notes=notes + ["données insuffisantes pour un ajustement"],
        )

    params = np.array([
        initial.yaw_deg, initial.pitch_deg, initial.roll_deg, initial.focal_mm,
        initial_clock_offset_s, initial_altitude_offset_m,
    ], dtype=float)
    free_idx = [PARAM_NAMES.index(n) for n in requested]
    steps = {"yaw_deg": 1e-3, "pitch_deg": 1e-3, "roll_deg": 1e-3,
             "focal_mm": 1e-4, "clock_offset_s": 1e-2, "altitude_offset_m": 1.0}

    active = list(correspondences)
    lam = 1e-3
    converged = False

    for _pass in range(3):                       # ajustement, rejet, réajustement
        for _ in range(max_iterations):
            pose_k, dt_k, dalt_k = _apply(initial, params)
            r = _residuals(site, active, pose_k, dt_k, dalt_k)
            good = np.isfinite(r)
            if good.sum() < 6:
                break

            # Jacobienne numérique (différences centrées) : le problème est
            # petit (≤6 paramètres) et cette voie évite toute dépendance externe.
            J = np.zeros((len(r), len(free_idx)))
            for j, idx in enumerate(free_idx):
                h = steps[PARAM_NAMES[idx]]
                p_plus, p_minus = params.copy(), params.copy()
                p_plus[idx] += h
                p_minus[idx] -= h
                r_plus = _residuals(site, active, *_apply(initial, p_plus))
                r_minus = _residuals(site, active, *_apply(initial, p_minus))
                col = (r_plus - r_minus) / (2 * h)
                J[:, j] = np.nan_to_num(col, nan=0.0)

            res = np.nan_to_num(r, nan=0.0)
            res[~good] = 0.0
            absr = np.abs(res)
            w = np.where(absr <= huber_px, 1.0, huber_px / np.maximum(absr, 1e-9))
            w[~good] = 0.0

            JtW = J.T * w
            H = JtW @ J
            g = JtW @ res
            H_damped = H + lam * np.diag(np.maximum(np.diag(H), 1e-9))
            try:
                delta = np.linalg.solve(H_damped, -g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue

            trial = params.copy()
            for j, idx in enumerate(free_idx):
                trial[idx] += delta[j]
            r_trial = _residuals(site, active, *_apply(initial, trial))
            cost_new = float(np.nansum(np.nan_to_num(r_trial, nan=0.0) ** 2))
            cost_old = float(np.sum(res**2))
            if cost_new < cost_old:
                params = trial
                lam = max(lam * 0.5, 1e-9)
                if np.max(np.abs(delta)) < 1e-6:
                    converged = True
                    break
            else:
                lam *= 4
                if lam > 1e6:
                    break

        # rejet des aberrants, puis on recommence
        pose_k, dt_k, dalt_k = _apply(initial, params)
        r = _residuals(site, active, pose_k, dt_k, dalt_k)
        per_point = np.hypot(r[0::2], r[1::2])
        keep = [i for i, v in enumerate(per_point) if np.isfinite(v) and v <= reject_px]
        if len(keep) == len(active) or len(keep) < 6:
            break
        notes.append(f"{len(active) - len(keep)} observation(s) écartée(s) au-delà de {reject_px:.0f} px")
        active = [active[i] for i in keep]

    pose_final, dt_final, dalt_final = _apply(initial, params)
    r = _residuals(site, active, pose_final, dt_final, dalt_final)
    per_point = np.hypot(r[0::2], r[1::2])
    valid = per_point[np.isfinite(per_point)]
    rms = float(np.sqrt(np.mean(valid**2))) if valid.size else float("inf")

    # Incertitude formelle des paramètres.
    sigma: dict[str, float] = {}
    if valid.size > len(free_idx):
        try:
            J = np.zeros((len(r), len(free_idx)))
            for j, idx in enumerate(free_idx):
                h = steps[PARAM_NAMES[idx]]
                p_plus, p_minus = params.copy(), params.copy()
                p_plus[idx] += h
                p_minus[idx] -= h
                col = (
                    _residuals(site, active, *_apply(initial, p_plus))
                    - _residuals(site, active, *_apply(initial, p_minus))
                ) / (2 * h)
                J[:, j] = np.nan_to_num(col, nan=0.0)
            dof = max(1, 2 * len(active) - len(free_idx))
            s2 = float(np.nansum(np.nan_to_num(r, nan=0.0) ** 2)) / dof
            cov = np.linalg.pinv(J.T @ J) * s2
            for j, idx in enumerate(free_idx):
                sigma[PARAM_NAMES[idx]] = float(math.sqrt(max(cov[j, j], 0.0)))
        except np.linalg.LinAlgError:
            notes.append("covariance non calculable (système mal conditionné)")

    return CalibrationResult(
        pose=pose_final,
        clock_offset_s=dt_final,
        altitude_offset_m=dalt_final,
        fitted=requested,
        frozen=frozen,
        n_used=len(active),
        n_rejected=len(correspondences) - len(active),
        rms_px=rms,
        max_residual_px=float(valid.max()) if valid.size else float("inf"),
        sigma=sigma,
        identifiability=ident,
        converged=converged or rms < reject_px,
        notes=notes,
    )


def calibrate(
    site: Site,
    observations: list[SkyObservation],
    tracks: dict[str, AircraftTrack],
    initial: CameraPose,
    gate_px: float = 80.0,
    fit: tuple[str, ...] | None = None,
    **kw,
) -> CalibrationResult:
    """Chaîne complète : association large, ajustement, réassociation resserrée.

    La deuxième passe compte : une fois la pose approximativement corrigée, la
    fenêtre d'association peut être fermée, ce qui récupère des observations
    initialement ambiguës et écarte des appariements douteux.
    """
    first = associate(site, observations, tracks, initial, gate_px=gate_px)
    coarse = fit_pose(site, first, initial, fit=fit, **kw)
    if coarse.n_used < 6:
        return coarse
    refined = associate(
        site, observations, tracks, coarse.pose,
        gate_px=max(10.0, 4.0 * coarse.rms_px),
        clock_offset_s=coarse.clock_offset_s,
        altitude_offset_m=coarse.altitude_offset_m,
    )
    if len(refined) < len(first) // 2:
        return coarse
    return fit_pose(
        site, refined, coarse.pose, fit=fit,
        initial_clock_offset_s=coarse.clock_offset_s,
        initial_altitude_offset_m=coarse.altitude_offset_m,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Surveillance de dérive
# --------------------------------------------------------------------------- #
def check_drift(
    reference: CalibrationResult,
    current: CalibrationResult,
    yaw_tolerance_deg: float = 0.15,
    pitch_tolerance_deg: float = 0.15,
) -> dict:
    """Compare un étalonnage récent à la référence du site.

    L'usage le plus rentable du module en exploitation : un preset qui a glissé,
    un bras qui a plié ou une platine heurtée se voient ici, avant de se voir sur
    une alerte mal localisée.
    """
    dyaw = angular_diff(current.pose.yaw_deg, reference.pose.yaw_deg)
    dpitch = current.pose.pitch_deg - reference.pose.pitch_deg
    droll = current.pose.roll_deg - reference.pose.roll_deg
    drifted = abs(dyaw) > yaw_tolerance_deg or abs(dpitch) > pitch_tolerance_deg

    if current.quality == "insufficient":
        status, message = "unknown", "étalonnage courant insuffisant : ne rien conclure"
    elif drifted:
        status = "drifted"
        message = (
            f"dérive détectée : lacet {dyaw:+.3f}°, assiette {dpitch:+.3f}° — "
            f"vérifier la fixation et réétalonner avant d'exploiter les positions"
        )
    else:
        status, message = "stable", "pose stable dans les tolérances"

    return {
        "status": status,
        "message": message,
        "delta_yaw_deg": round(dyaw, 4),
        "delta_pitch_deg": round(dpitch, 4),
        "delta_roll_deg": round(droll, 4),
        "reference_quality": reference.quality,
        "current_quality": current.quality,
    }


# --------------------------------------------------------------------------- #
# Détection des points du ciel
# --------------------------------------------------------------------------- #
def detect_sky_points(
    frame: np.ndarray,
    reference: np.ndarray,
    t: float,
    horizon_rows: np.ndarray | None = None,
    horizon_row: int | None = None,
    margin_px: int = 4,
    min_area_px: int = 1,
    max_area_px: int = 60,
    sigma_k: float = 6.0,
) -> list[SkyObservation]:
    """Détecte les points brillants compacts **au-dessus** de l'horizon.

    Un avion à 50 km ne fait qu'un ou deux pixels : c'est un point, pas un objet.
    On cherche donc de petites taches lumineuses nouvelles par rapport à la
    référence de la vue, et uniquement dans le ciel — la zone que le pipeline de
    détection de fumée ignore de toute façon.

    Attention aux traînées de condensation : elles sont bien plus visibles que
    l'avion, mais elles s'étirent derrière lui et dérivent avec le vent. Le
    filtre de surface maximale les écarte volontairement ; seul le point de tête
    est exploitable, et encore.
    """
    from .compat import binary_open, label_components, to_gray

    f = to_gray(frame).astype(np.float32)
    r = to_gray(reference).astype(np.float32)
    if f.shape != r.shape:
        raise ValueError(f"formes incompatibles : {f.shape} vs {r.shape}")

    sky = np.zeros(f.shape, dtype=bool)
    if horizon_rows is not None and len(horizon_rows) > 0:
        cols = np.linspace(0, len(horizon_rows) - 1, f.shape[1]).astype(int)
        for x in range(f.shape[1]):
            h = int(horizon_rows[cols[x]])
            if h < 0:
                h = f.shape[0]
            sky[: max(0, h - margin_px), x] = True
    else:
        h = int(horizon_row if horizon_row is not None else f.shape[0] // 2)
        sky[: max(0, h - margin_px), :] = True
    if not sky.any():
        return []

    diff = f - r
    vals = diff[sky]
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) * 1.4826
    if mad < 1e-6:
        mad = float(vals.std()) or 1.0
    mask = (diff > med + sigma_k * mad) & sky
    mask = binary_open(mask, 1) | (mask & ~binary_open(mask, 1) & (diff > med + 2 * sigma_k * mad))

    labels, n = label_components(mask)
    out: list[SkyObservation] = []
    for lab in range(1, n + 1):
        sel = labels == lab
        area = int(sel.sum())
        if area < min_area_px or area > max_area_px:
            continue
        ys, xs = np.nonzero(sel)
        weights = diff[sel] - (med + sigma_k * mad)
        weights = np.maximum(weights, 1e-6)
        out.append(
            SkyObservation(
                t=t,
                col=float(np.average(xs, weights=weights)),
                row=float(np.average(ys, weights=weights)),
                brightness=float(diff[sel].max()),
                area_px=area,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Sources ADS-B
# --------------------------------------------------------------------------- #
class AdsbSource:
    """Source de positions d'aéronefs."""

    name = "base"

    def states(self, t0: float, t1: float) -> list[AircraftState]:  # pragma: no cover
        raise NotImplementedError

    def tracks(self, t0: float, t1: float, min_states: int = 2) -> dict[str, AircraftTrack]:
        grouped: dict[str, list[AircraftState]] = {}
        for s in self.states(t0, t1):
            grouped.setdefault(s.icao24, []).append(s)
        return {
            k: AircraftTrack(icao24=k, states=v)
            for k, v in grouped.items()
            if len(v) >= min_states
        }


class StaticAdsbSource(AdsbSource):
    """Source alimentée en mémoire ou depuis un JSONL — rejeu et tests."""

    name = "static"

    def __init__(self, states: list[AircraftState] | None = None) -> None:
        self._states = list(states or [])

    def add(self, state: AircraftState) -> None:
        self._states.append(state)

    def states(self, t0: float, t1: float) -> list[AircraftState]:
        return [s for s in self._states if t0 <= s.t <= t1]

    @classmethod
    def from_jsonl(cls, path: str | Path) -> StaticAdsbSource:
        out: list[AircraftState] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(AircraftState(**json.loads(line)))
        return cls(out)

    def save_jsonl(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "\n".join(json.dumps(asdict(s), ensure_ascii=False) for s in self._states),
            encoding="utf-8",
        )


class Dump1090Source(AdsbSource):  # pragma: no cover - matériel
    """Récepteur ADS-B local (dump1090 / readsb, `aircraft.json`).

    **Option recommandée pour ce projet.** Une clé SDR à une vingtaine d'euros et
    une antenne sur le mât suffisent, et cela répond exactement au principe
    d'autonomie du site : aucune dépendance à Internet, horodatage local donc
    bien meilleur, et l'étalonnage continue de fonctionner quand la liaison est
    tombée.
    """

    name = "dump1090"

    def __init__(self, url: str = "http://127.0.0.1:8080/data/aircraft.json", timeout_s: float = 5.0) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("Dump1090Source : `pip install requests`") from exc
        self._requests = requests
        self.url = url
        self.timeout_s = timeout_s
        self._buffer: list[AircraftState] = []

    def poll(self) -> int:
        """Interroge le récepteur et accumule. À appeler périodiquement (~1 Hz)."""
        try:
            data = self._requests.get(self.url, timeout=self.timeout_s).json()
        except Exception:
            return 0
        now = float(data.get("now", 0.0))
        added = 0
        for ac in data.get("aircraft", []):
            if ac.get("lat") is None or ac.get("lon") is None:
                continue
            geo = ac.get("alt_geom")
            baro = ac.get("alt_baro")
            alt = geo if isinstance(geo, (int, float)) else baro
            if not isinstance(alt, (int, float)):
                continue
            self._buffer.append(
                AircraftState(
                    icao24=str(ac.get("hex", "")).lower(),
                    t=now - float(ac.get("seen_pos", 0.0)),
                    latitude=float(ac["lat"]),
                    longitude=float(ac["lon"]),
                    altitude_m=float(alt) * 0.3048,        # pieds → mètres
                    geometric_altitude=isinstance(geo, (int, float)),
                    track_deg=ac.get("track"),
                    ground_speed_ms=(float(ac["gs"]) * 0.514444) if ac.get("gs") else None,
                    callsign=str(ac.get("flight", "")).strip(),
                )
            )
            added += 1
        return added

    def states(self, t0: float, t1: float) -> list[AircraftState]:
        return [s for s in self._buffer if t0 <= s.t <= t1]


class OpenSkySource(AdsbSource):  # pragma: no cover - réseau
    """Réseau collaboratif OpenSky (API REST).

    Pratique pour démarrer sans matériel, mais moins bon qu'un récepteur local :
    cadence plus faible, horodatage moins précis, quotas, et dépendance à
    Internet — c'est-à-dire à la liaison qui tombe justement les jours d'orage.
    Les modalités d'accès (quotas, authentification) évoluent : vérifier la
    documentation du service.
    """

    name = "opensky"

    def __init__(self, url: str = "https://opensky-network.org/api/states/all",
                 auth: tuple[str, str] | None = None, timeout_s: float = 15.0) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("OpenSkySource : `pip install requests`") from exc
        self._requests = requests
        self.url = url
        self.auth = auth
        self.timeout_s = timeout_s
        self._buffer: list[AircraftState] = []

    def poll(self, lat_min: float, lon_min: float, lat_max: float, lon_max: float) -> int:
        params = {"lamin": lat_min, "lomin": lon_min, "lamax": lat_max, "lomax": lon_max}
        try:
            r = self._requests.get(self.url, params=params, auth=self.auth, timeout=self.timeout_s)
            data = r.json()
        except Exception:
            return 0
        added = 0
        for s in data.get("states") or []:
            # indices du format documenté : icao24, callsign, ..., lon, lat,
            # baro_altitude, on_ground, velocity, true_track, ..., geo_altitude
            try:
                lon, lat = s[5], s[6]
                if lon is None or lat is None:
                    continue
                geo, baro = s[13], s[7]
                alt = geo if geo is not None else baro
                if alt is None:
                    continue
                self._buffer.append(
                    AircraftState(
                        icao24=str(s[0]).lower(),
                        t=float(s[3] or s[4] or 0.0),
                        latitude=float(lat), longitude=float(lon),
                        altitude_m=float(alt),
                        geometric_altitude=geo is not None,
                        track_deg=s[10],
                        ground_speed_ms=s[9],
                        callsign=(s[1] or "").strip(),
                        on_ground=bool(s[8]),
                    )
                )
                added += 1
            except (IndexError, TypeError, ValueError):
                continue
        return added

    def states(self, t0: float, t1: float) -> list[AircraftState]:
        return [s for s in self._buffer if t0 <= s.t <= t1]


# --------------------------------------------------------------------------- #
# Trafic synthétique (validation)
# --------------------------------------------------------------------------- #
def synthesize_traffic(
    site: Site,
    n_aircraft: int = 12,
    t0: float = 1_770_000_000.0,
    duration_s: float = 3600.0,
    sample_period_s: float = 5.0,
    altitude_m: float = 10_668.0,
    speed_ms: float = 240.0,
    distance_range_m: tuple[float, float] = (25_000.0, 120_000.0),
    heading_spread_deg: float = 180.0,
    heading_center_deg: float = 90.0,
    geometric_altitude: bool = True,
    seed: int = 3,
) -> dict[str, AircraftTrack]:
    """Trafic synthétique pour valider la chaîne sans matériel ni réseau.

    ``heading_spread_deg`` permet de reproduire le cas dégénéré d'un couloir
    unique, où le décalage d'horloge cesse d'être identifiable.
    """
    rng = np.random.default_rng(seed)
    tracks: dict[str, AircraftTrack] = {}
    for k in range(n_aircraft):
        d0 = float(rng.uniform(*distance_range_m))
        az0 = float(rng.uniform(0, 360))
        heading = heading_center_deg + float(rng.uniform(-heading_spread_deg / 2, heading_spread_deg / 2))
        t_start = t0 + float(rng.uniform(0, max(duration_s - 120.0, 1.0)))

        lat0 = site.latitude + d0 * math.cos(math.radians(az0)) / 111_132.0
        lon0 = site.longitude + d0 * math.sin(math.radians(az0)) / (
            111_320.0 * math.cos(math.radians(site.latitude))
        )
        states: list[AircraftState] = []
        n_samples = int(120.0 / sample_period_s) + 1
        for i in range(n_samples):
            dt = i * sample_period_s
            dn = speed_ms * dt * math.cos(math.radians(heading))
            de = speed_ms * dt * math.sin(math.radians(heading))
            states.append(
                AircraftState(
                    icao24=f"sim{k:03d}",
                    t=t_start + dt,
                    latitude=lat0 + dn / 111_132.0,
                    longitude=lon0 + de / (111_320.0 * math.cos(math.radians(site.latitude))),
                    altitude_m=altitude_m + float(rng.uniform(-600, 600)),
                    geometric_altitude=geometric_altitude,
                    track_deg=heading % 360.0,
                    ground_speed_ms=speed_ms,
                    callsign=f"SIM{k:03d}",
                )
            )
        tracks[f"sim{k:03d}"] = AircraftTrack(icao24=f"sim{k:03d}", states=states)
    return tracks


def synthesize_observations(
    site: Site,
    tracks: dict[str, AircraftTrack],
    true_pose: CameraPose,
    noise_px: float = 1.5,
    clock_offset_s: float = 0.0,
    altitude_offset_m: float = 0.0,
    n_outliers: int = 0,
    sample_period_s: float = 10.0,
    seed: int = 11,
) -> list[SkyObservation]:
    """Observations simulées sous une pose vraie connue.

    ``clock_offset_s`` et ``altitude_offset_m`` injectent les deux biais
    systématiques qui font échouer une implémentation naïve ; ``n_outliers``
    injecte de fausses détections (satellite, oiseau, pixel chaud).
    """
    rng = np.random.default_rng(seed)
    out: list[SkyObservation] = []
    for track in tracks.values():
        times = track.times
        t = times[0]
        while t <= times[-1]:
            got = predict_pixel(
                site, track, true_pose, t,
                clock_offset_s=-clock_offset_s, altitude_offset_m=altitude_offset_m,
            )
            t += sample_period_s
            if got is None:
                continue
            (col, row), _state, _az, el, _d = got
            if el < 1.0 or not (0 <= col < true_pose.width_px and 0 <= row < true_pose.height_px):
                continue
            out.append(
                SkyObservation(
                    t=t - sample_period_s,
                    col=col + float(rng.normal(0, noise_px)),
                    row=row + float(rng.normal(0, noise_px)),
                    brightness=50.0,
                    area_px=2,
                )
            )
    for _ in range(n_outliers):
        out.append(
            SkyObservation(
                t=float(rng.choice([o.t for o in out])) if out else 0.0,
                col=float(rng.uniform(0, true_pose.width_px)),
                row=float(rng.uniform(0, true_pose.height_px * 0.4)),
                brightness=40.0,
                area_px=3,
            )
        )
    return out
