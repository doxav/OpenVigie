"""Génération de candidats par différence au fond recalée.

Cet étage n'est *pas* un classifieur : c'est un réducteur de débit. Il ramène
une image 5 MP à quelques dizaines de régions, dont seules celles-ci passeront
au réseau de neurones. C'est ce qui rend le tier MEDIUM (calcul dans la caméra)
possible du tout.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .compat import binary_close, binary_open, gaussian_blur, label_components, sobel_energy, to_gray


@dataclass
class Blob:
    """Région candidate dans une vue."""

    blob_id: int
    bbox: tuple[int, int, int, int]  # (x0, y0, x1, y1) exclusif
    centroid: tuple[float, float]    # (cx, cy)
    area_px: int
    mean_delta: float                # intensité moyenne de la différence au fond
    mask: np.ndarray = field(repr=False, default=None)

    @property
    def width_px(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height_px(self) -> int:
        return self.bbox[3] - self.bbox[1]

    @property
    def bottom_row(self) -> int:
        return self.bbox[3] - 1

    def iou(self, other: Blob) -> float:
        ax0, ay0, ax1, ay1 = self.bbox
        bx0, by0, bx1, by1 = other.bbox
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        if ix1 <= ix0 or iy1 <= iy0:
            return 0.0
        inter = (ix1 - ix0) * (iy1 - iy0)
        union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
        return inter / union if union > 0 else 0.0


@dataclass
class CandidateConfig:
    """Paramètres de l'étage classique. Tous sont à recalibrer par site."""

    mad_k: float = 4.0            # seuil = median + k * MAD
    blur_sigma: float = 1.2
    min_area_px: int = 25
    max_area_frac: float = 0.25   # au-delà, c'est un changement global (brouillard, exposition)
    open_iters: int = 1
    close_iters: int = 2
    require_ground_contact: bool = True
    ground_contact_margin_px: int = 8
    # Détection de changement global. AUDIT P0-06, et un défaut supplémentaire
    # trouvé en écrivant le test : le garde-fou ``max_area_frac`` ne se
    # déclenchait pratiquement JAMAIS. Le seuil MAD s'adapte à la dispersion de
    # la différence elle-même, donc quand toute l'image change, le seuil monte
    # avec elle et le masque reste vide. Un changement global se mesure sur des
    # statistiques globales, pas sur la surface d'un masque adaptatif.
    max_level_shift: float = 25.0          # décalage médian, en niveaux de gris
    min_contrast_ratio: float = 0.6        # brouillard : perte de contraste
    max_contrast_ratio: float = 1.7        # levée de brume, bascule WDR


def detect_global_change(
    frame: np.ndarray, reference: np.ndarray, cfg: CandidateConfig | None = None
) -> dict:
    """Détecte un basculement global de la scène : brouillard, WDR, pluie, nuit.

    Deux indicateurs, tous deux globaux et donc insensibles au seuillage
    adaptatif : le décalage du niveau médian, et le rapport d'énergie de
    gradient — c'est-à-dire la perte de contraste, signature du brouillard.

    Renvoyer un état nommé plutôt qu'une liste vide de candidats est essentiel :
    « aucun candidat » est indistinguable d'une scène calme, alors qu'un
    changement global doit suspendre l'apprentissage du fond.
    """
    cfg = cfg or CandidateConfig()
    f = to_gray(frame).astype(np.float32)
    r = to_gray(reference).astype(np.float32)
    level_shift = float(abs(np.median(f) - np.median(r)))
    e_r = float(sobel_energy(r).mean())
    contrast_ratio = float(sobel_energy(f).mean() / e_r) if e_r > 1e-6 else 1.0

    reasons = []
    if level_shift > cfg.max_level_shift:
        reasons.append(f"niveau global décalé de {level_shift:.0f} valeurs")
    if contrast_ratio < cfg.min_contrast_ratio:
        reasons.append(f"contraste réduit à {contrast_ratio:.0%} de la référence")
    elif contrast_ratio > cfg.max_contrast_ratio:
        reasons.append(f"contraste augmenté à {contrast_ratio:.0%} de la référence")

    return {
        "is_global_change": bool(reasons),
        "level_shift": round(level_shift, 2),
        "contrast_ratio": round(contrast_ratio, 3),
        "reason": " ; ".join(reasons),
    }


def robust_threshold(diff: np.ndarray, k: float = 4.0) -> float:
    """Seuil médian + k * MAD.

    Robuste aux variations globales d'exposition, contrairement à un seuil fixe
    ou à un Otsu qui bascule dès qu'un nuage passe.
    """
    med = float(np.median(diff))
    mad = float(np.median(np.abs(diff - med)))
    sigma = 1.4826 * mad
    if sigma < 1e-6:
        sigma = float(diff.std()) or 1.0
    return med + k * sigma


def extract_candidates(
    frame: np.ndarray,
    reference: np.ndarray,
    cfg: CandidateConfig | None = None,
    ground_mask: np.ndarray | None = None,
    return_change_fraction: bool = False,
):
    """Extrait les régions candidates d'une image recalée.

    ``ground_mask`` (True là où le pixel intersecte le sol) sert à deux choses :
    limiter la recherche, et surtout exiger que la *base* du candidat touche le
    terrain. Un nuage n'a jamais de base au sol : c'est le discriminant le plus
    puissant du système, et il ne coûte rien.
    """
    cfg = cfg or CandidateConfig()
    f = to_gray(frame).astype(np.float32)
    r = to_gray(reference).astype(np.float32)
    if f.shape != r.shape:
        raise ValueError(f"formes incompatibles: {f.shape} vs {r.shape}")

    diff = np.abs(gaussian_blur(f, cfg.blur_sigma) - gaussian_blur(r, cfg.blur_sigma))
    thr = robust_threshold(diff, cfg.mad_k)
    mask = diff > thr

    if ground_mask is not None:
        near_ground = ground_mask.copy()
        # on garde une bande au-dessus du sol : le panache monte
        for _ in range(max(1, f.shape[0] // 8)):
            near_ground[:-1] |= near_ground[1:]
        mask &= near_ground

    mask = binary_open(mask, cfg.open_iters)
    mask = binary_close(mask, cfg.close_iters)

    changed_fraction = float(mask.sum()) / float(f.size)
    if changed_fraction > cfg.max_area_frac:
        # Changement global : brouillard, exposition, pluie. Aucun candidat
        # exploitable — et surtout, l'appelant doit le savoir pour ne pas
        # apprendre cette image comme nouveau fond (AUDIT P0-06).
        return ([], changed_fraction) if return_change_fraction else []

    labels, n = label_components(mask)
    blobs: list[Blob] = []
    for lab in range(1, n + 1):
        sel = labels == lab
        area = int(sel.sum())
        if area < cfg.min_area_px:
            continue
        ys, xs = np.nonzero(sel)
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1

        if cfg.require_ground_contact and ground_mask is not None:
            band = slice(max(0, y1 - 1 - cfg.ground_contact_margin_px), min(f.shape[0], y1 + cfg.ground_contact_margin_px))
            if not ground_mask[band, x0:x1].any():
                continue

        blobs.append(
            Blob(
                blob_id=lab,
                bbox=(x0, y0, x1, y1),
                centroid=(float(xs.mean()), float(ys.mean())),
                area_px=area,
                mean_delta=float(diff[sel].mean()),
                mask=sel,
            )
        )
    blobs.sort(key=lambda b: b.area_px, reverse=True)
    return (blobs, changed_fraction) if return_change_fraction else blobs


def contrast_loss(frame: np.ndarray, reference: np.ndarray, bbox: tuple[int, int, int, int]) -> float:
    """Perte d'énergie de gradient dans une ROI, dans [0, 1].

    Une fumée translucide atténue les contours du relief derrière elle sans les
    effacer : c'est une signature très différente d'un objet opaque ajouté à la
    scène (véhicule, animal) qui, lui, *ajoute* des contours.
    """
    x0, y0, x1, y1 = bbox
    f = sobel_energy(to_gray(frame)[y0:y1, x0:x1])
    r = sobel_energy(to_gray(reference)[y0:y1, x0:x1])
    e_r = float(r.mean())
    if e_r < 1e-6:
        return 0.0
    return float(np.clip(1.0 - f.mean() / e_r, 0.0, 1.0))


def translucency(frame: np.ndarray, reference: np.ndarray, mask: np.ndarray) -> float:
    """Indice de translucidité : corrélation résiduelle entre la ROI et le fond.

    Proche de 1 = on voit encore le fond à travers (fumée fine) ;
    proche de 0 = le fond a disparu (objet opaque, ou fumée dense).
    """
    f = to_gray(frame)[mask].astype(np.float64)
    r = to_gray(reference)[mask].astype(np.float64)
    if f.size < 8 or f.std() < 1e-6 or r.std() < 1e-6:
        return 0.0
    return float(np.clip(np.corrcoef(f, r)[0, 1], 0.0, 1.0))
