"""Intégrité d'un jeu de données d'apprentissage.

Ce module vise une classe de défauts particulièrement coûteuse : ceux qui
**corrompent silencieusement l'apprentissage ou l'évaluation** sans faire
échouer aucune étape du pipeline. Le symptôme n'apparaît que bien plus tard,
sous la forme d'un modèle inexplicablement moins bon.

Trois contrôles, du plus grave au moins grave.

## 1. Collision inter-classes

Le même identifiant de séquence présent à la fois dans la classe « feu » et
dans la classe « faux positif ». Le pipeline ne signale rien : chaque classe est
cohérente prise isolément. Mais un vrai feu se retrouve appris **comme
exemple de ce qu'il ne faut pas détecter**, ce qui dégrade exactement la
capacité qu'on cherche à construire.

Ce contrôle est distinct d'un contrôle de fuite entre splits, qui vérifie
qu'une séquence n'apparaît pas dans deux splits d'une **même** classe. Les
deux sont nécessaires et ne se recouvrent pas : on peut parfaitement n'avoir
aucune fuite entre train/val/test et avoir une séquence classée dans les deux
sens.

## 2. Dérive du jeu de test entre deux constructions

Si le jeu de test change d'une construction à l'autre sans intention explicite,
les scores d'avant et d'après cessent d'être comparables — et l'on croit
mesurer un progrès de modèle là où l'on mesure un changement de données. Le
registre ci-dessous fige l'affectation et rapporte tout écart, en distinguant
ajouts, retraits et déplacements.

## 3. Non-reproductibilité d'une construction

Deux constructions successives à partir des mêmes entrées doivent produire
exactement les mêmes sorties. Comparer les empreintes des deux (« construire
deux fois et comparer ») détecte les sources de non-déterminisme — ordre de
parcours de système de fichiers, graine non fixée, dépendance à un
environnement de calcul.

Le module est pur : il travaille sur des ensembles d'identifiants et des
empreintes, sans dépendance à un format de stockage ou à un outil de
versionnement particulier.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# 1. Collision inter-classes
# --------------------------------------------------------------------------- #
@dataclass
class ClassCollision:
    """Un identifiant présent dans plusieurs classes mutuellement exclusives."""

    identifier: str
    classes: list[str]

    def as_dict(self) -> dict:
        return {"identifier": self.identifier, "classes": sorted(self.classes)}


def find_class_collisions(class_to_ids: dict[str, set[str]]) -> list[ClassCollision]:
    """Identifiants présents dans plusieurs classes exclusives.

    ``class_to_ids`` associe un nom de classe à ses identifiants de séquence,
    par exemple ``{"wildfire": {...}, "fp": {...}}``.

    Le contrôle est volontairement indépendant du split : une séquence qui est
    un feu dans le jeu d'entraînement et un faux positif dans le jeu de test
    est tout aussi corrompue qu'une collision à l'intérieur d'un même split.
    """
    seen: dict[str, list[str]] = {}
    for class_name, identifiers in class_to_ids.items():
        for identifier in identifiers:
            seen.setdefault(identifier, []).append(class_name)
    return [
        ClassCollision(identifier, classes)
        for identifier, classes in sorted(seen.items())
        if len(classes) > 1
    ]


def assert_no_class_collision(class_to_ids: dict[str, set[str]]) -> None:
    """Échoue immédiatement s'il existe une collision.

    L'échec est volontairement brutal : poursuivre une construction dont on
    sait qu'elle apprendra un feu comme faux positif ne présente aucun intérêt,
    et un simple avertissement finirait par être ignoré dans un journal.
    """
    collisions = find_class_collisions(class_to_ids)
    if not collisions:
        return
    detail = ", ".join(
        f"{c.identifier} ({'/'.join(sorted(c.classes))})" for c in collisions[:5]
    )
    more = f" et {len(collisions) - 5} autre(s)" if len(collisions) > 5 else ""
    raise ValueError(
        f"{len(collisions)} collision(s) inter-classes : {detail}{more}. "
        f"Un même identifiant ne peut pas être à la fois exemple et contre-exemple ; "
        f"la séquence serait apprise comme ce qu'il ne faut pas détecter."
    )


# --------------------------------------------------------------------------- #
# 2. Registre de split
# --------------------------------------------------------------------------- #
@dataclass
class SplitDrift:
    """Écart entre deux constructions d'un même jeu."""

    added: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    moved: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def is_stable(self) -> bool:
        return not (self.added or self.removed or self.moved)

    @property
    def n_changes(self) -> int:
        return (
            sum(len(v) for v in self.added.values())
            + sum(len(v) for v in self.removed.values())
            + len(self.moved)
        )

    def as_dict(self) -> dict:
        return {
            "stable": self.is_stable,
            "n_changes": self.n_changes,
            "added": {k: sorted(v) for k, v in self.added.items() if v},
            "removed": {k: sorted(v) for k, v in self.removed.items() if v},
            "moved": [{"id": i, "from": a, "to": b} for i, a, b in self.moved],
        }

    def summary(self) -> str:
        if self.is_stable:
            return "Jeu stable : les scores restent comparables à la construction précédente."
        parts = []
        if self.moved:
            parts.append(
                f"{len(self.moved)} séquence(s) ont changé de split — c'est le cas le plus "
                f"grave : une séquence passée de train à test rend la comparaison invalide "
                f"dans les deux sens"
            )
        n_added = sum(len(v) for v in self.added.values())
        n_removed = sum(len(v) for v in self.removed.values())
        if n_added:
            parts.append(f"{n_added} ajoutée(s)")
        if n_removed:
            parts.append(f"{n_removed} retirée(s)")
        return (
            f"Jeu modifié ({self.n_changes} changement(s)) : " + " ; ".join(parts) +
            ". Les scores d'avant et d'après ne mesurent plus la même chose."
        )


class SplitLedger:
    """Registre figé de l'affectation identifiant → split.

    Sert à répondre à une question simple et souvent négligée : *le jeu sur
    lequel je viens d'évaluer est-il bien celui sur lequel j'ai évalué la fois
    précédente ?*
    """

    def __init__(self, assignments: dict[str, str] | None = None) -> None:
        self.assignments: dict[str, str] = dict(assignments or {})

    @classmethod
    def from_splits(cls, splits: dict[str, set[str]]) -> SplitLedger:
        """Construit le registre à partir de ``{split: identifiants}``.

        Refuse un identifiant présent dans deux splits : contrairement à une
        collision de classe, celle-ci est une fuite pure et simple.
        """
        assignments: dict[str, str] = {}
        duplicates: list[str] = []
        for split, identifiers in splits.items():
            for identifier in identifiers:
                if identifier in assignments:
                    duplicates.append(identifier)
                else:
                    assignments[identifier] = split
        if duplicates:
            raise ValueError(
                f"{len(duplicates)} identifiant(s) présents dans plusieurs splits : "
                f"{sorted(duplicates)[:5]} — fuite entre jeux d'entraînement et d'évaluation."
            )
        return cls(assignments)

    def diff(self, other: SplitLedger) -> SplitDrift:
        """Compare ce registre (référence) à un autre (nouvelle construction)."""
        drift = SplitDrift()
        for identifier, split in other.assignments.items():
            if identifier not in self.assignments:
                drift.added.setdefault(split, []).append(identifier)
            elif self.assignments[identifier] != split:
                drift.moved.append((identifier, self.assignments[identifier], split))
        for identifier, split in self.assignments.items():
            if identifier not in other.assignments:
                drift.removed.setdefault(split, []).append(identifier)
        return drift

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for split in self.assignments.values():
            out[split] = out.get(split, 0) + 1
        return out

    # -- persistance --------------------------------------------------------- #
    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"assignments": self.assignments}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> SplitLedger:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("assignments", {}))


# --------------------------------------------------------------------------- #
# 3. Reproductibilité d'une construction
# --------------------------------------------------------------------------- #
def manifest_hash(entries: dict[str, str]) -> str:
    """Empreinte d'un manifeste ``{chemin: empreinte de contenu}``.

    L'ordre des clés est normalisé : sans cela, l'empreinte dépendrait de
    l'ordre de parcours du système de fichiers, qui est précisément l'une des
    sources de non-déterminisme qu'on cherche à détecter.
    """
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class BuildComparison:
    """Résultat d'un « construire deux fois et comparer »."""

    identical: bool
    only_in_first: list[str] = field(default_factory=list)
    only_in_second: list[str] = field(default_factory=list)
    differing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "identical": self.identical,
            "only_in_first": sorted(self.only_in_first)[:20],
            "only_in_second": sorted(self.only_in_second)[:20],
            "differing": sorted(self.differing)[:20],
            "n_only_in_first": len(self.only_in_first),
            "n_only_in_second": len(self.only_in_second),
            "n_differing": len(self.differing),
        }

    def summary(self) -> str:
        if self.identical:
            return "Construction reproductible : deux exécutions donnent le même jeu."
        return (
            f"Construction NON reproductible : {len(self.differing)} fichier(s) au contenu "
            f"différent, {len(self.only_in_first)} disparu(s), "
            f"{len(self.only_in_second)} apparu(s). Chercher une graine non fixée, un "
            f"parcours de répertoire non trié, ou une dépendance à l'environnement de calcul."
        )


def compare_builds(first: dict[str, str], second: dict[str, str]) -> BuildComparison:
    """Compare deux manifestes de construction fichier par fichier."""
    keys_a, keys_b = set(first), set(second)
    differing = [k for k in keys_a & keys_b if first[k] != second[k]]
    only_a = list(keys_a - keys_b)
    only_b = list(keys_b - keys_a)
    return BuildComparison(
        identical=not (differing or only_a or only_b),
        only_in_first=only_a,
        only_in_second=only_b,
        differing=differing,
    )


def file_manifest(root: str | Path, patterns: tuple[str, ...] = ("*",)) -> dict[str, str]:
    """Manifeste ``{chemin relatif: sha256}`` d'un répertoire.

    Les chemins sont relatifs et triés, pour que le manifeste ne dépende ni de
    l'emplacement d'exécution ni de l'ordre du système de fichiers.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError(f"répertoire introuvable : {root}")
    out: dict[str, str] = {}
    for pattern in patterns:
        for p in sorted(root_path.rglob(pattern)):
            if p.is_file():
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
                out[str(p.relative_to(root_path))] = digest
    return out


# --------------------------------------------------------------------------- #
# Rapport global
# --------------------------------------------------------------------------- #
@dataclass
class IntegrityReport:
    """Bilan des trois contrôles."""

    collisions: list[ClassCollision] = field(default_factory=list)
    drift: SplitDrift | None = None
    build: BuildComparison | None = None

    @property
    def ok(self) -> bool:
        return (
            not self.collisions
            and (self.drift is None or self.drift.is_stable)
            and (self.build is None or self.build.identical)
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "collisions": [c.as_dict() for c in self.collisions],
            "drift": self.drift.as_dict() if self.drift else None,
            "build": self.build.as_dict() if self.build else None,
        }

    def summary(self) -> str:
        lines: list[str] = []
        if self.collisions:
            lines.append(
                f"BLOQUANT — {len(self.collisions)} collision(s) inter-classes : "
                f"un feu serait appris comme faux positif."
            )
        if self.drift is not None and not self.drift.is_stable:
            lines.append(self.drift.summary())
        if self.build is not None and not self.build.identical:
            lines.append(self.build.summary())
        return "\n".join(lines) if lines else "Intégrité vérifiée sur les trois contrôles."
