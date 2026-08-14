"""Planification par secteurs angulaires utiles.

Répond à l'issue #1. La planification supposait implicitement qu'un site devait
couvrir 360°, et déduisait le nombre de caméras de cette contrainte. C'est
rarement ce que le terrain demande : une direction peut être masquée par une
crête, dépourvue de végétation, ou impossible à couvrir depuis le point de
montage disponible.

L'unité de base devient donc le **secteur angulaire utile**, et l'architecture
qui le couvre devient un choix à instruire plutôt qu'un présupposé. Trois
architectures sont évaluées sur les mêmes secteurs :

``FixedRing``            N caméras fixes, une par tranche de secteur.
``PtzSector``            une PTZ qui balaye un nombre limité de positions.
``WideAnglePlusPtz``     une fixe grand-angle en veille permanente, une PTZ
                         orientée à la demande.

Le compromis central n'est pas « combien de caméras » mais **portée contre
revisite**, et il n'a pas la même forme selon l'architecture :

- une caméra fixe voit en permanence, mais son champ large limite la portée ;
- une PTZ voit loin quand elle regarde au bon endroit, et pas du tout ailleurs ;
- l'option grand-angle + PTZ détecte à la portée (courte) du grand-angle et
  confirme à la résolution (fine) de la PTZ. C'est une architecture de
  **confirmation**, pas d'extension de portée — le contraire de ce qu'on
  suppose spontanément.

Le nombre de mouvements PTZ par an est calculé partout, parce que c'est lui qui
condamne la plupart des têtes : à 8 positions et 2 min de cycle, on dépasse les
2 millions de mouvements annuels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import (
    LensSpec,
    SensorSpec,
    hfov_deg,
    max_range_m,
    min_detectable_width_m,
)

SECONDS_PER_YEAR = 365.25 * 24 * 3600


# --------------------------------------------------------------------------- #
# Secteurs
# --------------------------------------------------------------------------- #
@dataclass
class Sector:
    """Un secteur angulaire réellement utile à surveiller.

    ``start_deg`` → ``end_deg`` se lit dans le sens horaire ; un secteur peut
    donc franchir le nord (par exemple 300° → 70°). ``max_range_m`` est la
    portée utile dans ce secteur, typiquement issue du viewshed : inutile de
    régler une focale longue face à une crête à 2 km.
    """

    name: str
    start_deg: float
    end_deg: float
    max_range_m: float
    priority: float = 1.0          # revisite relative ; 2.0 = visité deux fois plus
    target_plume_m: float = 30.0   # panache à détecter dans ce secteur

    def __post_init__(self) -> None:
        if self.max_range_m <= 0:
            raise ValueError(f"secteur '{self.name}' : portée utile nulle ou négative")
        if self.priority <= 0:
            raise ValueError(f"secteur '{self.name}' : priorité doit être > 0")
        self.start_deg %= 360.0
        self.end_deg %= 360.0

    @property
    def span_deg(self) -> float:
        """Ouverture angulaire, en gérant le franchissement du nord."""
        span = (self.end_deg - self.start_deg) % 360.0
        return 360.0 if span == 0.0 else span

    @property
    def center_deg(self) -> float:
        return (self.start_deg + self.span_deg / 2.0) % 360.0

    def contains(self, azimuth_deg: float) -> bool:
        return ((azimuth_deg - self.start_deg) % 360.0) <= self.span_deg + 1e-9

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "start_deg": round(self.start_deg, 1),
            "end_deg": round(self.end_deg, 1),
            "span_deg": round(self.span_deg, 1),
            "max_range_m": round(self.max_range_m),
            "priority": self.priority,
        }


def total_span_deg(sectors: list[Sector]) -> float:
    """Ouverture cumulée, en fusionnant les recouvrements.

    Deux secteurs qui se chevauchent ne comptent qu'une fois : c'est la surface
    angulaire réellement à couvrir, pas la somme des déclarations.
    """
    if not sectors:
        return 0.0
    # Échantillonnage au demi-degré : suffisant pour un budget de conception,
    # et immunisé contre les cas tordus de franchissement du nord.
    covered = set()
    for s in sectors:
        n = int(s.span_deg * 2)
        for i in range(n + 1):
            covered.add(round((s.start_deg + i * 0.5) % 360.0, 1))
    return min(len(covered) * 0.5, 360.0)


def sectors_from_viewshed(
    ranges: dict[float, float],
    min_useful_range_m: float = 2_000.0,
    merge_gap_deg: float = 15.0,
) -> list[Sector]:
    """Déduit les secteurs utiles d'un viewshed.

    Une direction dont la portée est inférieure à ``min_useful_range_m`` est
    considérée comme non exploitable — masquée par le relief ou par un obstacle
    proche — et n'est donc pas couverte. C'est exactement le cas que l'issue #1
    décrit : cesser de dépenser des caméras sur ce qu'on ne peut pas voir.
    """
    if not ranges:
        return []
    azimuths = sorted(ranges)
    useful = [az for az in azimuths if ranges[az] >= min_useful_range_m]
    if not useful:
        return []

    step = 360.0 / len(azimuths)
    groups: list[list[float]] = [[useful[0]]]
    for az in useful[1:]:
        if (az - groups[-1][-1]) <= merge_gap_deg + step:
            groups[-1].append(az)
        else:
            groups.append([az])
    # fusion circulaire : le dernier groupe peut rejoindre le premier
    if len(groups) > 1 and (360.0 - groups[-1][-1] + groups[0][0]) <= merge_gap_deg + step:
        groups[0] = groups[-1] + groups[0]
        groups.pop()

    out: list[Sector] = []
    for i, group in enumerate(groups):
        out.append(
            Sector(
                name=f"S{i:02d}",
                start_deg=(group[0] - step / 2) % 360.0,
                end_deg=(group[-1] + step / 2) % 360.0,
                max_range_m=min(ranges[az] for az in group),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Coûts (indicatifs, pour comparer des architectures entre elles)
# --------------------------------------------------------------------------- #
@dataclass
class CostModel:
    """Coûts unitaires indicatifs, en USD hors taxes et hors installation.

    Sert uniquement à **comparer des architectures entre elles** ; les valeurs
    absolues n'ont pas vocation à être un devis. Le poste dominant d'un site
    réel — mât, génie civil, main-d'œuvre en hauteur — n'est pas ici parce
    qu'il ne dépend pas de l'architecture choisie.
    """

    fixed_module_usd: float = 86.0        # module fixe 5 MP, objectif motorisé
    wide_module_usd: float = 86.0         # même module, réglé grand-angle
    ptz_block_usd: float = 308.0          # bloc 30x
    ptz_head_usd: float = 1_453.0         # tête motorisée industrielle
    enclosure_per_camera_usd: float = 100.0
    compute_usd: float = 249.0


# --------------------------------------------------------------------------- #
# Évaluation d'une architecture
# --------------------------------------------------------------------------- #
@dataclass
class ArchitectureEvaluation:
    """Ce que vaut une architecture sur un jeu de secteurs donné."""

    name: str
    n_fixed_cameras: int
    n_ptz: int
    n_presets: int
    hfov_deg: float
    focal_mm: float
    detection_range_m: float
    revisit_s: float
    ptz_moves_per_year: float
    cost_usd: float
    covered_span_deg: float
    required_span_deg: float
    required_range_m: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def n_cameras(self) -> int:
        return self.n_fixed_cameras + self.n_ptz

    @property
    def covers_all(self) -> bool:
        """Couverture ANGULAIRE seulement — voir aussi ``meets_range``."""
        return self.covered_span_deg >= self.required_span_deg - 1.0

    @property
    def meets_range(self) -> bool:
        """La portée de détection atteint-elle celle demandée par les secteurs ?

        Découvert en écrivant les tests : une architecture pouvait être déclarée
        « couvrante » alors qu'elle balayait bien tout l'angle demandé mais sans
        jamais voir assez loin pour y détecter quoi que ce soit. Couvrir un
        secteur et le surveiller utilement sont deux choses différentes.
        """
        return self.detection_range_m >= self.required_range_m - 1.0

    @property
    def is_viable(self) -> bool:
        return self.covers_all and self.meets_range

    @property
    def mean_latency_s(self) -> float:
        """Attente moyenne avant qu'un départ de feu soit regardé.

        Pour une caméra fixe, c'est nul : elle regarde en permanence. Pour une
        ronde PTZ, c'est en moyenne la moitié du cycle — et c'est un plancher
        de latence que ni le modèle ni le réseau ne peuvent rattraper.
        """
        return self.revisit_s / 2.0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "cameras": self.n_cameras,
            "fixed": self.n_fixed_cameras,
            "ptz": self.n_ptz,
            "presets": self.n_presets,
            "hfov_deg": round(self.hfov_deg, 1),
            "focal_mm": round(self.focal_mm, 2),
            "detection_range_km": round(self.detection_range_m / 1000.0, 1),
            "revisit_min": round(self.revisit_s / 60.0, 2),
            "mean_latency_min": round(self.mean_latency_s / 60.0, 2),
            "ptz_moves_per_year": round(self.ptz_moves_per_year),
            "cost_usd": round(self.cost_usd),
            "covers_all": self.covers_all,
            "meets_range": self.meets_range,
            "viable": self.is_viable,
            "notes": self.notes,
        }


def _presets_for_span(span_deg: float, hfov: float, overlap: float) -> int:
    """Nombre de positions nécessaires pour couvrir une ouverture donnée."""
    effective = hfov * (1.0 - overlap)
    if effective <= 0:
        raise ValueError("recouvrement trop élevé : champ effectif nul")
    return max(1, math.ceil(span_deg / effective))


def evaluate_fixed_ring(
    sectors: list[Sector],
    sensor: SensorSpec,
    lens: LensSpec,
    cost: CostModel | None = None,
    overlap: float = 0.15,
    add_confirmation_ptz: bool = False,
) -> ArchitectureEvaluation:
    """N caméras fixes couvrant les secteurs utiles, sans balayage.

    Revisite nulle, usure nulle. La focale est choisie pour atteindre le
    panache visé à la portée du secteur le plus exigeant.
    """
    cost = cost or CostModel()
    if not sectors:
        raise ValueError("aucun secteur à couvrir")

    span = total_span_deg(sectors)
    target_range = max(s.max_range_m for s in sectors)
    target_plume = min(s.target_plume_m for s in sectors)

    # Focale minimale atteignant le panache visé à la portée du secteur le plus
    # exigeant ; bornée par les limites physiques de l'objectif.
    needed_gsd = target_plume / 12.0
    needed_focal = (sensor.pixel_um * 1e-3) * target_range / needed_gsd
    focal = lens.clamp(needed_focal)
    hfov = hfov_deg(sensor, focal)

    n = _presets_for_span(span, hfov, overlap)
    notes = []
    if focal < needed_focal - 1e-6:
        notes.append(
            f"objectif limité à {lens.f_max_mm:.1f} mm : panache minimum "
            f"{min_detectable_width_m(sensor, focal, target_range):.0f} m à "
            f"{target_range / 1000:.1f} km au lieu de {target_plume:.0f} m"
        )

    total = n * (cost.fixed_module_usd + cost.enclosure_per_camera_usd)
    n_ptz = 0
    if add_confirmation_ptz:
        n_ptz = 1
        total += cost.ptz_block_usd + cost.ptz_head_usd + cost.enclosure_per_camera_usd
        notes.append("PTZ de confirmation incluse (aucun balayage : mouvements sur alerte seulement)")

    return ArchitectureEvaluation(
        name="Anneau de caméras fixes",
        n_fixed_cameras=n, n_ptz=n_ptz, n_presets=0,
        hfov_deg=hfov, focal_mm=focal,
        detection_range_m=max_range_m(sensor, focal, target_plume),
        revisit_s=0.0, ptz_moves_per_year=0.0,
        cost_usd=total, covered_span_deg=min(n * hfov * (1 - overlap), 360.0),
        required_span_deg=span, required_range_m=target_range, notes=notes,
    )


def evaluate_ptz_sector(
    sectors: list[Sector],
    sensor: SensorSpec,
    lens: LensSpec,
    cost: CostModel | None = None,
    overlap: float = 0.15,
    dwell_s: float = 15.0,
    settle_s: float = 4.0,
) -> ArchitectureEvaluation:
    """Option A de l'issue #1 : une PTZ balayant un nombre limité de positions.

    La détection n'a lieu qu'une fois la tête stabilisée — les images prises
    pendant le déplacement sont inutilisables pour une différence au fond.
    """
    cost = cost or CostModel()
    if not sectors:
        raise ValueError("aucun secteur à couvrir")

    span = total_span_deg(sectors)
    target_range = max(s.max_range_m for s in sectors)
    target_plume = min(s.target_plume_m for s in sectors)

    needed_gsd = target_plume / 12.0
    needed_focal = (sensor.pixel_um * 1e-3) * target_range / needed_gsd
    focal = lens.clamp(needed_focal)
    hfov = hfov_deg(sensor, focal)

    n_presets = _presets_for_span(span, hfov, overlap)
    # Les secteurs prioritaires sont revisités plus souvent : ils ajoutent des
    # positions dans la ronde sans élargir la couverture.
    extra = sum(
        _presets_for_span(s.span_deg, hfov, overlap) * (s.priority - 1.0)
        for s in sectors if s.priority > 1.0
    )
    visits_per_cycle = n_presets + int(round(extra))
    cycle_s = visits_per_cycle * (dwell_s + settle_s)
    moves_per_year = visits_per_cycle * SECONDS_PER_YEAR / max(cycle_s, 1e-9)

    notes = [
        f"{visits_per_cycle} position(s) par cycle ; détection uniquement après stabilisation",
    ]
    if moves_per_year > 500_000:
        notes.append(
            f"{moves_per_year:,.0f} mouvements/an : au-delà de la tenue d'une tête "
            f"de vidéosurveillance courante — exiger un positionneur à service continu"
        )
    if cycle_s > 300:
        notes.append(
            f"cycle de {cycle_s / 60:.1f} min : la latence est dominée par le "
            f"balayage, pas par le modèle"
        )

    return ArchitectureEvaluation(
        name="Module PTZ par secteur",
        n_fixed_cameras=0, n_ptz=1, n_presets=visits_per_cycle,
        hfov_deg=hfov, focal_mm=focal,
        detection_range_m=max_range_m(sensor, focal, target_plume),
        revisit_s=cycle_s, ptz_moves_per_year=moves_per_year,
        cost_usd=cost.ptz_block_usd + cost.ptz_head_usd + cost.enclosure_per_camera_usd,
        covered_span_deg=min(n_presets * hfov * (1 - overlap), 360.0),
        required_span_deg=span, required_range_m=target_range, notes=notes,
    )


def evaluate_wide_plus_ptz(
    sectors: list[Sector],
    sensor: SensorSpec,
    lens: LensSpec,
    cost: CostModel | None = None,
    overlap: float = 0.15,
    candidates_per_day: float = 12.0,
    confirmation_moves: int = 2,
) -> ArchitectureEvaluation:
    """Option B de l'issue #1 : grand-angle en veille + PTZ à la demande.

    Deux propriétés à ne pas confondre :

    - **la portée de détection est celle du grand-angle**, pas celle de la PTZ.
      La PTZ ne voit que là où on l'envoie, et on ne l'envoie que sur ce que le
      grand-angle a déjà vu. Cette architecture améliore la *levée de doute*,
      pas la distance à laquelle un départ de feu est repéré ;
    - **les mouvements PTZ deviennent proportionnels au nombre de candidats**,
      plus au balayage. C'est ce qui fait passer l'usure de quelques millions
      de mouvements par an à quelques milliers.
    """
    cost = cost or CostModel()
    if not sectors:
        raise ValueError("aucun secteur à couvrir")

    span = total_span_deg(sectors)
    target_range = max(s.max_range_m for s in sectors)
    target_plume = min(s.target_plume_m for s in sectors)

    # Le grand-angle vise à couvrir le secteur avec le moins de caméras
    # possible : on part du champ le plus large que l'objectif permet.
    wide_focal = lens.f_min_mm
    wide_hfov = hfov_deg(sensor, wide_focal)
    n_wide = _presets_for_span(span, wide_hfov, overlap)
    detection_range = max_range_m(sensor, wide_focal, target_plume)

    moves_per_year = candidates_per_day * confirmation_moves * 365.25

    notes = [
        f"portée de détection fixée par le grand-angle ({detection_range / 1000:.1f} km "
        f"pour un panache de {target_plume:.0f} m), non par la PTZ",
        f"mouvements proportionnels aux candidats ({candidates_per_day:.0f}/jour), non au balayage",
    ]

    return ArchitectureEvaluation(
        name="Grand-angle + PTZ à la demande",
        n_fixed_cameras=n_wide, n_ptz=1, n_presets=0,
        hfov_deg=wide_hfov, focal_mm=wide_focal,
        detection_range_m=detection_range,
        revisit_s=0.0, ptz_moves_per_year=moves_per_year,
        cost_usd=(n_wide * (cost.wide_module_usd + cost.enclosure_per_camera_usd)
                  + cost.ptz_block_usd + cost.ptz_head_usd + cost.enclosure_per_camera_usd),
        covered_span_deg=min(n_wide * wide_hfov * (1 - overlap), 360.0),
        required_span_deg=span, required_range_m=target_range, notes=notes,
    )


def compare_architectures(
    sectors: list[Sector],
    sensor: SensorSpec,
    lens: LensSpec,
    cost: CostModel | None = None,
    **kw,
) -> list[ArchitectureEvaluation]:
    """Évalue les architectures candidates sur les mêmes secteurs.

    Aucune n'est « la bonne » dans l'absolu : le classement dépend de ce qu'on
    accepte de perdre. C'est précisément pour ça que la comparaison est
    exposée plutôt qu'un choix imposé.
    """
    return [
        evaluate_fixed_ring(sectors, sensor, lens, cost, add_confirmation_ptz=True, **kw),
        evaluate_ptz_sector(sectors, sensor, lens, cost, **kw),
        evaluate_wide_plus_ptz(sectors, sensor, lens, cost, **kw),
    ]


def recommend(evaluations: list[ArchitectureEvaluation], max_latency_s: float = 120.0) -> str:
    """Formule une recommandation motivée, ou dit pourquoi aucune ne convient."""
    viable = [e for e in evaluations if e.is_viable and e.mean_latency_s <= max_latency_s]
    if not viable:
        angulaire = [e for e in evaluations if e.covers_all]
        if angulaire and not any(e.meets_range for e in angulaire):
            return (
                "Aucune architecture n'atteint la portée demandée sur ces secteurs, "
                "même en couvrant tout l'angle : un objectif plus long, un capteur "
                "plus défini, ou une portée visée plus modeste sont nécessaires."
            )
        return (
            "Aucune architecture ne tient la contrainte de latence sur ces secteurs : "
            "réduire la portée visée, restreindre les secteurs, ou accepter une "
            "revisite plus longue."
        )
    best = min(viable, key=lambda e: e.cost_usd)
    return (
        f"{best.name} — {best.n_cameras} caméra(s), "
        f"portée {best.detection_range_m / 1000:.1f} km, "
        f"latence moyenne {best.mean_latency_s / 60:.1f} min, "
        f"{best.cost_usd:.0f} USD de matériel."
    )
