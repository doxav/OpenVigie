"""Géoréférencement par modèle numérique de terrain.

Le modèle terre plate de ``geometry`` suffit à valider un pipeline, pas à
produire des coordonnées exploitables par un centre de secours. Ce module fait
le ray-casting réel sur un MNT et transforme « fumée à droite » en « 44.0231,
3.4712, ellipse 210 × 80 m ».

Trois apports que rien d'autre ne remplace :

  1. **la distance au sol par pixel**, donc la surface d'un panache en m² réels ;
  2. **la ligne d'horizon vraie**, crête par crête — un candidat au-dessus est
     un nuage, quelle que soit la probabilité du réseau ;
  3. **le viewshed**, qui dit quels secteurs sont masqués par le relief : inutile
     de dépenser des caméras ou des presets sur ce qu'on ne peut pas voir.

Format d'entrée volontairement minimal : une grille d'altitudes NumPy plus une
géotransformation. Pas de dépendance GDAL/rasterio dans le cœur — un site en
production a rarement la pile géospatiale complète installée sur sa passerelle.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
# Rayon effectif intégrant la réfraction atmosphérique standard (k ≈ 7/6).
# À 10 km, la correction de courbure vaut ~6,7 m : sur un relief plat, c'est la
# différence entre voir la base d'un panache et la croire au-dessus de l'horizon.
EFFECTIVE_EARTH_RADIUS_M = EARTH_RADIUS_M * 7.0 / 6.0


@dataclass
class GeoTransform:
    """Géoréférencement d'une grille régulière.

    ``crs`` vaut ``geographic`` (degrés, lat/lon) ou ``projected`` (mètres).
    Le premier est le cas courant d'un MNT téléchargé en WGS84 ; le second celui
    d'un MNT en projection métrique (Lambert-93 par exemple).
    """

    origin_x: float          # longitude ou easting du centre du pixel (0, 0)
    origin_y: float          # latitude ou northing
    pixel_x: float           # pas en x (positif vers l'est)
    pixel_y: float           # pas en y (négatif vers le sud, convention raster)
    crs: str = "geographic"

    def __post_init__(self) -> None:
        if self.crs not in ("geographic", "projected"):
            raise ValueError("crs doit être 'geographic' ou 'projected'")
        if self.pixel_x == 0 or self.pixel_y == 0:
            raise ValueError("pas de grille nul")

    def to_pixel(self, x: float, y: float) -> tuple[float, float]:
        """Coordonnées monde → indices (col, row), en flottants."""
        return ((x - self.origin_x) / self.pixel_x, (y - self.origin_y) / self.pixel_y)

    def to_world(self, col: float, row: float) -> tuple[float, float]:
        return (self.origin_x + col * self.pixel_x, self.origin_y + row * self.pixel_y)

    def meters_per_unit(self, latitude: float) -> tuple[float, float]:
        """Facteurs de conversion (x, y) → mètres à une latitude donnée."""
        if self.crs == "projected":
            return (1.0, 1.0)
        return (111_320.0 * math.cos(math.radians(latitude)), 111_132.0)


@dataclass
class DEM:
    """Modèle numérique de terrain."""

    elevation: np.ndarray            # (rows, cols), altitudes en mètres
    transform: GeoTransform
    nodata: float | None = None

    def __post_init__(self) -> None:
        if self.elevation.ndim != 2:
            raise ValueError("la grille d'altitudes doit être 2D")

    @property
    def shape(self) -> tuple[int, int]:
        return self.elevation.shape

    def elevation_at(self, x: float, y: float) -> float:
        """Altitude par interpolation bilinéaire. ``nan`` hors emprise."""
        col, row = self.transform.to_pixel(x, y)
        return self._sample(np.array([col]), np.array([row]))[0]

    def _sample(self, cols: np.ndarray, rows: np.ndarray) -> np.ndarray:
        """Échantillonnage bilinéaire vectorisé."""
        h, w = self.elevation.shape
        inside = (cols >= 0) & (cols <= w - 1) & (rows >= 0) & (rows <= h - 1)
        out = np.full(cols.shape, np.nan, dtype=np.float64)
        if not inside.any():
            return out

        c = np.clip(cols[inside], 0, w - 1)
        r = np.clip(rows[inside], 0, h - 1)
        c0 = np.floor(c).astype(int)
        r0 = np.floor(r).astype(int)
        c1 = np.clip(c0 + 1, 0, w - 1)
        r1 = np.clip(r0 + 1, 0, h - 1)
        fc = c - c0
        fr = r - r0

        top = self.elevation[r0, c0] * (1 - fc) + self.elevation[r0, c1] * fc
        bot = self.elevation[r1, c0] * (1 - fc) + self.elevation[r1, c1] * fc
        vals = top * (1 - fr) + bot * fr

        if self.nodata is not None:
            vals = np.where(np.isclose(vals, self.nodata), np.nan, vals)
        out[inside] = vals
        return out

    # -- entrées / sorties -------------------------------------------------- #
    @classmethod
    def from_npy(cls, array_path: str | Path, meta_path: str | Path | None = None) -> DEM:
        """Charge un MNT depuis un ``.npy`` et un ``.json`` de géoréférencement.

        Convertir un GeoTIFF IGN vers ce format se fait en trois lignes avec
        rasterio, une seule fois, sur un poste de préparation — pas sur la
        passerelle du site.
        """
        array_path = Path(array_path)
        meta_path = Path(meta_path) if meta_path else array_path.with_suffix(".json")
        meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        return cls(
            elevation=np.load(array_path).astype(np.float64),
            transform=GeoTransform(
                origin_x=float(meta["origin_x"]),
                origin_y=float(meta["origin_y"]),
                pixel_x=float(meta["pixel_x"]),
                pixel_y=float(meta["pixel_y"]),
                crs=meta.get("crs", "geographic"),
            ),
            nodata=meta.get("nodata"),
        )

    def save(self, array_path: str | Path) -> None:
        array_path = Path(array_path)
        array_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(array_path, self.elevation)
        meta = {
            "origin_x": self.transform.origin_x,
            "origin_y": self.transform.origin_y,
            "pixel_x": self.transform.pixel_x,
            "pixel_y": self.transform.pixel_y,
            "crs": self.transform.crs,
            "nodata": self.nodata,
        }
        array_path.with_suffix(".json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
# Profil et ray-casting
# --------------------------------------------------------------------------- #
@dataclass
class TerrainProfile:
    """Profil de terrain le long d'un relèvement."""

    distances_m: np.ndarray
    elevations_m: np.ndarray
    depression_deg: np.ndarray   # angle sous l'horizontale depuis la caméra
    visible: np.ndarray          # ligne de vue dégagée depuis la caméra

    @property
    def max_visible_distance_m(self) -> float:
        vis = self.distances_m[self.visible & np.isfinite(self.elevations_m)]
        return float(vis.max()) if vis.size else 0.0


def curvature_drop_m(distance_m: float | np.ndarray) -> np.ndarray:
    """Abaissement apparent dû à la courbure terrestre et à la réfraction."""
    d = np.asarray(distance_m, dtype=np.float64)
    return d * d / (2.0 * EFFECTIVE_EARTH_RADIUS_M)


def terrain_profile(
    dem: DEM,
    cam_lat: float,
    cam_lon: float,
    cam_height_m: float,
    bearing_deg: float,
    max_distance_m: float = 20_000.0,
    step_m: float = 25.0,
    ground_elevation_m: float | None = None,
) -> TerrainProfile:
    """Profil de terrain et angles de dépression le long d'un azimut.

    ``cam_height_m`` est la hauteur **au-dessus du sol** (hauteur de mât) ;
    l'altitude du sol est lue dans le MNT sauf si elle est fournie.
    """
    if step_m <= 0 or max_distance_m <= 0:
        raise ValueError("step_m et max_distance_m doivent être > 0")

    # AUDIT P0-09 (corrigé 0.4.0) : l'API acceptait crs="projected" mais recevait
    # quand même des latitudes/longitudes. Une dalle Lambert-93 (origine
    # ~700000/6600000) interrogée avec 3/44 place la caméra hors emprise — ou
    # pire, à un endroit arbitraire mais plausible. On refuse explicitement
    # plutôt que de calculer faux : la reprojection se fait à la préparation du
    # MNT, sur un poste outillé, pas sur la passerelle du site.
    if dem.transform.crs == "projected":
        raise ValueError(
            "MNT en coordonnées projetées : reprojeter la dalle en WGS84 lors de "
            "la préparation (voir docs/GEOREFERENCEMENT.md). Le cœur embarqué "
            "n'emporte pas de bibliothèque de projection."
        )
    base = (
        ground_elevation_m
        if ground_elevation_m is not None
        else dem.elevation_at(cam_lon, cam_lat)
    )
    if not np.isfinite(base):
        raise ValueError("la caméra est hors de l'emprise du MNT")
    cam_alt = base + cam_height_m

    d = np.arange(step_m, max_distance_m + step_m, step_m, dtype=np.float64)
    mx, my = dem.transform.meters_per_unit(cam_lat)
    dx = d * math.sin(math.radians(bearing_deg)) / mx
    dy = d * math.cos(math.radians(bearing_deg)) / my
    cols, rows = dem.transform.to_pixel(cam_lon + dx, cam_lat + dy)
    elev = dem._sample(cols, rows)

    # Altitude apparente : le terrain lointain « descend » avec la courbure.
    apparent = elev - curvature_drop_m(d)
    with np.errstate(invalid="ignore"):
        depression = np.degrees(np.arctan2(cam_alt - apparent, d))

    # Ligne de vue : un point n'est visible que si aucun point plus proche ne
    # présente un angle de dépression plus faible (c'est-à-dire ne le masque).
    visible = np.zeros(d.shape, dtype=bool)
    horizon = np.inf
    for i in range(d.size):
        if not np.isfinite(depression[i]):
            continue
        if depression[i] < horizon:
            visible[i] = True
            horizon = depression[i]
    return TerrainProfile(d, elev, depression, visible)


def distance_map_from_dem(
    dem: DEM,
    cam_lat: float,
    cam_lon: float,
    cam_height_m: float,
    view_azimuth_deg: float,
    hfov_deg: float,
    vfov_deg: float,
    width_px: int,
    height_px: int,
    tilt_deg: float = 0.0,
    max_distance_m: float = 20_000.0,
    step_m: float = 25.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Carte distance-au-sol par pixel, par ray-casting sur le MNT.

    Renvoie ``(distance_map, horizon_rows)``. ``distance_map`` vaut ``inf`` pour
    tout pixel sans intersection terrain (ciel), et ``horizon_rows`` donne, pour
    chaque colonne, la ligne de la crête — la vraie ligne d'horizon, pas une
    horizontale.

    Précalculé une fois par vue à l'installation, puis rechargé : le coût à
    l'exécution est nul.
    """
    if width_px < 1 or height_px < 1:
        raise ValueError("dimensions d'image invalides")

    dmap = np.full((height_px, width_px), np.inf, dtype=np.float64)
    horizon_rows = np.full(width_px, -1, dtype=np.int32)

    # AUDIT P0-10 (corrigé 0.4.0) : modèle rectilinéaire. La version précédente
    # répartissait linéairement azimuts et élévations sur les pixels, ce qui ne
    # décrit pas une caméra à objectif classique et produisait, au grand-angle,
    # une carte de distance incohérente avec le relèvement reporté dans l'alerte.
    focal_px_x = (width_px / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    focal_px_y = (height_px / 2.0) / math.tan(math.radians(vfov_deg) / 2.0)
    cx, cy = (width_px - 1) / 2.0, (height_px - 1) / 2.0
    col_bearings = view_azimuth_deg + np.degrees(
        np.arctan((np.arange(width_px, dtype=np.float64) - cx) / focal_px_x)
    )

    for col, bearing in enumerate(col_bearings):
        profile = terrain_profile(
            dem, cam_lat, cam_lon, cam_height_m, float(bearing),
            max_distance_m=max_distance_m, step_m=step_m,
        )
        sel = profile.visible & np.isfinite(profile.depression_deg)
        if not sel.any():
            continue
        dep = np.radians(profile.depression_deg[sel]) - math.radians(tilt_deg)
        dist = profile.distances_m[sel]

        # angle → ligne image, projection rectilinéaire
        rows = np.rint(cy + np.tan(dep) * focal_px_y).astype(int)
        inside = (rows >= 0) & (rows < height_px)
        rows, dist = rows[inside], dist[inside]
        if rows.size == 0:
            continue

        # Le terrain le plus proche gagne : ordre croissant puis premier écrit.
        order = np.argsort(dist)
        rows_s, dist_s = rows[order], dist[order]
        col_view = dmap[:, col]
        for r, dd in zip(rows_s, dist_s, strict=True):
            if not np.isfinite(col_view[r]):
                col_view[r] = dd
        horizon_rows[col] = int(rows.min())

    return dmap, horizon_rows


def fill_distance_gaps(dmap: np.ndarray) -> np.ndarray:
    """Bouche les trous d'échantillonnage d'une carte de distance.

    Un pas de ray-casting fini laisse des lignes vides entre deux échantillons.
    On interpole verticalement à l'intérieur de la partie « sol » de chaque
    colonne, sans jamais déborder au-dessus de l'horizon.
    """
    out = dmap.copy()
    for col in range(out.shape[1]):
        column = out[:, col]
        idx = np.nonzero(np.isfinite(column))[0]
        if idx.size < 2:
            continue
        rows = np.arange(idx.min(), idx.max() + 1)
        column[rows] = np.interp(rows, idx, column[idx])
    return out


# --------------------------------------------------------------------------- #
# Localisation d'une alerte
# --------------------------------------------------------------------------- #
def intersect_ground(
    dem: DEM,
    cam_lat: float,
    cam_lon: float,
    cam_height_m: float,
    bearing_deg: float,
    depression_deg: float,
    max_distance_m: float = 20_000.0,
    step_m: float = 25.0,
    tolerance_deg: float = 0.5,
) -> dict | None:
    """Intersection d'une ligne de visée avec le terrain.

    C'est ce qui transforme un pixel en coordonnées. Renvoie ``None`` si aucune
    surface visible ne se trouve à cet angle — donc si la visée part au-dessus
    de la ligne de crête, donc si le candidat est un nuage.
    """
    profile = terrain_profile(
        dem, cam_lat, cam_lon, cam_height_m, bearing_deg,
        max_distance_m=max_distance_m, step_m=step_m,
    )
    sel = profile.visible & np.isfinite(profile.depression_deg)
    if not sel.any():
        return None
    dep = profile.depression_deg[sel]
    dist = profile.distances_m[sel]
    elev = profile.elevations_m[sel]

    # On cherche la surface visible dont l'angle correspond à la visée. Si aucune
    # ne correspond à la tolérance près, la visée ne touche pas le sol : c'est le
    # test qui rejette les nuages, et il doit rester strict.
    i = int(np.argmin(np.abs(dep - depression_deg)))
    if abs(float(dep[i]) - depression_deg) > tolerance_deg:
        return None

    d = float(dist[i])
    mx, my = dem.transform.meters_per_unit(cam_lat)
    lat = cam_lat + d * math.cos(math.radians(bearing_deg)) / my
    lon = cam_lon + d * math.sin(math.radians(bearing_deg)) / mx
    return {
        "latitude": lat,
        "longitude": lon,
        "distance_m": d,
        "elevation_m": float(elev[i]) if np.isfinite(elev[i]) else None,
        "depression_deg": float(dep[i]),
    }


def viewshed_ranges(
    dem: DEM,
    cam_lat: float,
    cam_lon: float,
    cam_height_m: float,
    n_sectors: int = 72,
    max_distance_m: float = 20_000.0,
    step_m: float = 50.0,
) -> dict[float, float]:
    """Portée utile par secteur d'azimut, limitée par le relief.

    Alimente directement ``plan_adaptive_ring`` : une caméra face à une crête à
    2 km n'a aucune raison d'être réglée pour 12 km, et le secteur qui porte à
    15 km mérite une focale plus longue.
    """
    if n_sectors < 1:
        raise ValueError("n_sectors doit être >= 1")
    out: dict[float, float] = {}
    for i in range(n_sectors):
        az = i * 360.0 / n_sectors
        profile = terrain_profile(
            dem, cam_lat, cam_lon, cam_height_m, az,
            max_distance_m=max_distance_m, step_m=step_m,
        )
        out[az] = profile.max_visible_distance_m
    return out


def gaussian_smooth(arr: np.ndarray, radius: int = 3) -> np.ndarray:
    """Lissage par moyenne glissante séparable (NumPy pur)."""
    if radius < 1:
        return arr
    k = np.ones(2 * radius + 1) / (2 * radius + 1)
    out = np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 1, arr)
    return np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 0, out)


def synthetic_dem(
    center_lat: float = 44.0,
    center_lon: float = 3.0,
    size: int = 200,
    span_m: float = 20_000.0,
    relief_m: float = 300.0,
    seed: int = 7,
) -> DEM:
    """MNT synthétique pour les tests et la démonstration.

    Reproduit ce qui compte : des crêtes qui masquent, donc un horizon qui n'est
    pas une horizontale et des secteurs de portée très inégale.
    """
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    cx = cy = (size - 1) / 2.0
    elev = (
        relief_m * 0.28 * np.sin(2 * np.pi * (x - cx) / (size / 1.6))
        + relief_m * 0.22 * np.cos(2 * np.pi * (y - cy) / (size / 2.0))
    )
    elev += relief_m * 0.03 * gaussian_smooth(rng.standard_normal((size, size)), 4)
    # Point haut central : c'est là qu'on installe une tour de guet, et c'est ce
    # qui donne des secteurs de portée très inégaux derrière les crêtes.
    elev += relief_m * 1.1 * np.exp(
        -(((x - cx) ** 2 + (y - cy) ** 2) / (2 * (size / 5.0) ** 2))
    )
    elev = elev - elev.min() + 200.0

    m_per_deg_lon = 111_320.0 * math.cos(math.radians(center_lat))
    pixel_x = (span_m / size) / m_per_deg_lon
    pixel_y = -(span_m / size) / 111_132.0
    return DEM(
        elevation=elev,
        transform=GeoTransform(
            origin_x=center_lon - pixel_x * (size - 1) / 2.0,
            origin_y=center_lat - pixel_y * (size - 1) / 2.0,
            pixel_x=pixel_x,
            pixel_y=pixel_y,
            crs="geographic",
        ),
    )
