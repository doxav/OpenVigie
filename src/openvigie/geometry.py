"""Géométrie du système : optique, planification de couverture, budget de balayage,
projection sol et triangulation multi-tours.

C'est le module qui commande toute l'architecture : la focale décide de la portée,
la portée décide du nombre de caméras, le nombre de caméras décide du temps de
revisite, et le temps de revisite est le vrai plancher de la latence de détection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

EARTH_RADIUS_M = 6_371_000.0


# --------------------------------------------------------------------------- #
# Spécifications matérielles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SensorSpec:
    """Capteur d'imagerie."""

    name: str
    width_px: int
    height_px: int
    pixel_um: float

    @property
    def width_mm(self) -> float:
        return self.width_px * self.pixel_um * 1e-3

    @property
    def height_mm(self) -> float:
        return self.height_px * self.pixel_um * 1e-3


@dataclass(frozen=True)
class LensSpec:
    """Objectif, éventuellement motorisé (f_min == f_max pour une focale fixe)."""

    f_min_mm: float
    f_max_mm: float

    def clamp(self, f_mm: float) -> float:
        return min(max(f_mm, self.f_min_mm), self.f_max_mm)

    @property
    def zoom_ratio(self) -> float:
        return self.f_max_mm / self.f_min_mm


# Capteurs pertinents pour une carte OpenIPC.
# Les pas de pixel sont issus des fiches Sony et doivent être reconfirmés sur la
# référence exacte de module : une erreur de pas de pixel se propage directement
# dans le budget de portée.
IMX307 = SensorSpec("IMX307", 1920, 1080, 2.9)  # 1/2.8", 2 MP  — pilote OpenIPC en amont
IMX327 = SensorSpec("IMX327", 1920, 1080, 2.9)  # 1/2.8", 2 MP  — pilote OpenIPC en amont
IMX335 = SensorSpec("IMX335", 2592, 1944, 2.0)  # 1/2.8", 5 MP  — pilote OpenIPC en amont
IMX415 = SensorSpec("IMX415", 3864, 2192, 1.45) # 1/2.8", 8 MP  — pilote OpenIPC en amont
IMX662 = SensorSpec("IMX662", 1936, 1100, 2.9)  # 1/2.8", 2 MP  — STARVIS 2, portage requis
IMX664 = SensorSpec("IMX664", 2688, 1520, 2.9)  # 1/1.8", 4 MP  — STARVIS 2, portage requis
IMX675 = SensorSpec("IMX675", 2592, 1944, 2.0)  # 1/2.8", 5 MP  — STARVIS 2, portage requis
IMX678 = SensorSpec("IMX678", 3840, 2160, 2.0)  # 1/1.8", 8 MP  — STARVIS 2, portage requis
IMX585 = SensorSpec("IMX585", 3856, 2180, 2.9)  # 1/1.2", 8 MP  — STARVIS 2, portage requis

LENS_27135 = LensSpec(2.7, 13.5)  # objectif motorisé des modules fixes
LENS_30X = LensSpec(4.8, 144.0)   # bloc 30x (à vérifier sur fiche constructeur)
LENS_3611 = LensSpec(3.6, 11.0)   # module 8 MP K678A

SENSORS = {
    s.name: s
    for s in (IMX307, IMX327, IMX335, IMX415, IMX662, IMX664, IMX675, IMX678, IMX585)
}


# --------------------------------------------------------------------------- #
# Optique
# --------------------------------------------------------------------------- #
def hfov_deg(sensor: SensorSpec, f_mm: float) -> float:
    """Champ horizontal en degrés."""
    return 2.0 * math.degrees(math.atan(sensor.width_mm / (2.0 * f_mm)))


def vfov_deg(sensor: SensorSpec, f_mm: float) -> float:
    return 2.0 * math.degrees(math.atan(sensor.height_mm / (2.0 * f_mm)))


def focal_for_hfov(sensor: SensorSpec, target_hfov_deg: float) -> float:
    """Focale donnant un champ horizontal donné."""
    return sensor.width_mm / (2.0 * math.tan(math.radians(target_hfov_deg) / 2.0))


def ifov_mrad(sensor: SensorSpec, f_mm: float) -> float:
    """Angle sous-tendu par un pixel, en milliradians."""
    return (sensor.pixel_um * 1e-3) / f_mm * 1000.0


def ground_sample_m(sensor: SensorSpec, f_mm: float, distance_m: float) -> float:
    """Taille au sol d'un pixel, à distance donnée (petits angles)."""
    return ifov_mrad(sensor, f_mm) * 1e-3 * distance_m


def min_detectable_width_m(
    sensor: SensorSpec, f_mm: float, distance_m: float, min_px: float = 12.0
) -> float:
    """Largeur minimale d'un panache pour être accroché par un détecteur.

    ``min_px`` = 12 est un ordre de grandeur cohérent avec la taille des boîtes
    annotées dans PyroNear-2025 / FIgLib pour des fumées naissantes. C'est un
    paramètre à recalibrer sur vos propres données lors de la phase 1.
    """
    return ground_sample_m(sensor, f_mm, distance_m) * min_px


def max_range_m(
    sensor: SensorSpec, f_mm: float, plume_width_m: float, min_px: float = 12.0
) -> float:
    """Distance maximale à laquelle un panache de largeur donnée reste détectable."""
    gsd_at_1km = ground_sample_m(sensor, f_mm, 1000.0)
    return 1000.0 * plume_width_m / (gsd_at_1km * min_px)


# --------------------------------------------------------------------------- #
# Atmosphère
# --------------------------------------------------------------------------- #
def koschmieder_contrast(distance_m: float, visibility_m: float) -> float:
    """Fraction de contraste résiduel après extinction atmosphérique.

    C(d) = C0 * exp(-3.912 * d / V), V étant la portée optique météorologique.
    """
    if visibility_m <= 0:
        return 0.0
    return math.exp(-3.912 * distance_m / visibility_m)


def estimate_visibility_m(
    observed_contrast: float, reference_contrast: float, distance_m: float
) -> float:
    """Estime la visibilité à partir du contraste d'un amer fixe de distance connue.

    Indicateur gratuit et précieux : il conditionne la portée réelle du site,
    donc la validité de toute alerte lointaine.
    """
    if observed_contrast <= 0 or reference_contrast <= 0:
        return 0.0
    ratio = observed_contrast / reference_contrast
    ratio = min(max(ratio, 1e-6), 1.0 - 1e-9)
    return -3.912 * distance_m / math.log(ratio)


# --------------------------------------------------------------------------- #
# Planification de couverture
# --------------------------------------------------------------------------- #
@dataclass
class ViewPlan:
    """Une vue : caméra fixe, ou preset d'un PTZ."""

    view_id: str
    azimuth_deg: float
    focal_mm: float
    hfov_deg: float
    target_range_m: float
    min_plume_m: float
    tilt_deg: float = 0.0

    def as_dict(self) -> dict:
        return {
            "view_id": self.view_id,
            "azimuth_deg": round(self.azimuth_deg, 2),
            "focal_mm": round(self.focal_mm, 2),
            "hfov_deg": round(self.hfov_deg, 2),
            "target_range_m": round(self.target_range_m),
            "min_plume_m": round(self.min_plume_m, 1),
            "tilt_deg": round(self.tilt_deg, 2),
        }


def plan_uniform_ring(
    sensor: SensorSpec,
    lens: LensSpec,
    n_views: int,
    target_range_m: float,
    overlap: float = 0.15,
    prefix: str = "V",
) -> list[ViewPlan]:
    """Couronne 360° homogène de ``n_views`` vues.

    La focale est choisie pour que les champs se recouvrent de ``overlap``.
    """
    if n_views < 1:
        raise ValueError("n_views doit être >= 1")
    if not 0.0 <= overlap < 0.9:
        raise ValueError("overlap doit être dans [0, 0.9)")
    step = 360.0 / n_views
    needed_hfov = step / (1.0 - overlap)
    focal = lens.clamp(focal_for_hfov(sensor, min(needed_hfov, 179.0)))
    actual_hfov = hfov_deg(sensor, focal)
    return [
        ViewPlan(
            view_id=f"{prefix}{i:02d}",
            azimuth_deg=(i * step) % 360.0,
            focal_mm=focal,
            hfov_deg=actual_hfov,
            target_range_m=target_range_m,
            min_plume_m=min_detectable_width_m(sensor, focal, target_range_m),
        )
        for i in range(n_views)
    ]


def plan_adaptive_ring(
    sensor: SensorSpec,
    lens: LensSpec,
    sector_ranges: dict[float, float],
    min_plume_m: float = 30.0,
    overlap: float = 0.15,
    prefix: str = "V",
) -> list[ViewPlan]:
    """Couronne hétérogène : la focale s'adapte à la portée utile de chaque secteur.

    ``sector_ranges`` associe un azimut (deg) à la distance maximale utile dans
    ce secteur (typiquement issue d'un calcul de visibilité sur MNT : inutile de
    régler une focale longue face à une crête à 2 km).

    C'est l'intérêt principal d'un objectif motorisé 2,7-13,5 mm sur les modules
    fixes : chaque caméra est réglée selon son secteur, au lieu d'un compromis
    unique pour tout le tour d'horizon.
    """
    views: list[ViewPlan] = []
    azimuths = sorted(sector_ranges)
    for i, az in enumerate(azimuths):
        rng = sector_ranges[az]
        # focale minimale donnant min_plume_m à la distance visée
        needed_gsd = min_plume_m / 12.0
        needed_focal = (sensor.pixel_um * 1e-3) * rng / needed_gsd if needed_gsd > 0 else lens.f_min_mm
        focal = lens.clamp(needed_focal)
        views.append(
            ViewPlan(
                view_id=f"{prefix}{i:02d}",
                azimuth_deg=az % 360.0,
                focal_mm=focal,
                hfov_deg=hfov_deg(sensor, focal),
                target_range_m=rng,
                min_plume_m=min_detectable_width_m(sensor, focal, rng),
            )
        )
    _warn_gaps(views, overlap)
    return views


def _warn_gaps(views: list[ViewPlan], overlap: float) -> list[tuple[float, float]]:
    """Renvoie les intervalles d'azimut non couverts par le plan."""
    if not views:
        return [(0.0, 360.0)]
    covered = sorted(
        ((v.azimuth_deg - v.hfov_deg / 2) % 360.0, v.hfov_deg) for v in views
    )
    gaps: list[tuple[float, float]] = []
    cursor = covered[0][0]
    for start, span in covered:
        if start > cursor + 1e-6:
            gaps.append((cursor, start))
        cursor = max(cursor, start + span)
    if cursor < covered[0][0] + 360.0 - 1e-6:
        gaps.append((cursor % 360.0, covered[0][0]))
    return gaps


def coverage_gaps(views: list[ViewPlan]) -> list[tuple[float, float]]:
    """Trous de couverture azimutale, en degrés. Doit être vide pour un site opérationnel."""
    return _warn_gaps(views, 0.0)


# --------------------------------------------------------------------------- #
# Budget de balayage
# --------------------------------------------------------------------------- #
@dataclass
class ScanBudget:
    n_views: int
    dwell_s: float
    slew_s: float
    is_ptz: bool
    cycle_s: float = field(init=False)
    moves_per_year: float = field(init=False)
    frames_per_visit: int = field(init=False)

    def __post_init__(self) -> None:
        if self.is_ptz:
            self.cycle_s = self.n_views * (self.dwell_s + self.slew_s)
            self.moves_per_year = (
                self.n_views * (365.25 * 24 * 3600) / max(self.cycle_s, 1e-9)
            )
        else:
            # Caméras fixes : toutes les vues sont observées en permanence.
            self.cycle_s = self.dwell_s
            self.moves_per_year = 0.0
        self.frames_per_visit = max(1, int(self.dwell_s / 3.0))

    @property
    def detection_latency_floor_s(self) -> float:
        """Plancher de latence : il faut au moins 3 revisites pour confirmer."""
        return self.cycle_s * 3.0

    def as_dict(self) -> dict:
        return {
            "n_views": self.n_views,
            "cycle_s": round(self.cycle_s, 1),
            "cycle_min": round(self.cycle_s / 60.0, 2),
            "latency_floor_min": round(self.detection_latency_floor_s / 60.0, 2),
            "moves_per_year": round(self.moves_per_year),
            "frames_per_visit": self.frames_per_visit,
        }


def scan_budget(
    n_views: int, dwell_s: float = 12.0, slew_s: float = 3.0, is_ptz: bool = True
) -> ScanBudget:
    return ScanBudget(n_views=n_views, dwell_s=dwell_s, slew_s=slew_s, is_ptz=is_ptz)


# --------------------------------------------------------------------------- #
# Projection sol (modèle terre plate ; remplaçable par un vrai MNT)
# --------------------------------------------------------------------------- #
def flat_earth_distance_map(
    sensor: SensorSpec,
    f_mm: float,
    camera_height_m: float,
    tilt_deg: float = 0.0,
    max_distance_m: float = 20_000.0,
) -> np.ndarray:
    """Carte distance-au-sol par pixel, modèle terre plate.

    Chaque ligne de l'image correspond à un angle de dépression ; la distance au
    sol vaut h / tan(dépression). Au-dessus de l'horizon la distance est infinie
    (``np.inf``) : c'est ce qui permet de rejeter les nuages sans aucun modèle
    appris. Pour un site réel, remplacer par un ray-casting sur RGE ALTI.
    """
    h = sensor.height_px
    rows = np.arange(h, dtype=np.float64)
    # AUDIT P0-10 (corrigé 0.4.0) : la version précédente répartissait les angles
    # linéairement sur les lignes, ce qui décrit une projection équirectangulaire
    # et non une caméra rectilinéaire. Au grand-angle (2,8 mm) l'écart atteignait
    # 3,4° en bord de champ, soit ~300 m d'erreur transversale à 5 km, et surtout
    # il était INCOHÉRENT avec pixel_to_bearing, déjà rectilinéaire : le relèvement
    # et la distance d'une même alerte ne suivaient pas le même modèle optique.
    focal_px = f_mm / (sensor.pixel_um * 1e-3)
    angle = np.arctan((rows - (h - 1) / 2.0) / focal_px)
    depression = angle + math.radians(tilt_deg)
    dist = np.full(h, np.inf)
    below = depression > 1e-6
    dist[below] = camera_height_m / np.tan(depression[below])
    dist = np.clip(dist, 0.0, max_distance_m)
    dist[~below] = np.inf
    return np.repeat(dist[:, None], sensor.width_px, axis=1)


def horizon_row(sensor: SensorSpec, f_mm: float, tilt_deg: float = 0.0) -> int:
    """Ligne image de l'horizon géométrique (négatif si hors champ vers le haut).

    AUDIT P0-10 : modèle rectilinéaire, cohérent avec ``pixel_to_bearing`` et
    ``flat_earth_distance_map``.
    """
    h = sensor.height_px
    focal_px = f_mm / (sensor.pixel_um * 1e-3)
    return int(round((h - 1) / 2.0 - math.tan(math.radians(tilt_deg)) * focal_px))


def ground_mask(distance_map: np.ndarray) -> np.ndarray:
    """Masque des pixels ayant une intersection avec le sol."""
    return np.isfinite(distance_map)


def pixel_area_to_m2(
    n_pixels: int, distance_m: float, sensor: SensorSpec, f_mm: float
) -> float:
    """Surface réelle approximative correspondant à un nombre de pixels."""
    gsd = ground_sample_m(sensor, f_mm, distance_m)
    return n_pixels * gsd * gsd


def pixel_to_bearing(
    col: float, sensor: SensorSpec, f_mm: float, view_azimuth_deg: float
) -> float:
    """Azimut absolu d'une colonne image."""
    offset_mm = (col - (sensor.width_px - 1) / 2.0) * sensor.pixel_um * 1e-3
    return (view_azimuth_deg + math.degrees(math.atan(offset_mm / f_mm))) % 360.0


# --------------------------------------------------------------------------- #
# Triangulation multi-tours
# --------------------------------------------------------------------------- #
def triangulate(
    lat1: float, lon1: float, bearing1_deg: float,
    lat2: float, lon2: float, bearing2_deg: float,
) -> tuple[float, float] | None:
    """Intersection de deux relèvements (approximation ENU locale).

    Renvoie ``None`` si les relèvements sont parallèles ou si l'intersection est
    derrière l'une des deux tours (solution non physique).
    """
    lat0 = math.radians((lat1 + lat2) / 2.0)
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(lat0)

    def to_en(lat: float, lon: float) -> tuple[float, float]:
        return ((lon - lon1) * m_per_deg_lon, (lat - lat1) * m_per_deg_lat)

    e1, n1 = to_en(lat1, lon1)
    e2, n2 = to_en(lat2, lon2)
    d1 = (math.sin(math.radians(bearing1_deg)), math.cos(math.radians(bearing1_deg)))
    d2 = (math.sin(math.radians(bearing2_deg)), math.cos(math.radians(bearing2_deg)))

    det = d1[0] * (-d2[1]) - d1[1] * (-d2[0])
    if abs(det) < 1e-9:
        return None
    rhs_e, rhs_n = e2 - e1, n2 - n1
    t1 = (rhs_e * (-d2[1]) - rhs_n * (-d2[0])) / det
    t2 = (d1[0] * rhs_n - d1[1] * rhs_e) / det
    if t1 <= 0 or t2 <= 0:
        return None
    e = e1 + t1 * d1[0]
    n = n1 + t1 * d1[1]
    return (lat1 + n / m_per_deg_lat, lon1 + e / m_per_deg_lon)


def bearing_range_to_latlon(
    lat: float, lon: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """Point à ``distance_m`` dans la direction ``bearing_deg`` depuis (lat, lon)."""
    lat0 = math.radians(lat)
    dn = distance_m * math.cos(math.radians(bearing_deg))
    de = distance_m * math.sin(math.radians(bearing_deg))
    return (lat + dn / 111_132.0, lon + de / (111_320.0 * math.cos(lat0)))


def angular_diff(a_deg: float, b_deg: float) -> float:
    """Écart angulaire signé minimal entre deux azimuts, dans [-180, 180]."""
    return (a_deg - b_deg + 180.0) % 360.0 - 180.0
