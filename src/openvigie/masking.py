"""Masques de confidentialité.

AUDIT P0-21 (corrigé 0.4.0). Les masques figuraient dans la configuration du
site et la documentation les présentait comme une protection de la vie privée,
mais **aucun code ne les lisait**. Une promesse documentaire sans effet est pire
qu'une absence de promesse : elle décourage la mise en place d'une vraie mesure.

Deux règles tenues ici :

1. **Le masquage précède l'analyse.** Un masque appliqué au moment de l'export
   ne protégerait rien : les pixels auraient déjà alimenté un modèle de fond,
   produit une vignette, servi à un score. On masque à l'acquisition.
2. **Le masque est opaque, pas flouté.** Un floutage réversible sur une image
   compressée n'est pas une protection ; on remplace par une valeur constante.

Un zoom 30× identifie personnes et véhicules à plusieurs kilomètres : l'usage
n'a rien de théorique sur une tour surplombant une route ou des habitations.
"""

from __future__ import annotations

import numpy as np

Box = tuple[int, int, int, int]   # (x0, y0, x1, y1), bornes exclusives à droite


def apply_masks(
    frame: np.ndarray, boxes: list[Box] | None, fill: float = 0.0
) -> np.ndarray:
    """Occulte des rectangles d'une image. Renvoie l'image inchangée si aucun.

    L'image n'est copiée que s'il y a effectivement quelque chose à masquer :
    sur un site sans masque, le coût est nul.
    """
    if not boxes:
        return frame
    out = np.array(frame, copy=True)
    h, w = out.shape[:2]
    for box in boxes:
        x0, y0, x1, y1 = (int(v) for v in box)
        x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
        y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
        if x1 > x0 and y1 > y0:
            out[y0:y1, x0:x1] = fill
    return out


def masked_fraction(shape: tuple[int, ...], boxes: list[Box] | None) -> float:
    """Part de l'image occultée — utile pour signaler un masquage excessif."""
    if not boxes:
        return 0.0
    h, w = shape[:2]
    grid = np.zeros((h, w), dtype=bool)
    for x0, y0, x1, y1 in boxes:
        grid[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    return float(grid.mean())


def validate_masks(masks: dict[str, list[list[int]]], max_fraction: float = 0.5) -> list[str]:
    """Contrôles de cohérence des masques déclarés."""
    problems: list[str] = []
    for view_id, boxes in masks.items():
        for box in boxes:
            if len(box) != 4:
                problems.append(f"{view_id}: boîte mal formée {box} (attendu [x0, y0, x1, y1])")
                continue
            x0, y0, x1, y1 = box
            if x1 <= x0 or y1 <= y0:
                problems.append(f"{view_id}: boîte vide ou inversée {box}")
    return problems
