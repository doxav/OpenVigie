"""Relevé d'installation : position et orientation mesurées à la pose.

Répond à l'issue #2. L'étalonnage par trafic aérien atteint le centième de
degré, mais il lui faut du trafic, du ciel dégagé, et une pose initiale
suffisamment proche pour que l'appariement converge. Un relevé fait au moment
de l'installation — smartphone ou petit module GNSS/IMU — comble exactement ce
manque, pour quelques minutes de travail et aucun matériel dédié.

## Ce que le relevé mesure bien, et ce qu'il mesure mal

C'est le point qui décide de tout, et il est contre-intuitif : les trois
grandeurs ne se valent pas du tout.

===================  ==================  ==========================================
Grandeur             Incertitude type    Pourquoi
===================  ==================  ==========================================
Assiette et roulis   **±0,3 à 1°**       L'accéléromètre mesure la gravité. Rien
                                         sur un pylône ne perturbe la gravité.
Position, altitude   ±3 à 10 m           GNSS ordinaire. L'altitude est le point
                                         faible et compte pour l'ADS-B.
**Azimut**           **±2 à 15°**        Le magnétomètre mesure le champ
                                         magnétique — et un pylône treillis en
                                         acier le déforme massivement.
===================  ==================  ==========================================

D'où la complémentarité, qui n'est pas un slogan mais une propriété
géométrique : **le relevé est excellent là où l'ADS-B est coûteux (assiette,
roulis), et mauvais là où l'ADS-B excelle (azimut).** L'un donne gratuitement
ce que l'autre peine à obtenir.

Et l'assiette est justement la grandeur qui commande la portée estimée : une
erreur de 0,5° produit 44 % d'erreur de distance à 5 km pour une caméra
dominant son terrain de 100 m. Un relevé à l'accéléromètre, à ±0,5°, vaut donc
bien mieux qu'un réglage au jugé — et il est disponible dès la pose, avant
toute accumulation de passages d'avions.

## Déclinaison magnétique : obligatoire, jamais devinée

Un smartphone donne le nord **magnétique**. La déclinaison vaut environ 1 à 3°
est en France métropolitaine, varie selon le lieu et dérive d'année en année.
L'oublier, c'est introduire un biais systématique du même ordre que ce que le
relevé prétend mesurer.

Ce module **exige** donc la déclinaison plutôt que de l'approximer : la
calculer demande un modèle géomagnétique (IGRF/WMM) qui n'a pas sa place dans
un cœur embarqué, et une valeur inventée serait exactement le genre de chiffre
plausible et faux que ce projet s'attache à refuser. Elle s'obtient en quelques
secondes sur un calculateur NOAA ou IGN à partir des coordonnées du site.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Incertitudes types, en degrés. Servent de valeurs par défaut et de repères
# de saisie ; un relevé soigné peut faire mieux, un pylône treillis bien pire.
SIGMA_TILT_ACCELEROMETER_DEG = 0.5
SIGMA_ROLL_ACCELEROMETER_DEG = 0.8
SIGMA_AZIMUTH_PHONE_DEG = 5.0          # smartphone, environnement dégagé
SIGMA_AZIMUTH_STEEL_TOWER_DEG = 15.0   # pylône treillis : le champ est déformé
SIGMA_AZIMUTH_SURVEYED_DEG = 0.5       # visée sur amer relevé, ou GNSS bi-antenne

MOUNTING_AZIMUTH_SIGMA = {
    "open": SIGMA_AZIMUTH_PHONE_DEG,        # mât bois/béton, dégagé
    "steel_tower": SIGMA_AZIMUTH_STEEL_TOWER_DEG,
    "surveyed": SIGMA_AZIMUTH_SURVEYED_DEG,  # relevé topographique ou double GNSS
}


class SurveyError(ValueError):
    """Relevé incomplet ou incohérent."""


@dataclass
class InstallationSurvey:
    """Relevé effectué à la pose d'une caméra.

    ``azimuth_magnetic_deg`` est la valeur brute lue au smartphone ou au
    compas ; ``magnetic_declination_deg`` (positive vers l'est) la convertit en
    azimut vrai. ``tilt_deg`` est positif vers le bas, conformément à la
    configuration du site.
    """

    view_id: str
    latitude: float
    longitude: float
    ground_altitude_m: float
    camera_height_m: float

    azimuth_magnetic_deg: float
    magnetic_declination_deg: float | None = None
    tilt_deg: float = 0.0
    roll_deg: float = 0.0

    mounting: str = "steel_tower"
    azimuth_sigma_deg: float | None = None
    tilt_sigma_deg: float = SIGMA_TILT_ACCELEROMETER_DEG
    roll_sigma_deg: float = SIGMA_ROLL_ACCELEROMETER_DEG
    position_sigma_m: float = 5.0
    altitude_sigma_m: float = 8.0

    surveyed_at: str = ""
    instrument: str = "smartphone"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.magnetic_declination_deg is None:
            raise SurveyError(
                "déclinaison magnétique absente. Un smartphone donne le nord "
                "MAGNÉTIQUE ; sans conversion, l'azimut est biaisé de 1 à 3° en "
                "France — du même ordre que ce que le relevé prétend mesurer. "
                "La valeur s'obtient en quelques secondes sur un calculateur "
                "géomagnétique (NOAA/IGN) à partir des coordonnées du site."
            )
        if self.mounting not in MOUNTING_AZIMUTH_SIGMA:
            raise SurveyError(
                f"type de montage inconnu : '{self.mounting}' "
                f"(attendus : {sorted(MOUNTING_AZIMUTH_SIGMA)})"
            )
        if not -90.0 <= self.latitude <= 90.0 or not -180.0 <= self.longitude <= 180.0:
            raise SurveyError("coordonnées hors domaine")
        if self.camera_height_m < 0:
            raise SurveyError("hauteur de caméra négative")
        if self.azimuth_sigma_deg is None:
            self.azimuth_sigma_deg = MOUNTING_AZIMUTH_SIGMA[self.mounting]

    # -- grandeurs dérivées -------------------------------------------------- #
    @property
    def azimuth_true_deg(self) -> float:
        """Azimut géographique, déclinaison appliquée."""
        return (self.azimuth_magnetic_deg + self.magnetic_declination_deg) % 360.0

    @property
    def camera_altitude_m(self) -> float:
        return self.ground_altitude_m + self.camera_height_m

    @property
    def pitch_deg(self) -> float:
        """Convention pose : positif vers le haut (l'inverse du tilt)."""
        return -self.tilt_deg

    def to_pose(self, sensor, focal_mm: float, width_px: int = 0, height_px: int = 0):
        """Construit une pose caméra utilisable comme point de départ."""
        from .calibration import CameraPose

        return CameraPose(
            yaw_deg=self.azimuth_true_deg,
            pitch_deg=self.pitch_deg,
            roll_deg=self.roll_deg,
            focal_mm=focal_mm,
            sensor=sensor,
            width_px=width_px or sensor.width_px,
            height_px=height_px or sensor.height_px,
        )

    def to_site(self):
        """Emplacement au sens du module d'étalonnage."""
        from .calibration import Site

        return Site(
            latitude=self.latitude,
            longitude=self.longitude,
            altitude_m=self.ground_altitude_m,
            height_m=self.camera_height_m,
        )

    def prior_sigma(self) -> dict[str, float]:
        """Incertitude a priori de chaque paramètre de pose, en degrés."""
        return {
            "yaw_deg": float(self.azimuth_sigma_deg),
            "pitch_deg": float(self.tilt_sigma_deg),
            "roll_deg": float(self.roll_sigma_deg),
        }

    def gate_px(self, sensor, focal_mm: float, width_px: int = 0, margin: float = 3.0) -> float:
        """Fenêtre d'appariement déduite de l'incertitude du relevé.

        C'est l'apport concret de l'issue #2 : plutôt qu'une fenêtre fixe et
        généreuse choisie au doigt mouillé, on la dimensionne sur ce que le
        relevé garantit réellement. Une fenêtre plus serrée réduit les
        appariements ambigus — et un faux appariement tire l'ajustement bien
        plus qu'une observation manquante ne le prive d'information.

        Nuance assumée : l'appariement utilise une fenêtre **circulaire**, donc
        dimensionnée par le pire des deux axes. Sur un pylône treillis, où
        l'azimut est incertain à ±15°, la fenêtre couvre tout le champ et le
        relevé ne restreint donc pas la recherche. Sa valeur est alors ailleurs
        — dans l'excellent a priori d'assiette et de roulis, qui fait converger
        l'ajustement bien plus vite. La fenêtre est bornée à la diagonale de
        l'image : au-delà, elle n'exprime plus aucune contrainte.
        """
        width = width_px or sensor.width_px
        height = int(width * sensor.height_px / sensor.width_px)
        focal_px = focal_mm / (sensor.width_mm / width)
        worst_deg = max(self.azimuth_sigma_deg, self.tilt_sigma_deg)
        gate = margin * focal_px * math.tan(math.radians(min(worst_deg, 60.0)))
        return float(min(gate, math.hypot(width, height)))

    def gate_axes_px(self, sensor, focal_mm: float, width_px: int = 0, margin: float = 3.0) -> dict:
        """Fenêtres horizontale et verticale séparées.

        Rend visible l'asymétrie que la fenêtre circulaire masque : sur un
        pylône treillis, la contrainte verticale (assiette, accéléromètre) est
        typiquement trente fois plus serrée que l'horizontale (azimut,
        magnétomètre).
        """
        width = width_px or sensor.width_px
        focal_px = focal_mm / (sensor.width_mm / width)
        return {
            "horizontal_px": round(margin * focal_px * math.tan(
                math.radians(min(self.azimuth_sigma_deg, 60.0))), 1),
            "vertical_px": round(margin * focal_px * math.tan(
                math.radians(min(self.tilt_sigma_deg, 60.0))), 1),
        }

    def as_dict(self) -> dict:
        d = asdict(self)
        d["azimuth_true_deg"] = round(self.azimuth_true_deg, 2)
        d["camera_altitude_m"] = round(self.camera_altitude_m, 1)
        return d

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> InstallationSurvey:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------- #
# Contrôles de cohérence
# --------------------------------------------------------------------------- #
def check_survey(survey: InstallationSurvey, dem=None) -> list[str]:
    """Repère les erreurs de saisie les plus coûteuses.

    Toutes portent sur des grandeurs qui, si elles sont fausses, produisent une
    géométrie plausible et fausse plutôt qu'une erreur visible.
    """
    problems: list[str] = []

    if abs(survey.magnetic_declination_deg) > 30.0:
        problems.append(
            f"déclinaison de {survey.magnetic_declination_deg:+.1f}° : "
            f"invraisemblable en Europe (attendu entre -5° et +5°)"
        )
    if abs(survey.tilt_deg) > 45.0:
        problems.append(
            f"assiette de {survey.tilt_deg:+.1f}° : une caméra de guet regarde "
            f"l'horizon, quelques degrés vers le bas au plus"
        )
    if abs(survey.roll_deg) > 15.0:
        problems.append(f"roulis de {survey.roll_deg:+.1f}° : vérifier la mise à niveau du support")
    if survey.camera_height_m > 200.0:
        problems.append(f"hauteur de {survey.camera_height_m:.0f} m au-dessus du sol : vérifier l'unité")
    if survey.ground_altitude_m == 0.0:
        problems.append(
            "altitude du terrain à 0 m : si le site n'est pas au niveau de la mer, "
            "l'étalonnage par trafic aérien sera faussé de plusieurs dixièmes de degré"
        )
    if survey.mounting == "steel_tower" and survey.azimuth_sigma_deg < 5.0:
        problems.append(
            "azimut annoncé à mieux que 5° sur un pylône treillis : optimiste, "
            "l'acier déforme le champ magnétique local"
        )

    if dem is not None:
        try:
            dem_alt = dem.elevation_at(survey.longitude, survey.latitude)
        except (ValueError, AttributeError):
            problems.append("position hors de l'emprise du MNT fourni")
        else:
            # elevation_at renvoie NaN hors emprise plutôt que de lever : sans ce
            # test, une position hors dalle passait totalement inaperçue.
            if dem_alt != dem_alt:
                problems.append(
                    "position hors de l'emprise du MNT fourni : vérifier les "
                    "coordonnées du relevé ou l'étendue de la dalle"
                )
            else:
                delta = abs(dem_alt - survey.ground_altitude_m)
                if delta > 50.0:
                    problems.append(
                        f"altitude relevée ({survey.ground_altitude_m:.0f} m) et MNT "
                        f"({dem_alt:.0f} m) diffèrent de {delta:.0f} m : vérifier le "
                        f"référentiel vertical ou la position"
                    )

    return problems


# --------------------------------------------------------------------------- #
# Étalonnage amorcé par le relevé
# --------------------------------------------------------------------------- #
@dataclass
class BootstrapResult:
    """Comparaison entre le relevé et l'étalonnage qui en découle."""

    survey_yaw_deg: float
    fitted_yaw_deg: float
    survey_pitch_deg: float
    fitted_pitch_deg: float
    gate_px: float
    quality: str
    consistent: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def yaw_correction_deg(self) -> float:
        return (self.fitted_yaw_deg - self.survey_yaw_deg + 180.0) % 360.0 - 180.0

    @property
    def pitch_correction_deg(self) -> float:
        return self.fitted_pitch_deg - self.survey_pitch_deg

    def as_dict(self) -> dict:
        return {
            "survey_yaw_deg": round(self.survey_yaw_deg, 3),
            "fitted_yaw_deg": round(self.fitted_yaw_deg, 3),
            "yaw_correction_deg": round(self.yaw_correction_deg, 3),
            "survey_pitch_deg": round(self.survey_pitch_deg, 3),
            "fitted_pitch_deg": round(self.fitted_pitch_deg, 3),
            "pitch_correction_deg": round(self.pitch_correction_deg, 3),
            "gate_px": round(self.gate_px, 1),
            "quality": self.quality,
            "consistent": self.consistent,
            "warnings": self.warnings,
        }


def calibrate_from_survey(
    survey: InstallationSurvey,
    observations,
    tracks,
    sensor,
    focal_mm: float,
    width_px: int = 0,
    height_px: int = 0,
    fit: tuple[str, ...] | None = None,
    **kw,
):
    """Étalonne en partant du relevé, avec une fenêtre dimensionnée par lui.

    Renvoie ``(CalibrationResult, BootstrapResult)``. Le second objet compare
    l'ajustement au relevé : un écart d'azimut de plusieurs degrés est normal
    sur un pylône, mais un écart d'assiette important trahit une erreur de
    saisie — l'accéléromètre ne se trompe pas de 3°.
    """
    from .calibration import calibrate

    initial = survey.to_pose(sensor, focal_mm, width_px, height_px)
    gate = survey.gate_px(sensor, focal_mm, width_px)
    result = calibrate(
        survey.to_site(), observations, tracks, initial,
        gate_px=gate, fit=fit, **kw,
    )

    warnings: list[str] = []
    yaw_delta = (result.pose.yaw_deg - initial.yaw_deg + 180.0) % 360.0 - 180.0
    pitch_delta = result.pose.pitch_deg - initial.pitch_deg

    # Découvert en écrivant les tests : quand le relevé est très faux ET la
    # fenêtre serrée, l'appariement ne trouve rien, l'ajustement renvoie la pose
    # initiale inchangée, et l'écart mesuré est donc nul. Sans ce test, un relevé
    # aberrant était rapporté comme « cohérent » — le pire des deux mondes.
    if result.quality == "insufficient":
        warnings.append(
            "étalonnage insuffisant : aucune conclusion sur le relevé. Une "
            "fenêtre d'appariement serrée et un relevé très faux produisent ce "
            "résultat — élargir la fenêtre, ou vérifier azimut et déclinaison."
        )

    if abs(yaw_delta) > 3.0 * survey.azimuth_sigma_deg:
        warnings.append(
            f"azimut corrigé de {yaw_delta:+.2f}°, soit plus de trois fois "
            f"l'incertitude annoncée du relevé ({survey.azimuth_sigma_deg:.1f}°) — "
            f"déclinaison magnétique oubliée ou mal saisie ?"
        )
    if abs(pitch_delta) > 3.0 * survey.tilt_sigma_deg:
        warnings.append(
            f"assiette corrigée de {pitch_delta:+.2f}° : l'accéléromètre est "
            f"fiable à {survey.tilt_sigma_deg:.1f}° près, un tel écart suggère une "
            f"erreur de saisie (signe du tilt ?) ou une altitude de site fausse"
        )

    bootstrap = BootstrapResult(
        survey_yaw_deg=initial.yaw_deg,
        fitted_yaw_deg=result.pose.yaw_deg,
        survey_pitch_deg=initial.pitch_deg,
        fitted_pitch_deg=result.pose.pitch_deg,
        gate_px=gate,
        quality=result.quality,
        consistent=not warnings,
        warnings=warnings,
    )
    return result, bootstrap
