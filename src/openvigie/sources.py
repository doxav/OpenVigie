"""Sources d'images.

AUDIT P0-12 (corrigé 0.4.0) : toutes les sources produisent désormais des
horodatages **UTC conscients du fuseau**. Auparavant ``datetime.now()`` renvoyait
une date naïve, ensuite traitée comme de l'UTC : deux tours dans des fuseaux
différents, ou un simple passage à l'heure d'été, faussaient silencieusement la
corrélation multi-tours — et la corrélation est justement ce qui fonde la
triangulation.

Règle du projet : **on analyse des snapshots JPEG de haute qualité, pas un flux
H.265**. Le générateur synthétique sert aux tests et aux démonstrations, et
permet de valider tout le pipeline sans matériel — y compris les cas négatifs
(nuage au-dessus de l'horizon) qui sont les plus difficiles à collecter.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np


class FrameSource(ABC):
    @abstractmethod
    def read(self) -> tuple[np.ndarray, dt.datetime] | None:
        ...

    def close(self) -> None:
        return None


class FileSequenceSource(FrameSource):
    """Séquence d'images sur disque, triée par nom."""

    def __init__(self, directory: str | Path, pattern: str = "*.jpg", start: dt.datetime | None = None, period_s: float = 30.0) -> None:
        self.paths = sorted(Path(directory).glob(pattern))
        self.index = 0
        self.start = start or dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.period_s = period_s

    def read(self):
        from .compat import HAS_CV2, cv2

        if self.index >= len(self.paths):
            return None
        path = self.paths[self.index]
        ts = self.start + dt.timedelta(seconds=self.index * self.period_s)
        self.index += 1
        if not HAS_CV2:  # pragma: no cover
            raise RuntimeError("FileSequenceSource requiert OpenCV")
        img = cv2.imread(str(path))
        if img is None:  # pragma: no cover
            return None
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB), ts


class SnapshotHttpSource(FrameSource):  # pragma: no cover - I/O réseau
    """Snapshot HTTP (chemin recommandé : pas de recompression vidéo)."""

    def __init__(self, url: str, user: str = "admin", password: str = "", period_s: float = 30.0) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("SnapshotHttpSource: `pip install requests`") from exc
        self._requests = requests
        self.url = url
        self.auth = (user, password)
        self.period_s = period_s

    def read(self):
        from .compat import HAS_CV2, cv2

        if not HAS_CV2:
            raise RuntimeError("SnapshotHttpSource requiert OpenCV pour décoder")
        r = self._requests.get(self.url, auth=self.auth, timeout=10)
        if r.status_code != 200:
            return None
        buf = np.frombuffer(r.content, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return None
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB), dt.datetime.now(dt.timezone.utc)


class RtspSource(FrameSource):  # pragma: no cover - I/O réseau
    """Repli RTSP, à n'utiliser que si les snapshots ne sont pas disponibles."""

    def __init__(self, url: str) -> None:
        from .compat import HAS_CV2, cv2

        if not HAS_CV2:
            raise RuntimeError("RtspSource requiert OpenCV")
        self.cap = cv2.VideoCapture(url)
        self._cv2 = cv2

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return None
        return self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB), dt.datetime.now(dt.timezone.utc)

    def close(self) -> None:
        self.cap.release()


# --------------------------------------------------------------------------- #
# Générateur synthétique
# --------------------------------------------------------------------------- #
class SyntheticScene:
    """Scène de test : relief en bas, ciel en haut, texture stable.

    Le panache est translucide (mélange alpha avec le fond), ce qui reproduit la
    propriété essentielle d'une fumée naissante : elle *atténue* le décor sans
    le remplacer.
    """

    def __init__(self, height: int = 180, width: int = 320, horizon_row: int = 90, seed: int = 42) -> None:
        self.height = height
        self.width = width
        self.horizon_row = horizon_row
        rng = np.random.default_rng(seed)
        base = np.zeros((height, width), dtype=np.float32)
        # ciel : dégradé lisse
        base[:horizon_row] = np.linspace(200, 165, horizon_row)[:, None]
        # relief : texture contrastée stable (arbres, crêtes)
        ground = 90 + 45 * rng.random((height - horizon_row, width)).astype(np.float32)
        ground += 25 * np.sin(np.linspace(0, 14, width))[None, :]
        base[horizon_row:] = ground
        self.base = np.clip(base, 0, 255)
        self._rng = rng

    def frame(self, noise: float = 1.5) -> np.ndarray:
        img = self.base + self._rng.normal(0, noise, self.base.shape).astype(np.float32)
        return np.clip(np.repeat(img[..., None], 3, axis=2), 0, 255).astype(np.uint8)

    def with_plume(
        self, cx: int, base_row: int, width_px: float, height_px: float,
        opacity: float = 0.35, noise: float = 1.5,
    ) -> np.ndarray:
        """Panache translucide ancré au sol, s'élevant depuis ``base_row``."""
        img = self.frame(noise).astype(np.float32)
        yy, xx = np.mgrid[0 : self.height, 0 : self.width]
        top = base_row - height_px
        sigma_x = max(width_px / 2.0, 1.0)
        sigma_y = max(height_px / 2.0, 1.0)
        cy = (base_row + top) / 2.0
        blob = np.exp(-(((xx - cx) ** 2) / (2 * sigma_x**2) + ((yy - cy) ** 2) / (2 * sigma_y**2)))
        blob[yy > base_row] = 0.0
        alpha = np.clip(blob * opacity, 0, 1)[..., None]
        smoke = np.full_like(img, 205.0)
        return np.clip(img * (1 - alpha) + smoke * alpha, 0, 255).astype(np.uint8)

    def with_cloud(self, cx: int, cy: int, radius: float, opacity: float = 0.5, noise: float = 1.5) -> np.ndarray:
        """Nuage : même apparence, mais **entièrement au-dessus de l'horizon**.

        C'est le négatif dur de référence : seul le test d'origine au sol le
        distingue d'un panache.
        """
        img = self.frame(noise).astype(np.float32)
        yy, xx = np.mgrid[0 : self.height, 0 : self.width]
        blob = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * radius**2)))
        blob[yy > self.horizon_row - 6] = 0.0
        alpha = np.clip(blob * opacity, 0, 1)[..., None]
        cloud = np.full_like(img, 235.0)
        return np.clip(img * (1 - alpha) + cloud * alpha, 0, 255).astype(np.uint8)


class SyntheticSource(FrameSource):
    """Séquence synthétique : ``n_background`` images stables puis un panache croissant."""

    def __init__(
        self, scene: SyntheticScene | None = None, n_background: int = 6, n_plume: int = 6,
        period_s: float = 30.0, start: dt.datetime | None = None,
        growth_px_per_step: float = 6.0, mode: str = "plume",
    ) -> None:
        self.scene = scene or SyntheticScene()
        self.n_background = n_background
        self.n_plume = n_plume
        self.period_s = period_s
        self.start = start or dt.datetime(2026, 8, 1, 14, 0, 0, tzinfo=dt.timezone.utc)
        self.growth = growth_px_per_step
        self.mode = mode
        self.i = 0

    def read(self):
        if self.i >= self.n_background + self.n_plume:
            return None
        ts = self.start + dt.timedelta(seconds=self.i * self.period_s)
        if self.i < self.n_background:
            frame = self.scene.frame()
        else:
            k = self.i - self.n_background + 1
            if self.mode == "cloud":
                frame = self.scene.with_cloud(
                    cx=self.scene.width // 2, cy=self.scene.horizon_row // 2,
                    radius=10 + self.growth * k,
                )
            else:
                frame = self.scene.with_plume(
                    cx=self.scene.width // 2,
                    base_row=self.scene.horizon_row + 20,
                    width_px=10 + self.growth * k,
                    height_px=14 + 1.8 * self.growth * k,
                )
        self.i += 1
        return frame, ts
