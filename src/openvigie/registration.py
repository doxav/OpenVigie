"""Recalage d'image.

Sans cette étape, toute différence au fond est inexploitable : la dérive de
preset d'une tête PTZ (0,1 à 0,5°) et la flexion d'un pylône sous le vent
produisent des bords fantômes sur tous les contours de la scène, qui dominent
largement le signal d'un panache naissant.

Deux niveaux :
  - corrélation de phase (translation, sous-pixel) : NumPy pur, embarquable ;
  - homographie ORB+RANSAC (rotation/échelle) : requiert OpenCV.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .compat import HAS_CV2, cv2, to_gray


@dataclass
class Alignment:
    """Résultat d'un recalage."""

    dy: float
    dx: float
    confidence: float
    method: str

    @property
    def magnitude_px(self) -> float:
        return float(np.hypot(self.dy, self.dx))


def _hann2d(shape: tuple[int, int]) -> np.ndarray:
    wy = np.hanning(shape[0])
    wx = np.hanning(shape[1])
    return np.outer(wy, wx).astype(np.float32)


def phase_correlate(ref: np.ndarray, img: np.ndarray) -> Alignment:
    """Corrélation de phase.

    Convention : le décalage renvoyé est la **correction** à appliquer à ``img``
    pour l'amener sur ``ref``. Si ``img`` a glissé de +5 px vers le bas, on
    renvoie dy = -5.

    Interpolation parabolique du pic pour une précision sous-pixel.
    """
    a = to_gray(ref)
    b = to_gray(img)
    if a.shape != b.shape:
        raise ValueError(f"formes incompatibles: {a.shape} vs {b.shape}")

    win = _hann2d(a.shape)
    fa = np.fft.rfft2((a - a.mean()) * win)
    fb = np.fft.rfft2((b - b.mean()) * win)
    cross = fa * np.conj(fb)
    denom = np.abs(cross)
    denom[denom < 1e-12] = 1e-12
    corr = np.fft.irfft2(cross / denom, s=a.shape)

    peak = int(np.argmax(corr))
    py, px = np.unravel_index(peak, corr.shape)
    peak_val = float(corr[py, px])

    def _sub(axis_vals: np.ndarray, idx: int) -> float:
        n = len(axis_vals)
        prev = axis_vals[(idx - 1) % n]
        cur = axis_vals[idx]
        nxt = axis_vals[(idx + 1) % n]
        denom_ = 2 * cur - prev - nxt
        return 0.0 if abs(denom_) < 1e-12 else 0.5 * (prev - nxt) / denom_

    dy = py + _sub(corr[:, px], py)
    dx = px + _sub(corr[py, :], px)
    if dy > a.shape[0] / 2:
        dy -= a.shape[0]
    if dx > a.shape[1] / 2:
        dx -= a.shape[1]

    mean_corr = float(np.abs(corr).mean())
    confidence = peak_val / mean_corr if mean_corr > 1e-12 else 0.0
    return Alignment(dy=float(dy), dx=float(dx), confidence=confidence, method="phase")


def shift_image(img: np.ndarray, dy: float, dx: float) -> np.ndarray:
    """Applique un décalage (interpolation bilinéaire, bords répliqués)."""
    arr = np.asarray(img, dtype=np.float32)
    if arr.ndim == 3:
        return np.stack([shift_image(arr[..., c], dy, dx) for c in range(arr.shape[2])], axis=-1)

    h, w = arr.shape
    yy, xx = np.meshgrid(np.arange(h, dtype=np.float32), np.arange(w, dtype=np.float32), indexing="ij")
    sy = np.clip(yy - dy, 0, h - 1)
    sx = np.clip(xx - dx, 0, w - 1)
    y0 = np.floor(sy).astype(np.int32)
    x0 = np.floor(sx).astype(np.int32)
    y1 = np.clip(y0 + 1, 0, h - 1)
    x1 = np.clip(x0 + 1, 0, w - 1)
    wy = (sy - y0).astype(np.float32)
    wx = (sx - x0).astype(np.float32)
    top = arr[y0, x0] * (1 - wx) + arr[y0, x1] * wx
    bot = arr[y1, x0] * (1 - wx) + arr[y1, x1] * wx
    return top * (1 - wy) + bot * wy


def align_to_reference(
    ref: np.ndarray, img: np.ndarray, max_shift_px: float = 60.0
) -> tuple[np.ndarray, Alignment]:
    """Recale ``img`` sur ``ref``.

    Si le décalage estimé dépasse ``max_shift_px``, on considère le recalage non
    fiable (scène trop changée, brouillard total, obturateur) et on renvoie
    l'image inchangée avec une confiance nulle : le pipeline saute alors le cycle
    plutôt que de produire des candidats fantômes.
    """
    al = phase_correlate(ref, img)
    if al.magnitude_px > max_shift_px or not np.isfinite(al.magnitude_px):
        return np.asarray(img, dtype=np.float32), Alignment(0.0, 0.0, 0.0, "rejected")
    return shift_image(img, al.dy, al.dx), al


def homography_align(ref: np.ndarray, img: np.ndarray):  # pragma: no cover - requiert cv2
    """Recalage par homographie ORB+RANSAC. Utile si la tête PTZ introduit de la
    rotation (jeu mécanique) et pas seulement de la translation."""
    if not HAS_CV2:
        raise RuntimeError("homography_align requiert OpenCV")
    a = to_gray(ref).astype(np.uint8)
    b = to_gray(img).astype(np.uint8)
    orb = cv2.ORB_create(2000)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return None, None
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = sorted(matcher.match(db, da), key=lambda m: m.distance)[:200]
    if len(matches) < 8:
        return None, None
    src = np.float32([kb[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    dst = np.float32([ka[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    if H is None:
        return None, None
    warped = cv2.warpPerspective(np.asarray(img), H, (a.shape[1], a.shape[0]))
    return warped, H


def preset_repeatability(frames: list[np.ndarray]) -> dict:
    """Mesure la répétabilité d'un preset PTZ.

    On revient N fois sur le même preset et on mesure le décalage de chaque
    image par rapport à la première. C'est la mesure la plus utile de la phase 1 :
    elle décide si un modèle de fond par preset est viable sur ce matériel.
    """
    if len(frames) < 2:
        raise ValueError("il faut au moins 2 images")
    ref = frames[0]
    shifts = [phase_correlate(ref, f) for f in frames[1:]]
    mags = np.array([s.magnitude_px for s in shifts])
    return {
        "n_samples": len(shifts),
        "mean_px": float(mags.mean()),
        "p95_px": float(np.percentile(mags, 95)),
        "max_px": float(mags.max()),
        "std_px": float(mags.std()),
        "shifts": [(round(s.dy, 2), round(s.dx, 2)) for s in shifts],
    }


def vibration_index(frames: list[np.ndarray]) -> float:
    """Amplitude RMS du décalage entre images consécutives, en pixels.

    Sur un pylône treillis au vent, cet indice explose : il sert de porte
    (on suspend l'analyse au-delà d'un seuil) et de diagnostic de montage.
    On mesure une amplitude et non une dispersion : une oscillation régulière
    de grande amplitude est tout aussi destructrice qu'un mouvement erratique.
    """
    if len(frames) < 3:
        return 0.0
    mags = np.array(
        [phase_correlate(frames[i - 1], frames[i]).magnitude_px for i in range(1, len(frames))]
    )
    return float(np.sqrt(np.mean(mags**2)))
