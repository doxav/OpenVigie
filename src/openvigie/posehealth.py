"""Santé sémantique d'une caméra et d'une tête PTZ.

Ce module répond à une classe de pannes particulièrement pernicieuse : celles
où **le système paraît sain alors qu'il ne surveille plus rien**. Une zone
aveugle invisible pour l'opérateur est pire qu'une panne franche, parce que
personne ne va la corriger.

Deux modes de défaillance sont traités, tous deux observés en production :

## 1. Une tête PTZ bloquée sur une seule position

Plusieurs presets sont commandés, la tête ne bouge pas — mécanique grippée,
alimentation du moteur coupée, commande refusée en silence — et **la caméra
continue de renvoyer des images parfaitement valides**. Rien ne signale la
panne : le flux répond, les images sont nettes, l'inférence tourne. Mais toutes
les poses renvoient la même scène.

Conséquences en cascade : les azimuts attribués aux détections sont faux (on
croit regarder au sud-ouest, on regarde au nord), les alertes se dupliquent
pose après pose, et la surveillance se limite à une direction sans que
personne ne le sache.

**Détection** : une empreinte perceptuelle par pose. Si deux poses censées
regarder des directions différentes produisent des images quasi identiques, la
tête n'a pas bougé. Le test est purement logiciel et ne coûte qu'un
redimensionnement par image.

## 2. Une caméra hors ligne déclarée vivante

Quand l'image provient d'un cache sans durée de validité, une caméra
déconnectée peut continuer indéfiniment à paraître active : le dernier cliché
réussi est resservi à chaque interrogation. Le battement de cœur est vert, la
caméra est morte.

**Détection** : horodater la **capture** (et non la lecture), puis appliquer
une durée de validité explicite. Une image plus vieille que sa fenêtre est
déclarée périmée, quelle que soit la réussite de la requête.

Le module est pur NumPy, sans dépendance à une caméra ou à un réseau, pour
rester testable et réutilisable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

# --------------------------------------------------------------------------- #
# Empreinte perceptuelle
# --------------------------------------------------------------------------- #
DEFAULT_HASH_SIZE = 8


def _to_gray(frame: np.ndarray) -> np.ndarray:
    arr = np.asarray(frame)
    if arr.ndim == 3 and arr.shape[2] >= 3:
        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        return (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float32)
    if arr.ndim == 2:
        return arr.astype(np.float32)
    raise ValueError(f"format d'image non supporté : {arr.shape}")


def _resize_nearest(img: np.ndarray, height: int, width: int) -> np.ndarray:
    """Redimensionnement par plus proche voisin, sans dépendance externe."""
    h, w = img.shape
    rows = np.linspace(0, h - 1, height).astype(int)
    cols = np.linspace(0, w - 1, width).astype(int)
    return img[np.ix_(rows, cols)]


def perceptual_hash(frame: np.ndarray, hash_size: int = DEFAULT_HASH_SIZE) -> np.ndarray:
    """Empreinte perceptuelle (dHash) d'une image, sous forme de bits.

    Le dHash compare chaque pixel à son voisin de droite après réduction
    drastique. Il est choisi ici pour trois raisons :

    - **insensible au bruit et à l'exposition** : on compare des voisins, donc
      un changement global de luminosité ne modifie pas l'empreinte. Une même
      scène à dix minutes d'intervalle garde la même empreinte ;
    - **sensible à la structure** : deux directions différentes du paysage
      donnent des empreintes très éloignées ;
    - **coût négligeable** : quelques centaines d'opérations par image.

    C'est exactement le compromis recherché : on veut détecter « la tête n'a pas
    bougé », pas « l'image a légèrement changé ».
    """
    if hash_size < 2:
        raise ValueError("hash_size doit être >= 2")
    gray = _to_gray(frame)
    small = _resize_nearest(gray, hash_size, hash_size + 1)
    return (small[:, 1:] > small[:, :-1]).flatten()


def hamming_distance(a: np.ndarray, b: np.ndarray) -> int:
    """Nombre de bits différents entre deux empreintes."""
    if a.shape != b.shape:
        raise ValueError("empreintes de tailles différentes")
    return int(np.count_nonzero(a != b))


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Similarité dans [0, 1] : 1 = images perceptuellement identiques."""
    return 1.0 - hamming_distance(a, b) / a.size


# --------------------------------------------------------------------------- #
# Fraîcheur des images
# --------------------------------------------------------------------------- #
@dataclass
class FrameStamp:
    """Une image et l'instant de sa **capture**, non de sa lecture.

    La distinction est le cœur du correctif : un cache qui resert le dernier
    cliché réussi renvoie une image dont la lecture est récente et la capture
    ancienne. Seule la seconde dit si la caméra répond encore.
    """

    pose_id: str
    captured_at: float
    fingerprint: np.ndarray = field(repr=False)

    def age_s(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.captured_at

    def is_stale(self, ttl_s: float, now: float | None = None) -> bool:
        return self.age_s(now) > ttl_s


# --------------------------------------------------------------------------- #
# Diagnostic PTZ
# --------------------------------------------------------------------------- #
@dataclass
class PoseCollision:
    """Deux poses distinctes qui renvoient la même scène."""

    pose_a: str
    pose_b: str
    similarity: float

    def as_dict(self) -> dict:
        return {
            "pose_a": self.pose_a,
            "pose_b": self.pose_b,
            "similarity": round(self.similarity, 4),
        }


@dataclass
class PtzHealthReport:
    """Diagnostic d'une tête PTZ, au-delà de la simple disponibilité HTTP."""

    camera_id: str
    n_poses: int
    collisions: list[PoseCollision] = field(default_factory=list)
    stale_poses: list[str] = field(default_factory=list)
    distinct_poses: int = 0
    checked_at: float = 0.0

    @property
    def stuck(self) -> bool:
        """La tête est-elle bloquée ?

        Le critère est volontairement strict : il faut qu'au moins deux poses
        distinctes se confondent. Une seule collision suffit à fausser les
        azimuts, il n'y a donc aucune raison d'attendre qu'elles soient
        nombreuses.
        """
        return bool(self.collisions)

    @property
    def status(self) -> str:
        if self.stuck:
            return "stuck"
        if self.stale_poses:
            return "stale"
        if self.n_poses and self.distinct_poses < self.n_poses:
            return "degraded"
        return "ok"

    @property
    def message(self) -> str:
        if self.stuck:
            pairs = ", ".join(f"{c.pose_a}≡{c.pose_b}" for c in self.collisions[:3])
            return (
                f"tête probablement bloquée : {len(self.collisions)} paire(s) de poses "
                f"identiques ({pairs}). Les azimuts attribués aux détections sont faux, "
                f"et la surveillance se limite à une direction."
            )
        if self.stale_poses:
            return (
                f"{len(self.stale_poses)} pose(s) périmée(s) : "
                f"{', '.join(self.stale_poses[:3])}. La caméra répond peut-être depuis "
                f"un cache alors qu'elle est hors ligne."
            )
        return f"{self.distinct_poses}/{self.n_poses} poses distinctes, images fraîches"

    def as_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "status": self.status,
            "stuck": self.stuck,
            "n_poses": self.n_poses,
            "distinct_poses": self.distinct_poses,
            "collisions": [c.as_dict() for c in self.collisions],
            "stale_poses": self.stale_poses,
            "message": self.message,
        }


class PoseFingerprintRegistry:
    """Suit l'empreinte de chaque pose d'une caméra et diagnostique la tête.

    Usage : appeler ``record`` après chaque acquisition, puis ``report``
    périodiquement (typiquement à chaque battement de cœur).

    ``collision_threshold`` est la similarité au-delà de laquelle deux poses
    sont jugées identiques. La valeur par défaut (0,92) laisse passer les
    variations normales d'une même scène — nuages, ombres, végétation qui
    bouge — tout en détectant deux poses réellement confondues. Elle est
    réglable parce que la marge dépend du paysage : un horizon très uniforme
    (mer, plaine) rapproche naturellement les empreintes de deux directions
    voisines, et demande donc un seuil plus haut.
    """

    def __init__(
        self,
        camera_id: str,
        collision_threshold: float = 0.92,
        ttl_s: float = 900.0,
        hash_size: int = DEFAULT_HASH_SIZE,
        clock=time.time,
    ) -> None:
        if not 0.0 < collision_threshold <= 1.0:
            raise ValueError("collision_threshold doit être dans ]0, 1]")
        if ttl_s <= 0:
            raise ValueError("ttl_s doit être > 0")
        self.camera_id = camera_id
        self.collision_threshold = collision_threshold
        self.ttl_s = ttl_s
        self.hash_size = hash_size
        self.clock = clock
        self._stamps: dict[str, FrameStamp] = {}

    def record(self, pose_id: str, frame: np.ndarray, captured_at: float | None = None) -> FrameStamp:
        """Enregistre l'empreinte d'une pose.

        ``captured_at`` doit être l'instant de **capture** fourni par la source.
        À défaut, on prend l'instant courant — ce qui est correct pour une
        acquisition directe, mais masquerait un cache : c'est pourquoi le
        paramètre existe et doit être renseigné dès que la source le permet.
        """
        stamp = FrameStamp(
            pose_id=pose_id,
            captured_at=self.clock() if captured_at is None else captured_at,
            fingerprint=perceptual_hash(frame, self.hash_size),
        )
        self._stamps[pose_id] = stamp
        return stamp

    def report(self, now: float | None = None) -> PtzHealthReport:
        """Diagnostic complet de la tête."""
        now = self.clock() if now is None else now
        poses = sorted(self._stamps)
        collisions: list[PoseCollision] = []

        for i, a in enumerate(poses):
            for b in poses[i + 1 :]:
                sim = similarity(self._stamps[a].fingerprint, self._stamps[b].fingerprint)
                if sim >= self.collision_threshold:
                    collisions.append(PoseCollision(a, b, sim))

        stale = [p for p in poses if self._stamps[p].is_stale(self.ttl_s, now)]

        # Nombre de scènes réellement distinctes, par regroupement transitif.
        groups: list[set[str]] = []
        for p in poses:
            placed = False
            for g in groups:
                if any(
                    similarity(self._stamps[p].fingerprint, self._stamps[q].fingerprint)
                    >= self.collision_threshold
                    for q in g
                ):
                    g.add(p)
                    placed = True
                    break
            if not placed:
                groups.append({p})

        return PtzHealthReport(
            camera_id=self.camera_id,
            n_poses=len(poses),
            collisions=collisions,
            stale_poses=stale,
            distinct_poses=len(groups),
            checked_at=now,
        )

    def drift_since(self, pose_id: str, frame: np.ndarray) -> float | None:
        """Similarité entre une nouvelle image et la référence d'une pose.

        Une chute progressive signale une dérive de cadrage — vent, maintenance,
        fixation qui a bougé — bien avant qu'elle ne devienne une collision.
        Renvoie ``None`` si la pose n'a jamais été enregistrée.
        """
        stamp = self._stamps.get(pose_id)
        if stamp is None:
            return None
        return similarity(stamp.fingerprint, perceptual_hash(frame, self.hash_size))

    @property
    def known_poses(self) -> list[str]:
        return sorted(self._stamps)
