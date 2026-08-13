"""Couche de compatibilité.

Le pipeline doit tourner à l'identique sur un Jetson (OpenCV + SciPy présents)
et sur une carte caméra OpenIPC où seul NumPy est disponible. Chaque primitive
lourde a donc un repli NumPy pur, et les tests s'exécutent dans les deux modes
via la variable d'environnement ``OPENVIGIE_FORCE_NUMPY=1``.
"""

from __future__ import annotations

import os

import numpy as np

_FORCE_NUMPY = os.environ.get("OPENVIGIE_FORCE_NUMPY", "0") == "1"

try:  # pragma: no cover - dépend de l'environnement
    import cv2 as _cv2

    HAS_CV2 = not _FORCE_NUMPY
except Exception:  # pragma: no cover
    _cv2 = None
    HAS_CV2 = False

try:  # pragma: no cover
    from scipy import ndimage as _ndimage

    HAS_SCIPY = not _FORCE_NUMPY
except Exception:  # pragma: no cover
    _ndimage = None
    HAS_SCIPY = False

cv2 = _cv2
ndimage = _ndimage


def _shift_or(arr: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """Décale un tableau booléen en remplissant les bords avec False."""
    out = np.zeros_like(arr)
    ys = slice(max(0, dy), arr.shape[0] + min(0, dy))
    xs = slice(max(0, dx), arr.shape[1] + min(0, dx))
    ys_src = slice(max(0, -dy), arr.shape[0] + min(0, -dy))
    xs_src = slice(max(0, -dx), arr.shape[1] + min(0, -dx))
    out[ys, xs] = arr[ys_src, xs_src]
    return out


def binary_dilate(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """Dilatation binaire 3x3 (connexité 8)."""
    out = mask.astype(bool)
    for _ in range(iterations):
        if HAS_SCIPY:
            out = ndimage.binary_dilation(out)
            continue
        acc = out.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                acc |= _shift_or(out, dy, dx)
        out = acc
    return out


def binary_erode(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """Érosion binaire 3x3 (connexité 8)."""
    out = mask.astype(bool)
    for _ in range(iterations):
        if HAS_SCIPY:
            out = ndimage.binary_erosion(out, border_value=0)
            continue
        acc = out.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                acc &= _shift_or(out, dy, dx)
        out = acc
    return out


def binary_open(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    return binary_dilate(binary_erode(mask, iterations), iterations)


def binary_close(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    return binary_erode(binary_dilate(mask, iterations), iterations)


def label_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Étiquetage en composantes connexes (connexité 8).

    Renvoie ``(labels, n)`` avec ``labels`` valant 0 pour le fond.
    Repli NumPy pur : union-find sur deux passes.
    """
    mask = mask.astype(bool)
    if HAS_SCIPY:
        structure = np.ones((3, 3), dtype=bool)
        labels, n = ndimage.label(mask, structure=structure)
        return labels.astype(np.int32), int(n)

    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for y in range(h):
        for x in range(w):
            if not mask[y, x]:
                continue
            neigh = []
            for dy, dx in ((-1, -1), (-1, 0), (-1, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and labels[ny, nx]:
                    neigh.append(labels[ny, nx])
            if not neigh:
                parent.append(len(parent))
                labels[y, x] = len(parent) - 1
            else:
                m = min(neigh)
                labels[y, x] = m
                for other in neigh:
                    union(m, other)

    remap: dict[int, int] = {0: 0}
    nxt = 1
    out = np.zeros_like(labels)
    for y in range(h):
        for x in range(w):
            lab = labels[y, x]
            if lab == 0:
                continue
            root = find(lab)
            if root not in remap:
                remap[root] = nxt
                nxt += 1
            out[y, x] = remap[root]
    return out, nxt - 1


def gaussian_blur(img: np.ndarray, sigma: float = 1.0) -> np.ndarray:
    """Flou gaussien séparable."""
    if sigma <= 0:
        return img.astype(np.float32)
    if HAS_SCIPY:
        return ndimage.gaussian_filter(img.astype(np.float32), sigma)
    radius = max(1, int(3 * sigma))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    k = np.exp(-(x**2) / (2 * sigma**2))
    k /= k.sum()
    pad = np.pad(img.astype(np.float32), radius, mode="edge")
    tmp = np.apply_along_axis(lambda m: np.convolve(m, k, mode="valid"), 1, pad)
    out = np.apply_along_axis(lambda m: np.convolve(m, k, mode="valid"), 0, tmp)
    return out


def sobel_energy(img: np.ndarray) -> np.ndarray:
    """Énergie de gradient (magnitude de Sobel), utilisée pour la perte de contraste."""
    f = img.astype(np.float32)
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
    ky = kx.T
    pad = np.pad(f, 1, mode="edge")
    gx = np.zeros_like(f)
    gy = np.zeros_like(f)
    for dy in range(3):
        for dx in range(3):
            window = pad[dy : dy + f.shape[0], dx : dx + f.shape[1]]
            gx += kx[dy, dx] * window
            gy += ky[dy, dx] * window
    return np.sqrt(gx**2 + gy**2)


def to_gray(img: np.ndarray) -> np.ndarray:
    """Conversion en niveaux de gris (luminance BT.601), tolérante au format."""
    arr = np.asarray(img)
    if arr.ndim == 2:
        return arr.astype(np.float32)
    if arr.ndim == 3 and arr.shape[2] >= 3:
        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        return (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float32)
    raise ValueError(f"format d'image non supporté: {arr.shape}")


__all__ = [
    "HAS_CV2",
    "HAS_SCIPY",
    "cv2",
    "ndimage",
    "binary_dilate",
    "binary_erode",
    "binary_open",
    "binary_close",
    "label_components",
    "gaussian_blur",
    "sobel_energy",
    "to_gray",
]
