"""Association robuste détection → piste/séquence.

Ce module répond au défaut d'association le plus coûteux observé en production
sur une stack de détection de fumée : **le premier candidat qui chevauche
gagne**, quelle que soit la qualité de ce chevauchement.

## Le mode de défaillance

Le schéma fautif, tel qu'on le rencontre, tient en trois décisions qui
s'aggravent mutuellement :

1. **les candidats sont parcourus par récence** (`last_seen_at` décroissant),
   donc l'ordre d'examen n'a rien à voir avec la qualité spatiale ;
2. **le test de correspondance est booléen** — un chevauchement d'un pixel vaut
   autant qu'un recouvrement parfait ;
3. **on s'arrête au premier candidat qui passe** (`break`).

Conséquence : deux feux visibles sur la même pose, une boîte anormalement
grande sur l'un d'eux, et cette boîte « vole » les détections de l'autre. Comme
l'identité spatiale d'une séquence est portée par sa **dernière** boîte, le vol
est irréversible : la séquence volante s'élargit encore, et absorbe tout. Un
incident de ce type a duré 2 h 30 en production, avec des images ne
correspondant plus aux positions triangulées présentées à l'opérateur.

Une fenêtre d'association longue (deux heures est une valeur observée) aggrave
encore le problème : une seule intersection d'un pixel peut relier des épisodes
séparés de plusieurs heures.

## Ce que ce module change

- **Score continu** plutôt que booléen : IoU, distance des centres, rapport de
  tailles et écart temporel, combinés en une qualité dans [0, 1].
- **Meilleur match global** plutôt que premier match : tous les candidats sont
  évalués, le meilleur gagne — et si deux candidats sont à égalité, on refuse
  plutôt que de trancher au hasard.
- **Garde anti-boîte-géante** : un candidat dont la surface est
  disproportionnée par rapport à la détection ne peut pas l'absorber.
- **État de piste robuste** : la position de référence est la médiane des
  dernières observations, pas la dernière boîte. Une boîte aberrante ne
  déplace donc plus durablement l'identité de la piste.
- **Découpage en épisodes** : au-delà d'un écart temporel, on ouvre une
  nouvelle piste au lieu de recoller deux événements distincts.

Le module est **pur** (aucune dépendance à une base, un ORM ou un réseau) pour
rester réutilisable et testable ailleurs — y compris en le proposant en amont.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# Boîte relative (x_min, y_min, x_max, y_max) dans [0, 1].
Box = tuple[float, float, float, float]


# --------------------------------------------------------------------------- #
# Primitives géométriques
# --------------------------------------------------------------------------- #
def box_area(box: Box) -> float:
    x0, y0, x1, y1 = box
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def box_center(box: Box) -> tuple[float, float]:
    x0, y0, x1, y1 = box
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def box_diagonal(box: Box) -> float:
    x0, y0, x1, y1 = box
    return math.hypot(max(0.0, x1 - x0), max(0.0, y1 - y0))


def box_iou(a: Box, b: Box) -> float:
    """Intersection sur union. 0 si disjointes."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = box_area(a) + box_area(b) - inter
    return float(inter / union) if union > 0 else 0.0


def box_coverage(inner: Box, outer: Box) -> float:
    """Fraction de ``inner`` contenue dans ``outer``.

    Distincte de l'IoU : une petite boîte entièrement incluse dans une grande a
    une couverture de 1 mais une IoU faible. C'est précisément la situation
    d'une boîte géante qui « avale » une détection légitime, et c'est pourquoi
    les deux grandeurs sont nécessaires.
    """
    ix0, iy0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix1, iy1 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    area = box_area(inner)
    return float(((ix1 - ix0) * (iy1 - iy0)) / area) if area > 0 else 0.0


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class AssociationConfig:
    """Paramètres de l'association. Tous documentés, aucun magique.

    Les valeurs par défaut sont volontairement conservatrices : en cas de doute,
    ouvrir une nouvelle piste coûte un doublon à l'opérateur, tandis qu'une
    association erronée lui montre les images d'un feu à la position d'un autre.
    Le second est bien plus grave.
    """

    # Poids de la fonction de coût (normalisés à la somme).
    w_iou: float = 0.5
    w_center: float = 0.3
    w_size: float = 0.2

    min_quality: float = 0.15
    """Qualité minimale pour associer. En dessous, on ouvre une piste."""

    ambiguity_margin: float = 0.05
    """Si les deux meilleurs candidats sont à moins de cette marge, on refuse.

    Trancher au hasard entre deux pistes plausibles est exactement ce qui
    produit un vol de détections : mieux vaut une nouvelle piste, quitte à
    fusionner plus tard sur des preuves plus solides.
    """

    max_gap_s: float = 900.0
    """Écart temporel au-delà duquel on considère deux épisodes distincts.

    Une fenêtre de deux heures permet de relier des événements sans rapport ;
    15 minutes est un compromis défendable pour une fumée qui persiste.
    """

    max_area_ratio: float = 8.0
    """Garde anti-boîte-géante : rapport de surface maximal toléré.

    Au-delà, le candidat est trop gros par rapport à la détection pour qu'il
    s'agisse du même panache observé au même instant.
    """

    max_center_travel: float = 0.25
    """Déplacement maximal plausible du centre entre deux observations,
    en fraction de la diagonale de l'image."""

    history_window: int = 5
    """Nombre d'observations utilisées pour la position de référence médiane."""

    def __post_init__(self) -> None:
        total = self.w_iou + self.w_center + self.w_size
        if total <= 0:
            raise ValueError("la somme des poids doit être > 0")
        if not 0.0 <= self.min_quality <= 1.0:
            raise ValueError("min_quality doit être dans [0, 1]")
        if self.max_area_ratio <= 1.0:
            raise ValueError("max_area_ratio doit être > 1")


# --------------------------------------------------------------------------- #
# Piste
# --------------------------------------------------------------------------- #
@dataclass
class TrackState:
    """État d'une piste, robuste aux boîtes aberrantes.

    L'identité spatiale est portée par la **médiane** des dernières boîtes, non
    par la dernière. C'est le correctif qui rend un vol de détections
    réversible : une seule boîte anormale ne déplace plus la piste.
    """

    track_id: str
    boxes: list[Box] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    history_window: int = 5

    def add(self, box: Box, t: float) -> None:
        self.boxes.append(box)
        self.timestamps.append(t)

    @property
    def last_seen(self) -> float:
        return self.timestamps[-1] if self.timestamps else float("-inf")

    @property
    def last_box(self) -> Box | None:
        return self.boxes[-1] if self.boxes else None

    @property
    def reference_box(self) -> Box | None:
        """Boîte de référence : médiane coordonnée par coordonnée.

        La médiane est choisie plutôt que la moyenne parce qu'une seule boîte
        géante suffirait à tirer une moyenne, ce qui est précisément le défaut
        qu'on corrige.
        """
        if not self.boxes:
            return None
        window = self.boxes[-self.history_window :]
        arr = np.asarray(window, dtype=float)
        med = np.median(arr, axis=0)
        return (float(med[0]), float(med[1]), float(med[2]), float(med[3]))

    @property
    def median_area(self) -> float:
        if not self.boxes:
            return 0.0
        return float(np.median([box_area(b) for b in self.boxes[-self.history_window :]]))


# --------------------------------------------------------------------------- #
# Score
# --------------------------------------------------------------------------- #
@dataclass
class MatchScore:
    """Résultat détaillé d'une évaluation, explicable à un opérateur."""

    track_id: str
    quality: float
    iou: float
    center_distance: float
    size_ratio: float
    time_gap_s: float
    rejected_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.rejected_reason is None

    def as_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "quality": round(self.quality, 4),
            "iou": round(self.iou, 4),
            "center_distance": round(self.center_distance, 4),
            "size_ratio": round(self.size_ratio, 3),
            "time_gap_s": round(self.time_gap_s, 1),
            "rejected_reason": self.rejected_reason,
        }


def score_match(
    track: TrackState,
    box: Box,
    t: float,
    cfg: AssociationConfig | None = None,
) -> MatchScore:
    """Évalue la qualité d'une association candidate.

    Renvoie toujours un ``MatchScore`` — y compris en cas de rejet, avec le
    motif. Un rejet silencieux serait indébuggable en production, où c'est
    précisément l'explication du choix qui manque quand une association se
    révèle fausse.
    """
    cfg = cfg or AssociationConfig()
    ref = track.reference_box
    if ref is None:
        return MatchScore(track.track_id, 0.0, 0.0, 1.0, 0.0, 0.0, "piste sans observation")

    gap = t - track.last_seen
    iou = box_iou(ref, box)

    cx_ref, cy_ref = box_center(ref)
    cx, cy = box_center(box)
    # Distance normalisée par la diagonale de l'image (coordonnées relatives),
    # ce qui rend le critère indépendant de la résolution.
    center_distance = math.hypot(cx - cx_ref, cy - cy_ref) / math.sqrt(2.0)

    area_ref = max(track.median_area, 1e-9)
    area_box = max(box_area(box), 1e-9)
    size_ratio = max(area_ref / area_box, area_box / area_ref)

    # --- rejets durs, dans l'ordre de gravité --------------------------------
    if gap < 0:
        return MatchScore(track.track_id, 0.0, iou, center_distance, size_ratio, gap,
                          "détection antérieure à la dernière observation de la piste")
    if gap > cfg.max_gap_s:
        return MatchScore(track.track_id, 0.0, iou, center_distance, size_ratio, gap,
                          f"écart de {gap / 60:.0f} min : épisodes distincts")
    if size_ratio > cfg.max_area_ratio:
        return MatchScore(track.track_id, 0.0, iou, center_distance, size_ratio, gap,
                          f"rapport de surface {size_ratio:.1f}× : boîte disproportionnée")
    if center_distance > cfg.max_center_travel:
        return MatchScore(track.track_id, 0.0, iou, center_distance, size_ratio, gap,
                          f"déplacement du centre de {center_distance:.2f} : trop rapide")

    # --- qualité continue ----------------------------------------------------
    s_iou = iou
    s_center = max(0.0, 1.0 - center_distance / max(cfg.max_center_travel, 1e-9))
    s_size = 1.0 / size_ratio          # 1 si tailles identiques, décroît ensuite
    total_w = cfg.w_iou + cfg.w_center + cfg.w_size
    quality = (cfg.w_iou * s_iou + cfg.w_center * s_center + cfg.w_size * s_size) / total_w

    reason = None if quality >= cfg.min_quality else f"qualité {quality:.3f} sous le seuil"
    return MatchScore(track.track_id, quality, iou, center_distance, size_ratio, gap, reason)


# --------------------------------------------------------------------------- #
# Décision
# --------------------------------------------------------------------------- #
@dataclass
class AssociationResult:
    """Décision d'association, avec l'ensemble des candidats évalués."""

    matched_track_id: str | None
    scores: list[MatchScore] = field(default_factory=list)
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.matched_track_id is not None

    @property
    def best(self) -> MatchScore | None:
        accepted = [s for s in self.scores if s.accepted]
        return max(accepted, key=lambda s: s.quality) if accepted else None

    def as_dict(self) -> dict:
        return {
            "matched_track_id": self.matched_track_id,
            "reason": self.reason,
            "scores": [s.as_dict() for s in self.scores],
        }


def associate_detection(
    tracks: list[TrackState],
    box: Box,
    t: float,
    cfg: AssociationConfig | None = None,
) -> AssociationResult:
    """Associe **une** détection à la meilleure piste, ou à aucune.

    C'est la forme directement transposable à un flux qui traite les détections
    une par une. Trois différences avec un `first-match-wins` :

    1. tous les candidats sont évalués, aucun `break` prématuré ;
    2. la comparaison est une qualité continue, pas un booléen ;
    3. une égalité entre les deux meilleurs candidats conduit à un refus
       explicite plutôt qu'à un choix arbitraire.
    """
    cfg = cfg or AssociationConfig()
    scores = [score_match(tr, box, t, cfg) for tr in tracks]
    accepted = sorted((s for s in scores if s.accepted), key=lambda s: s.quality, reverse=True)

    if not accepted:
        return AssociationResult(None, scores, "aucun candidat recevable")

    best = accepted[0]
    if len(accepted) > 1:
        runner_up = accepted[1]
        if best.quality - runner_up.quality < cfg.ambiguity_margin:
            return AssociationResult(
                None, scores,
                f"ambiguïté entre {best.track_id} ({best.quality:.3f}) et "
                f"{runner_up.track_id} ({runner_up.quality:.3f}) : nouvelle piste",
            )
    return AssociationResult(best.track_id, scores, f"meilleur score {best.quality:.3f}")


def assign_batch(
    tracks: list[TrackState],
    detections: list[tuple[Box, float]],
    cfg: AssociationConfig | None = None,
) -> dict[int, str]:
    """Affectation globale d'un lot de détections aux pistes.

    Quand plusieurs détections arrivent simultanément (cas d'une image
    multi-boîtes), les traiter une par une peut produire un ordre-dépendance :
    la première détection prend la meilleure piste, la seconde se rabat sur une
    piste médiocre alors qu'un autre appariement global aurait été meilleur pour
    les deux.

    On résout donc l'affectation globalement, par choix glouton sur les scores
    **triés toutes paires confondues** — équivalent à une affectation optimale
    dans l'écrasante majorité des cas réels (peu de pistes, peu de détections),
    sans introduire de dépendance à un solveur.

    Renvoie ``{index de détection: track_id}`` ; les détections absentes du
    dictionnaire n'ont pas été associées.
    """
    cfg = cfg or AssociationConfig()
    triples: list[tuple[float, int, str]] = []
    for i, (box, t) in enumerate(detections):
        for tr in tracks:
            s = score_match(tr, box, t, cfg)
            if s.accepted:
                triples.append((s.quality, i, tr.track_id))
    triples.sort(key=lambda x: x[0], reverse=True)

    out: dict[int, str] = {}
    used_tracks: set[str] = set()
    for _quality, det_index, track_id in triples:
        if det_index in out or track_id in used_tracks:
            continue
        out[det_index] = track_id
        used_tracks.add(track_id)
    return out


# --------------------------------------------------------------------------- #
# Comparaison avec la logique historique — pour mesurer le gain
# --------------------------------------------------------------------------- #
def boxes_overlap_legacy(a: Box, b: Box, tolerance: float = 0.05) -> bool:
    """Test booléen historique : chevauchement, avec tolérance de jeu.

    Reproduit fidèlement la logique qu'on corrige, afin de pouvoir démontrer la
    différence sur un même scénario plutôt que de l'affirmer.
    """
    inter_w = min(a[2], b[2]) - max(a[0], b[0])
    inter_h = min(a[3], b[3]) - max(a[1], b[1])
    return inter_w > -tolerance and inter_h > -tolerance


def associate_legacy(
    tracks: list[TrackState],
    box: Box,
    t: float,
    tolerance: float = 0.05,
) -> str | None:
    """Association historique : premier candidat qui chevauche, par récence.

    Les candidats sont examinés du plus récemment vu au plus ancien, et le
    premier qui chevauche l'emporte — sans mesure de qualité.
    """
    for tr in sorted(tracks, key=lambda x: x.last_seen, reverse=True):
        last = tr.last_box
        if last is not None and boxes_overlap_legacy(last, box, tolerance):
            return tr.track_id
    return None
