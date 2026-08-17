"""Métriques opérationnelles et garde de non-régression.

Ce module répond à une faiblesse structurelle de l'évaluation des détecteurs de
fumée : **un bon F1 peut masquer un système inutilisable sur le terrain**, et
un nouveau modèle peut être moins bon que l'ancien sans que le classement
principal le montre.

## Pourquoi le F1 induit en erreur ici

Le F1 pondère identiquement un faux positif sur une image de fond et une fumée
manquée. Or les deux n'ont ni le même coût ni la même fréquence :

- **le coût** est asymétrique — une fumée manquée peut coûter un massif, un
  faux positif coûte une vérification à un opérateur ;
- **la fréquence** l'est encore plus — un jeu de test équilibre grossièrement
  fond et fumée, alors qu'en exploitation une caméra produit des milliers
  d'images de fond par départ de feu.

Conséquence : un taux de faux positifs qui paraît acceptable sur un benchmark
devient ingérable une fois multiplié par le nombre réel d'images par jour.
``fpr_to_fp_per_camera_per_day`` fait cette conversion, et c'est probablement
la fonction la plus utile du module — elle transforme un nombre abstrait en
une charge de travail que quelqu'un devra assumer.

## Ce que ce module mesure à la place

Trois familles, toutes exprimées dans des unités qu'un service opérationnel
peut discuter :

1. **charge** — faux positifs par caméra et par jour, mesurés au niveau
   séquence, c'est-à-dire après filtrage temporel ;
2. **délai** — temps de détection depuis l'ignition, en **médiane et p90**
   plutôt qu'en moyenne : la distribution est fortement asymétrique, et c'est
   sa queue qui fait perdre un massif ;
3. **couverture stratifiée** — rappel par taille de panache, distance et
   visibilité. Un rappel global élevé peut parfaitement cacher que les petites
   fumées lointaines, c'est-à-dire les départs précoces, sont toutes manquées.

## Garde de non-régression

Une nouvelle version de modèle doit être refusée si elle régresse sur **une
seule** strate, même si sa métrique agrégée s'améliore. C'est le cas
explicitement visé : améliorer le F1 en gagnant sur les grosses fumées proches
tout en perdant sur les petites lointaines est une régression opérationnelle
déguisée en progrès.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


# --------------------------------------------------------------------------- #
# Conversion benchmark → exploitation
# --------------------------------------------------------------------------- #
def frames_per_camera_per_day(
    poses_per_camera: int = 4,
    seconds_per_pose: float = 30.0,
    duty_hours_per_day: float = 24.0,
) -> float:
    """Nombre d'images analysées par caméra et par jour.

    Une caméra PTZ qui parcourt ``poses_per_camera`` positions en s'arrêtant
    ``seconds_per_pose`` sur chacune produit une image par pose et par cycle.
    Le nombre d'images ne dépend donc que de la cadence, pas du nombre de poses
    — ces dernières se partagent le même temps.
    """
    if seconds_per_pose <= 0 or duty_hours_per_day <= 0 or poses_per_camera < 1:
        raise ValueError("paramètres de cadence invalides")
    return duty_hours_per_day * 3600.0 / seconds_per_pose


def fpr_to_fp_per_camera_per_day(fpr: float, frames_per_day: float) -> float:
    """Traduit un taux de faux positifs **par image** en activations quotidiennes.

    Un FPR de 0,05 semble anodin ; à 2 880 images par caméra et par jour, il
    vaut 144 activations quotidiennes **par caméra**.

    ⚠️ **Ce n'est PAS le nombre d'alertes vues par un opérateur**, et le
    confondre serait une erreur grossière. Un moteur de détection sérieux
    applique un lissage temporel — fenêtre glissante, vote majoritaire sur des
    détections spatialement cohérentes — qui supprime l'essentiel de ces
    activations avant qu'elles ne deviennent des alertes.

    Ce que cette conversion mesure réellement, c'est la **charge d'entrée du
    filtre temporel**. Elle reste utile pour deux raisons :

    - elle borne ce que le filtre doit absorber, et dit à quel point on dépend
      de lui ;
    - elle rend comparables des FPR mesurés sur des jeux de tailles
      différentes.

    Pour la charge réellement subie, mesurer au niveau **séquence**, après
    filtrage : c'est ce que fait ``compute_operational_metrics``.
    """
    if not 0.0 <= fpr <= 1.0:
        raise ValueError("fpr doit être dans [0, 1]")
    if frames_per_day < 0:
        raise ValueError("frames_per_day doit être >= 0")
    return fpr * frames_per_day


def fp_budget_to_max_fpr(fp_per_day_budget: float, frames_per_day: float) -> float:
    """Taux de faux positifs maximal admissible pour tenir un budget.

    L'inverse de la fonction précédente, et la façon dont un seuil devrait être
    choisi : partir de ce que l'exploitant accepte, pas de ce que la courbe ROC
    propose.
    """
    if frames_per_day <= 0:
        raise ValueError("frames_per_day doit être > 0")
    return max(0.0, fp_per_day_budget / frames_per_day)


def temporal_survival_probability(
    p_per_frame: float, window: int = 8, min_hits: int | None = None
) -> float:
    """Probabilité qu'un faux positif **persistant** franchisse le filtre temporel.

    Modélise le lissage par fenêtre glissante employé par les moteurs de
    détection : sur ``window`` images, il faut qu'au moins ``min_hits``
    contiennent une détection spatialement cohérente (par défaut la majorité,
    ``(window + 1) // 2``).

    Le résultat éclaire une asymétrie décisive, et souvent implicite :

    - **contre un faux positif erratique** — un artefact qui apparaît à un
      endroit différent à chaque image — le filtre est quasi parfait, puisque
      la cohérence spatiale n'est jamais atteinte ;
    - **contre un faux positif persistant** — banc de brouillard, panache
      industriel, poussière, nuage stationnaire — il ne sert presque à rien :
      la structure est là image après image, au même endroit.

    Or ce sont précisément les faux positifs persistants qui coûtent cher en
    exploitation. Un taux de faux positifs élevé mesuré sur benchmark n'est
    donc **pas** rassurant sous prétexte qu'un filtre temporel suit : tout
    dépend de la **fraction persistante**, que le FPR seul ne dit pas.

    C'est la raison pour laquelle la charge doit se mesurer au niveau séquence
    et pas se déduire du FPR.
    """
    if not 0.0 <= p_per_frame <= 1.0:
        raise ValueError("p_per_frame doit être dans [0, 1]")
    if window < 1:
        raise ValueError("window doit être >= 1")
    k = (window + 1) // 2 if min_hits is None else min_hits
    if not 0 <= k <= window:
        raise ValueError("min_hits doit être dans [0, window]")

    # Survie binomiale : P(X >= k) avec X ~ Bin(window, p).
    total = 0.0
    for i in range(k, window + 1):
        total += math.comb(window, i) * (p_per_frame ** i) * ((1.0 - p_per_frame) ** (window - i))
    return float(min(1.0, max(0.0, total)))


def temporal_suppression_factor(
    raw_activations_per_day: float, observed_alerts_per_day: float
) -> float:
    """Rapport entre activations brutes et alertes réellement émises.

    Mesure à quel point le système **dépend** de son filtre temporel. Un
    facteur de 1 000 signifie que le filtre absorbe 99,9 % des activations :
    la performance apparente repose alors sur lui, et non sur le détecteur.

    C'est une dépendance fragile, parce que le filtre est inefficace contre les
    faux positifs persistants — ceux-là mêmes qui dominent en exploitation. Un
    facteur très élevé mérite donc d'être surveillé plutôt que célébré.
    """
    if observed_alerts_per_day <= 0:
        return float("inf") if raw_activations_per_day > 0 else 1.0
    return raw_activations_per_day / observed_alerts_per_day


# --------------------------------------------------------------------------- #
# Délai de détection
# --------------------------------------------------------------------------- #
@dataclass
class SequenceOutcome:
    """Résultat d'une séquence annotée, du point de vue opérationnel."""

    sequence_id: str
    is_wildfire: bool
    ignition_at: float | None = None
    first_alert_at: float | None = None
    # Strates : renseigner ce qui est disponible, les absentes sont ignorées.
    plume_size_px: float | None = None
    distance_m: float | None = None
    visibility_m: float | None = None
    camera_id: str = ""

    @property
    def detected(self) -> bool:
        return self.first_alert_at is not None

    @property
    def time_to_detect_s(self) -> float | None:
        """Délai entre l'ignition et la première alerte.

        ``None`` si le feu n'a pas été détecté ou si l'instant d'ignition est
        inconnu — cas fréquent et qu'il ne faut pas combler par une valeur par
        défaut, sous peine de fausser la médiane.
        """
        if self.ignition_at is None or self.first_alert_at is None:
            return None
        return max(0.0, self.first_alert_at - self.ignition_at)


@dataclass
class DelayStats:
    """Distribution des délais de détection."""

    n: int
    median_s: float | None
    p90_s: float | None
    max_s: float | None
    n_undetected: int

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "median_min": round(self.median_s / 60.0, 2) if self.median_s is not None else None,
            "p90_min": round(self.p90_s / 60.0, 2) if self.p90_s is not None else None,
            "max_min": round(self.max_s / 60.0, 2) if self.max_s is not None else None,
            "n_undetected": self.n_undetected,
        }


def detection_delays(outcomes: list[SequenceOutcome]) -> DelayStats:
    """Statistiques de délai sur les feux réels.

    La p90 est rapportée en plus de la médiane parce que c'est la queue de
    distribution qui fait perdre un massif : un système dont la médiane est
    excellente mais qui met une heure une fois sur dix n'est pas fiable.
    """
    fires = [o for o in outcomes if o.is_wildfire]
    delays = [d for d in (o.time_to_detect_s for o in fires) if d is not None]
    undetected = sum(1 for o in fires if not o.detected)
    if not delays:
        return DelayStats(len(fires), None, None, None, undetected)
    arr = np.asarray(delays, dtype=float)
    return DelayStats(
        n=len(fires),
        median_s=float(np.median(arr)),
        p90_s=float(np.percentile(arr, 90)),
        max_s=float(arr.max()),
        n_undetected=undetected,
    )


# --------------------------------------------------------------------------- #
# Rappel stratifié
# --------------------------------------------------------------------------- #
DEFAULT_SIZE_BINS = ((0.0, 20.0), (20.0, 50.0), (50.0, 200.0), (200.0, math.inf))
DEFAULT_DISTANCE_BINS = ((0.0, 3000.0), (3000.0, 7000.0), (7000.0, 15000.0), (15000.0, math.inf))


def _bin_label(value: float, bins: tuple[tuple[float, float], ...], unit: str) -> str:
    for lo, hi in bins:
        if lo <= value < hi:
            hi_txt = "∞" if math.isinf(hi) else f"{hi:g}"
            return f"[{lo:g}, {hi_txt}) {unit}"
    return "hors bornes"


@dataclass
class StratumRecall:
    """Rappel sur une strate, avec son effectif."""

    stratum: str
    n: int
    detected: int

    @property
    def recall(self) -> float:
        return self.detected / self.n if self.n else 0.0

    def as_dict(self) -> dict:
        return {
            "stratum": self.stratum,
            "n": self.n,
            "detected": self.detected,
            "recall": round(self.recall, 4),
        }


def stratified_recall(
    outcomes: list[SequenceOutcome],
    by: str = "plume_size_px",
    bins: tuple[tuple[float, float], ...] | None = None,
    unit: str = "px",
) -> list[StratumRecall]:
    """Rappel ventilé par strate.

    C'est le remède au rappel global : celui-ci est dominé par les cas faciles
    (fumées grosses et proches), qui sont aussi les moins urgents à détecter.
    Un système utile doit être jugé sur les petites fumées lointaines.

    Les strates d'effectif nul sont omises ; celles d'effectif faible sont
    conservées mais leur ``n`` est exposé, parce qu'un rappel de 1,0 sur deux
    séquences ne veut rien dire et que le masquer serait pire.
    """
    if bins is None:
        bins = DEFAULT_SIZE_BINS if by == "plume_size_px" else DEFAULT_DISTANCE_BINS
    groups: dict[str, list[SequenceOutcome]] = {}
    for o in outcomes:
        if not o.is_wildfire:
            continue
        value = getattr(o, by, None)
        if value is None:
            continue
        groups.setdefault(_bin_label(float(value), bins, unit), []).append(o)

    out = [
        StratumRecall(label, len(items), sum(1 for i in items if i.detected))
        for label, items in groups.items()
    ]
    return sorted(out, key=lambda s: s.stratum)


# --------------------------------------------------------------------------- #
# Bilan opérationnel
# --------------------------------------------------------------------------- #
@dataclass
class OperationalMetrics:
    """Ce qu'un service opérationnel peut réellement discuter."""

    n_sequences: int
    n_fires: int
    n_detected_fires: int
    n_false_positive_sequences: int
    observation_days: float
    n_cameras: int
    delays: DelayStats
    strata: dict[str, list[StratumRecall]] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return self.n_detected_fires / self.n_fires if self.n_fires else 0.0

    @property
    def fp_per_camera_per_day(self) -> float:
        denom = self.observation_days * self.n_cameras
        return self.n_false_positive_sequences / denom if denom > 0 else float("inf")

    @property
    def worst_stratum(self) -> StratumRecall | None:
        """Strate la moins bien couverte, tous axes confondus.

        C'est le chiffre à regarder en premier : il dit où le système échoue,
        là où le rappel global dit seulement qu'il réussit en moyenne.
        """
        allstrata = [s for group in self.strata.values() for s in group if s.n > 0]
        return min(allstrata, key=lambda s: s.recall) if allstrata else None

    def as_dict(self) -> dict:
        return {
            "recall": round(self.recall, 4),
            "n_fires": self.n_fires,
            "n_detected_fires": self.n_detected_fires,
            "fp_per_camera_per_day": round(self.fp_per_camera_per_day, 3),
            "delays": self.delays.as_dict(),
            "worst_stratum": self.worst_stratum.as_dict() if self.worst_stratum else None,
            "strata": {k: [s.as_dict() for s in v] for k, v in self.strata.items()},
        }


def compute_operational_metrics(
    outcomes: list[SequenceOutcome],
    observation_days: float,
    n_cameras: int = 1,
    stratify: tuple[str, ...] = ("plume_size_px", "distance_m"),
) -> OperationalMetrics:
    """Bilan complet à partir de séquences annotées."""
    if observation_days <= 0:
        raise ValueError("observation_days doit être > 0")
    if n_cameras < 1:
        raise ValueError("n_cameras doit être >= 1")

    fires = [o for o in outcomes if o.is_wildfire]
    non_fires = [o for o in outcomes if not o.is_wildfire]
    strata: dict[str, list[StratumRecall]] = {}
    for axis in stratify:
        unit = "px" if axis.endswith("_px") else ("m" if axis.endswith("_m") else "")
        bins = DEFAULT_SIZE_BINS if axis == "plume_size_px" else DEFAULT_DISTANCE_BINS
        result = stratified_recall(outcomes, by=axis, bins=bins, unit=unit)
        if result:
            strata[axis] = result

    return OperationalMetrics(
        n_sequences=len(outcomes),
        n_fires=len(fires),
        n_detected_fires=sum(1 for o in fires if o.detected),
        n_false_positive_sequences=sum(1 for o in non_fires if o.detected),
        observation_days=observation_days,
        n_cameras=n_cameras,
        delays=detection_delays(outcomes),
        strata=strata,
    )


# --------------------------------------------------------------------------- #
# Garde de non-régression
# --------------------------------------------------------------------------- #
@dataclass
class GateViolation:
    """Une régression bloquante, avec le chiffre qui la motive."""

    kind: str
    detail: str
    baseline: float
    candidate: float

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "baseline": round(self.baseline, 4),
            "candidate": round(self.candidate, 4),
        }


@dataclass
class GateConfig:
    """Tolérances de la garde de release.

    Volontairement asymétriques : on tolère une petite dégradation de charge
    (des faux positifs en plus se gèrent), mais très peu sur le rappel et le
    délai, qui décident si un feu est vu et quand.
    """

    max_recall_drop: float = 0.02
    max_stratum_recall_drop: float = 0.05
    max_fp_per_day_increase: float = 0.25       # relatif
    max_median_delay_increase_s: float = 60.0
    min_stratum_size: int = 5
    """Effectif en dessous duquel une strate n'est pas opposable.

    Un rappel calculé sur deux séquences est du bruit ; bloquer une release
    dessus ferait perdre confiance dans la garde elle-même.
    """


def release_gate(
    baseline: OperationalMetrics,
    candidate: OperationalMetrics,
    cfg: GateConfig | None = None,
) -> list[GateViolation]:
    """Refuse une version qui régresse, même si son agrégat s'améliore.

    Le cas visé est précis : gagner du rappel sur les fumées grosses et proches
    tout en en perdant sur les petites lointaines améliore le rappel global et
    dégrade le système. Un contrôle strate par strate est le seul moyen de
    l'attraper.

    Renvoie la liste des violations — vide si la version peut passer.
    """
    cfg = cfg or GateConfig()
    violations: list[GateViolation] = []

    drop = baseline.recall - candidate.recall
    if drop > cfg.max_recall_drop:
        violations.append(GateViolation(
            "recall", f"rappel global en baisse de {drop:.3f}", baseline.recall, candidate.recall))

    base_fp = baseline.fp_per_camera_per_day
    cand_fp = candidate.fp_per_camera_per_day
    if math.isfinite(base_fp) and math.isfinite(cand_fp):
        allowed = base_fp * (1.0 + cfg.max_fp_per_day_increase)
        if cand_fp > allowed and cand_fp - base_fp > 1e-9:
            violations.append(GateViolation(
                "fp_per_day",
                f"charge portée de {base_fp:.2f} à {cand_fp:.2f} FP/caméra/jour",
                base_fp, cand_fp))

    b_med, c_med = baseline.delays.median_s, candidate.delays.median_s
    if b_med is not None and c_med is not None:
        if c_med - b_med > cfg.max_median_delay_increase_s:
            violations.append(GateViolation(
                "delay",
                f"délai médian allongé de {(c_med - b_med) / 60:.1f} min",
                b_med, c_med))

    # --- le contrôle qui attrape les régressions déguisées en progrès -------
    for axis, base_strata in baseline.strata.items():
        cand_by_label = {s.stratum: s for s in candidate.strata.get(axis, [])}
        for bs in base_strata:
            cs = cand_by_label.get(bs.stratum)
            if cs is None or bs.n < cfg.min_stratum_size or cs.n < cfg.min_stratum_size:
                continue
            stratum_drop = bs.recall - cs.recall
            if stratum_drop > cfg.max_stratum_recall_drop:
                violations.append(GateViolation(
                    "stratum_recall",
                    f"{axis} {bs.stratum} : rappel en baisse de {stratum_drop:.3f}",
                    bs.recall, cs.recall))
    return violations


def gate_report(violations: list[GateViolation]) -> str:
    """Message lisible, destiné à une sortie de CI."""
    if not violations:
        return "Aucune régression détectée : la version peut être proposée."
    lines = [f"{len(violations)} régression(s) bloquante(s) :"]
    lines += [f"  - [{v.kind}] {v.detail}" for v in violations]
    lines.append(
        "Une amélioration de la métrique agrégée ne compense pas une régression "
        "sur une strate : c'est précisément ce que cette garde protège."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Front de Pareto
# --------------------------------------------------------------------------- #
@dataclass
class ConfigPoint:
    """Une configuration évaluée, dans l'espace des compromis."""

    name: str
    recall: float
    fp_per_camera_per_day: float
    median_delay_s: float
    extra: dict = field(default_factory=dict)

    def dominates(self, other: ConfigPoint) -> bool:
        """Domination au sens de Pareto : au moins aussi bon partout, meilleur
        quelque part. Rappel à maximiser, charge et délai à minimiser."""
        at_least_as_good = (
            self.recall >= other.recall
            and self.fp_per_camera_per_day <= other.fp_per_camera_per_day
            and self.median_delay_s <= other.median_delay_s
        )
        strictly_better = (
            self.recall > other.recall
            or self.fp_per_camera_per_day < other.fp_per_camera_per_day
            or self.median_delay_s < other.median_delay_s
        )
        return at_least_as_good and strictly_better

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "recall": round(self.recall, 4),
            "fp_per_camera_per_day": round(self.fp_per_camera_per_day, 3),
            "median_delay_min": round(self.median_delay_s / 60.0, 2),
            **self.extra,
        }


def pareto_front(points: list[ConfigPoint]) -> list[ConfigPoint]:
    """Configurations non dominées.

    Il n'existe pas de « meilleur » réglage dans l'absolu : baisser le seuil
    gagne du rappel et coûte des faux positifs. Exposer le front laisse
    l'arbitrage à qui en assume les conséquences — un service opérationnel —
    plutôt que de le figer dans un classement unique.
    """
    return [p for p in points if not any(q.dominates(p) for q in points if q is not p)]


def select_under_budget(
    points: list[ConfigPoint],
    fp_per_day_budget: float,
    max_median_delay_s: float | None = None,
) -> ConfigPoint | None:
    """Meilleur rappel parmi les configurations qui tiennent les contraintes.

    C'est la façon opérationnelle de choisir : on fixe d'abord ce qu'on accepte
    de subir, puis on maximise ce qu'on veut obtenir.
    """
    eligible = [p for p in points if p.fp_per_camera_per_day <= fp_per_day_budget]
    if max_median_delay_s is not None:
        eligible = [p for p in eligible if p.median_delay_s <= max_median_delay_s]
    return max(eligible, key=lambda p: p.recall) if eligible else None
