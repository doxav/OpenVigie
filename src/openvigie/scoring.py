"""Fusion des features et décision.

Deux principes :

1. **Pas de somme pondérée devinée.** Le modèle est une régression logistique
   dont les coefficients sont *appris* sur les données du site (voir
   ``fit_logistic``) et dont la sortie est une probabilité calibrée. Tant qu'on
   n'a pas de données, les coefficients par défaut ci-dessous encodent
   simplement des priorités raisonnables — et le fichier de config indique
   explicitement qu'ils sont provisoires.

2. **Le seuil se règle sur le taux de fausses alertes, pas sur le F1.** Un SDIS
   abandonne un système à ~2 fausses alertes/caméra/jour. ``threshold_for_fpr``
   choisit donc le seuil à partir d'un budget de FP/jour mesuré sur les négatifs
   réels du site.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .tracking import TrackFeatures

# Ordre canonique des features. Toute modification casse la compatibilité des
# poids sauvegardés : la version est donc explicite.
FEATURE_ORDER = (
    "persistence",
    "growth_score",
    "upward_score",
    "ground_origin",
    "contrast_loss",
    "translucency",
    "wind_coherence",
    "cnn_score",
)

FEATURE_SCHEMA_VERSION = 1

# Priors provisoires, à remplacer par un fit sur données de site.
DEFAULT_WEIGHTS: dict[str, float] = {
    "persistence": 1.6,
    "growth_score": 1.8,
    "upward_score": 1.1,
    "ground_origin": 2.2,
    "contrast_loss": 0.9,
    "translucency": 0.6,
    "wind_coherence": 0.7,
    "cnn_score": 2.4,
}
DEFAULT_BIAS = -4.5


@dataclass
class FusionModel:
    """Régression logistique sur les features de piste."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    bias: float = DEFAULT_BIAS
    fitted: bool = False
    schema_version: int = FEATURE_SCHEMA_VERSION

    def vector(self, feats: TrackFeatures) -> np.ndarray:
        return np.array([getattr(feats, name) for name in FEATURE_ORDER], dtype=np.float64)

    def logit(self, feats: TrackFeatures) -> float:
        w = np.array([self.weights.get(n, 0.0) for n in FEATURE_ORDER])
        return float(self.vector(feats) @ w + self.bias)

    def score(self, feats: TrackFeatures) -> float:
        """Probabilité dans [0, 1]."""
        z = self.logit(feats)
        z = min(max(z, -60.0), 60.0)
        return 1.0 / (1.0 + math.exp(-z))

    def veto(self, feats: TrackFeatures, require_ground_origin: bool = True) -> str | None:
        """Règles de rejet dur, appliquées avant le score.

        Un candidat au-dessus de l'horizon est un nuage, quelle que soit la
        probabilité du réseau. On garde ce test hors du modèle appris pour qu'il
        soit auditable et non contournable par un sur-apprentissage.
        """
        if require_ground_origin and feats.ground_origin < 0.5:
            return "au-dessus de l'horizon (pas d'origine au sol)"
        if feats.growth_m2_s < -1e-6 and feats.persistence >= 0.75:
            return "surface décroissante sur plusieurs revisites"
        return None

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "bias": self.bias,
            "fitted": self.fitted,
            "weights": dict(self.weights),
        }

    @classmethod
    def from_dict(cls, d: dict) -> FusionModel:
        version = int(d.get("schema_version", FEATURE_SCHEMA_VERSION))
        if version != FEATURE_SCHEMA_VERSION:
            raise ValueError(
                f"poids de fusion en version {version}, attendu {FEATURE_SCHEMA_VERSION} : réentraîner"
            )
        return cls(
            weights=dict(d.get("weights", DEFAULT_WEIGHTS)),
            bias=float(d.get("bias", DEFAULT_BIAS)),
            fitted=bool(d.get("fitted", False)),
            schema_version=version,
        )


def fit_logistic(
    X: np.ndarray, y: np.ndarray, epochs: int = 400, lr: float = 0.2, l2: float = 1e-3
) -> FusionModel:
    """Descente de gradient simple, sans dépendance scikit-learn.

    Volontairement minimal : sur ce problème le nombre de features est petit et
    le nombre d'exemples étiquetés (les alertes validées/invalidées par les
    opérateurs) se compte en milliers, pas en millions.
    """
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64).ravel()
    if X.ndim != 2 or X.shape[1] != len(FEATURE_ORDER):
        raise ValueError(f"X doit être de forme (n, {len(FEATURE_ORDER)})")
    if X.shape[0] != y.shape[0]:
        raise ValueError("X et y de tailles incompatibles")

    w = np.zeros(X.shape[1])
    b = 0.0
    n = X.shape[0]
    for _ in range(epochs):
        z = np.clip(X @ w + b, -60, 60)
        p = 1.0 / (1.0 + np.exp(-z))
        err = p - y
        w -= lr * ((X.T @ err) / n + l2 * w)
        b -= lr * err.mean()
    return FusionModel(
        weights={name: float(w[i]) for i, name in enumerate(FEATURE_ORDER)},
        bias=float(b),
        fitted=True,
    )


def threshold_for_fp_budget(
    negative_scores: np.ndarray, observation_days: float, fp_per_day_budget: float = 1.0
) -> float:
    """Seuil satisfaisant un budget de faux positifs par jour et par caméra.

    ``negative_scores`` = scores obtenus sur le jeu de négatifs continus du site
    (30 jours, 24/24). C'est ce jeu, et non le jeu positif, qui fixe le seuil.
    """
    scores = np.sort(np.asarray(negative_scores, dtype=np.float64))[::-1]
    if scores.size == 0:
        return 0.5
    allowed = max(0, int(round(fp_per_day_budget * max(observation_days, 1e-9))))
    if allowed >= scores.size:
        return float(scores[-1]) * 0.999
    if allowed == 0:
        return float(min(1.0, scores[0] + 1e-6))
    return float(scores[allowed - 1])


@dataclass
class DecisionConfig:
    """Hystérésis : entrer en alerte est plus difficile qu'y rester."""

    enter_threshold: float = 0.75
    exit_threshold: float = 0.45
    min_persistence_visits: int = 3
    require_ground_origin: bool = True
    require_growth: bool = True


def decide(
    state: str,
    score: float,
    feats: TrackFeatures,
    n_visits: int,
    cfg: DecisionConfig | None = None,
    model: FusionModel | None = None,
) -> tuple[str, str | None]:
    """Machine à états. Renvoie ``(nouvel_état, motif)``.

    États : NEW -> CANDIDATE -> CONFIRMED -> ALERTED, ou DISMISSED.
    """
    cfg = cfg or DecisionConfig()
    model = model or FusionModel()

    reason = model.veto(feats, cfg.require_ground_origin)
    if reason:
        return "DISMISSED", reason

    if cfg.require_growth and n_visits >= cfg.min_persistence_visits and feats.growth_m2_s <= 0:
        return "DISMISSED", "pas de croissance après plusieurs revisites"

    if state in ("ALERTED", "CONFIRMED"):
        if score < cfg.exit_threshold:
            return "CANDIDATE", "score retombé sous le seuil de maintien"
        return ("ALERTED" if state == "ALERTED" else "CONFIRMED"), None

    if score >= cfg.enter_threshold and n_visits >= cfg.min_persistence_visits:
        return "CONFIRMED", "seuil franchi avec persistance suffisante"
    if score >= cfg.exit_threshold:
        return "CANDIDATE", None
    return ("CANDIDATE" if state == "CANDIDATE" else "NEW"), None
