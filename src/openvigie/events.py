"""Schéma d'événement et cycle de vie.

C'est le développement au meilleur rapport valeur/effort du projet après le
détecteur lui-même : figer tôt le format d'événement conditionne tout ce qui
vient ensuite — corrélation multi-tours, file d'attente hors ligne, portail
opérateur, apprentissage, et plus tard un adaptateur vers un système de gestion
opérationnelle.

Deux principes tenus ici :

1. **Le schéma reste neutre.** Aucun format métier externe (CISU, EMSI, EDXL)
   n'entre dans le cœur du logiciel. Un adaptateur se branchera dessus le jour
   où un partenariat le justifiera ; contaminer tout le code avec un format
   institutionnel avant d'avoir démontré la détection serait l'ordre inverse.

2. **L'incertitude est portée par l'événement.** Une alerte affirme une position
   *et* l'ellipse dans laquelle elle se trouve. Prétendre à une précision
   inexistante est le meilleur moyen de perdre la confiance d'un opérateur.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "1.0"

# --------------------------------------------------------------------------- #
# Cycle de vie
# --------------------------------------------------------------------------- #
CANDIDATE = "candidate"
CONFIRMED = "confirmed"
TRANSMITTED = "transmitted"
ACKNOWLEDGED = "acknowledged"
OPERATOR_VALIDATED = "operator_validated"
OPERATOR_REJECTED = "operator_rejected"
CLOSED = "closed"
EXPIRED = "expired"

STATES = (
    CANDIDATE, CONFIRMED, TRANSMITTED, ACKNOWLEDGED,
    OPERATOR_VALIDATED, OPERATOR_REJECTED, CLOSED, EXPIRED,
)

TERMINAL_STATES = (CLOSED, EXPIRED)

# Transitions autorisées. Volontairement explicite : une alerte perdue ou
# doublonnée dans un état incohérent est un incident opérationnel, pas un bug
# cosmétique.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    CANDIDATE: (CONFIRMED, EXPIRED, OPERATOR_REJECTED),
    CONFIRMED: (TRANSMITTED, OPERATOR_VALIDATED, OPERATOR_REJECTED, EXPIRED),
    TRANSMITTED: (ACKNOWLEDGED, OPERATOR_VALIDATED, OPERATOR_REJECTED, EXPIRED),
    ACKNOWLEDGED: (OPERATOR_VALIDATED, OPERATOR_REJECTED, EXPIRED),
    OPERATOR_VALIDATED: (CLOSED,),
    OPERATOR_REJECTED: (CLOSED,),
    CLOSED: (),
    EXPIRED: (),
}


class InvalidTransition(ValueError):
    """Transition d'état refusée par la machine à états."""


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, ())


# Typologie des décisions opérateur. Chaque motif de rejet devient une classe
# de négatifs pour le réentraînement : c'est la boucle d'apprentissage la plus
# rentable du projet.
OPERATOR_DECISIONS = (
    "fire",                 # feu réel
    "prescribed_burn",      # brûlage dirigé / écobuage
    "agricultural_burn",    # sarments, déchets verts
    "dust",                 # poussière (moisson, piste, engin)
    "pollen",
    "cloud",
    "fog",
    "haze",
    "industrial",           # aéroréfrigérant, scierie, cimenterie
    "light",                # phares, balisage, feux d'artifice
    "optical_artifact",     # hublot sale, insecte, goutte, givre
    "camera_motion",        # vibration du mât, dérive de preset
    "already_known",        # feu déjà en cours de traitement
    "unknown",
)

POSITIVE_DECISIONS = ("fire",)


# --------------------------------------------------------------------------- #
# Localisation et incertitude
# --------------------------------------------------------------------------- #
@dataclass
class Uncertainty:
    """Ellipse d'incertitude au sol, en mètres.

    ``semi_major_m`` porte l'erreur en distance (dominante pour une tour seule),
    ``semi_minor_m`` l'erreur transversale (liée à la précision d'azimut),
    ``orientation_deg`` l'azimut du grand axe.
    """

    semi_major_m: float
    semi_minor_m: float
    orientation_deg: float

    @property
    def area_m2(self) -> float:
        return math.pi * self.semi_major_m * self.semi_minor_m

    def as_dict(self) -> dict:
        return {
            "semi_major_m": round(self.semi_major_m, 1),
            "semi_minor_m": round(self.semi_minor_m, 1),
            "orientation_deg": round(self.orientation_deg, 1),
            "area_m2": round(self.area_m2),
        }


def bearing_uncertainty(
    distance_m: float,
    bearing_sigma_deg: float = 0.5,
    range_relative_sigma: float = 0.25,
) -> Uncertainty:
    """Incertitude d'une localisation par relèvement + intersection terrain.

    L'erreur transversale suit directement l'incertitude d'azimut (calibration,
    dérive de preset, centroïde du panache). L'erreur en distance est bien plus
    grande : elle dépend de la pente du terrain à l'intersection, et 25 % est un
    ordre de grandeur prudent pour un relief modéré. C'est précisément pourquoi
    une deuxième tour change tout.
    """
    cross = distance_m * math.radians(max(bearing_sigma_deg, 1e-6))
    along = distance_m * max(range_relative_sigma, 1e-6)
    return Uncertainty(semi_major_m=along, semi_minor_m=cross, orientation_deg=0.0)


def triangulation_uncertainty(
    d1_m: float, d2_m: float, crossing_angle_deg: float, bearing_sigma_deg: float = 0.5
) -> Uncertainty:
    """Incertitude d'une intersection de deux relèvements.

    Deux relèvements qui se croisent à angle droit donnent une ellipse quasi
    circulaire ; deux relèvements presque colinéaires donnent une ellipse très
    allongée — et une confiance trompeuse si on ne la calcule pas.
    """
    theta = math.radians(max(abs(crossing_angle_deg), 1.0))
    s = math.radians(max(bearing_sigma_deg, 1e-6))
    e1, e2 = d1_m * s, d2_m * s
    denom = max(math.sin(theta), 1e-6)
    semi_major = math.hypot(e1, e2) / denom
    semi_minor = min(e1, e2)
    return Uncertainty(
        semi_major_m=semi_major,
        semi_minor_m=min(semi_minor, semi_major),
        orientation_deg=0.0,
    )


# --------------------------------------------------------------------------- #
# Événement
# --------------------------------------------------------------------------- #
@dataclass
class DetectionEvent:
    """Événement canonique. Un seul objet traverse tout le système."""

    event_id: str
    site_id: str
    camera_id: str
    detected_at: str                     # ISO 8601 UTC — horodatage de détection
    event_type: str = "wildfire_smoke_candidate"
    state: str = CANDIDATE
    schema_version: str = SCHEMA_VERSION

    # géométrie
    bearing_deg: float = 0.0
    distance_m: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float | None = None
    localization_method: str = "bearing_only"   # bearing_only | dem_intersect | triangulated
    uncertainty: dict | None = None

    # scores
    ai_probability: float = 0.0
    physical_score: float = 0.0
    fused_score: float = 0.0
    features: dict = field(default_factory=dict)

    # corroboration
    tower_votes: list[str] = field(default_factory=list)
    ptz_confirmed: bool = False
    n_visits: int = 0

    # preuves
    image_urls: list[str] = field(default_factory=list)
    sequence_urls: list[str] = field(default_factory=list)
    bbox: list[int] = field(default_factory=list)

    # contexte
    weather: dict = field(default_factory=dict)
    visibility_m: float | None = None

    # traçabilité
    model_version: str = "unknown"
    pipeline_tier: str = "unknown"
    software_version: str = "unknown"

    # workflow
    history: list[dict] = field(default_factory=list)
    operator_decision: str | None = None
    operator_comment: str | None = None
    transmission_status: str = "pending"   # pending | sent | acked | failed

    # -- cycle de vie ------------------------------------------------------- #
    def transition(self, target: str, reason: str = "", at: dt.datetime | None = None) -> DetectionEvent:
        """Change d'état en journalisant la raison. Refuse toute transition illégale."""
        if target not in STATES:
            raise InvalidTransition(f"état inconnu: '{target}'")
        if not can_transition(self.state, target):
            raise InvalidTransition(
                f"transition refusée: {self.state} → {target} "
                f"(autorisées: {TRANSITIONS.get(self.state, ()) or 'aucune, état terminal'})"
            )
        stamp = (at or dt.datetime.now(dt.timezone.utc)).isoformat()
        self.history.append({"at": stamp, "from": self.state, "to": target, "reason": reason})
        self.state = target
        return self

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_actionable(self) -> bool:
        """Un événement qu'un opérateur doit voir."""
        return self.state in (CONFIRMED, TRANSMITTED, ACKNOWLEDGED)

    def record_operator_decision(
        self, decision: str, comment: str = "", at: dt.datetime | None = None
    ) -> DetectionEvent:
        """Enregistre la validation ou l'invalidation par un opérateur.

        C'est l'entrée de la boucle d'apprentissage : chaque invalidation motivée
        devient un négatif étiqueté du site.
        """
        if decision not in OPERATOR_DECISIONS:
            raise ValueError(f"décision inconnue: '{decision}' (attendues: {OPERATOR_DECISIONS})")
        self.operator_decision = decision
        self.operator_comment = comment or None
        target = OPERATOR_VALIDATED if decision in POSITIVE_DECISIONS else OPERATOR_REJECTED
        return self.transition(target, reason=f"opérateur: {decision}", at=at)

    def mark_transmitted(self, at: dt.datetime | None = None) -> DetectionEvent:
        self.transmission_status = "sent"
        if can_transition(self.state, TRANSMITTED):
            self.transition(TRANSMITTED, reason="émis vers la plateforme", at=at)
        return self

    def mark_acknowledged(self, at: dt.datetime | None = None) -> DetectionEvent:
        self.transmission_status = "acked"
        if can_transition(self.state, ACKNOWLEDGED):
            self.transition(ACKNOWLEDGED, reason="accusé de réception", at=at)
        return self

    # -- sérialisation ------------------------------------------------------ #
    def as_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> DetectionEvent:
        version = data.get("schema_version", SCHEMA_VERSION)
        if str(version).split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise ValueError(
                f"événement en schéma v{version}, incompatible avec v{SCHEMA_VERSION}"
            )
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            # Tolérant en lecture : un champ ajouté par une version plus récente
            # ne doit pas faire tomber un site qui n'a pas encore été mis à jour.
            data = {k: v for k, v in data.items() if k in known}
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> DetectionEvent:
        return cls.from_dict(json.loads(text))

    def as_geojson_feature(self) -> dict:
        """Représentation GeoJSON, directement affichable sur une carte."""
        geometry = (
            {"type": "Point", "coordinates": [self.longitude, self.latitude]}
            if self.latitude is not None and self.longitude is not None
            else None
        )
        return {
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "event_id": self.event_id,
                "site_id": self.site_id,
                "camera_id": self.camera_id,
                "detected_at": self.detected_at,
                "state": self.state,
                "bearing_deg": round(self.bearing_deg, 2),
                "distance_m": self.distance_m,
                "fused_score": round(self.fused_score, 4),
                "localization_method": self.localization_method,
                "uncertainty": self.uncertainty,
                "tower_votes": self.tower_votes,
                "operator_decision": self.operator_decision,
            },
        }

    def summary_line(self) -> str:
        """Ligne lisible par un humain, pour un log ou une notification."""
        pos = (
            f"{self.latitude:.5f},{self.longitude:.5f}"
            if self.latitude is not None
            else f"az {self.bearing_deg:.1f}°"
        )
        dist = f" à {self.distance_m / 1000:.1f} km" if self.distance_m else ""
        votes = f" [{'+'.join(self.tower_votes)}]" if len(self.tower_votes) > 1 else ""
        return (
            f"{self.detected_at} {self.site_id}/{self.camera_id} "
            f"{self.state} score={self.fused_score:.2f} {pos}{dist}{votes}"
        )


def new_event_id() -> str:
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    """Horodatage UTC ISO 8601.

    Toujours en UTC : corréler deux tours dont les horloges sont exprimées en
    heure locale, avec ou sans heure d'été, est une source d'erreur silencieuse.
    """
    return dt.datetime.now(dt.timezone.utc).isoformat()


def event_from_alert(alert, software_version: str = "unknown") -> DetectionEvent:
    """Convertit une alerte du pipeline en événement canonique."""
    unc = None
    if alert.distance_m:
        unc = bearing_uncertainty(float(alert.distance_m)).as_dict()
    return DetectionEvent(
        event_id=new_event_id(),
        site_id=alert.site_id,
        camera_id=alert.view_id,
        detected_at=alert.timestamp,
        state=CANDIDATE,
        bearing_deg=alert.bearing_deg,
        distance_m=alert.distance_m,
        latitude=alert.latitude,
        longitude=alert.longitude,
        localization_method=alert.localization,
        uncertainty=unc,
        ai_probability=float(alert.features.get("cnn_score", 0.0)),
        physical_score=float(alert.features.get("growth_score", 0.0)),
        fused_score=alert.score,
        features={k: v for k, v in alert.features.items() if k != "bbox"},
        bbox=list(alert.features.get("bbox", [])),
        n_visits=alert.n_visits,
        tower_votes=[alert.site_id],
        model_version=alert.model_version,
        pipeline_tier=alert.pipeline_tier,
        software_version=software_version,
    )
