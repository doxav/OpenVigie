"""Backends de classification / détection.

Le projet est sous Apache-2.0. Les backends s'appuyant sur des dépendances
AGPL-3.0 (Ultralytics) sont des **greffons optionnels installés par
l'utilisateur** : ils ne sont pas importés par défaut et ne sont pas une
dépendance du paquet. Cela laisse le choix au déployeur — un projet associatif
ou un SDIS peut utiliser Ultralytics sans difficulté, une intégration dans un
produit fermé non.

Backends fournis :
  ``classical``   aucune dépendance ML — features manuelles + logistique.
                  C'est le backend du tier MINIMAL et le secours du tier MEDIUM.
  ``onnx``        onnxruntime, poids Apache-2.0 (RTMDet-tiny, D-FINE, YOLOX...).
  ``ultralytics`` greffon AGPL, permet d'utiliser directement les poids
                  Pyronear entraînés sur Pyro-SDIS.
  ``nnie``        délègue à un binaire d'inférence embarqué sur HI3516AV300.
  ``null``        ne classe rien (score neutre), pour isoler l'étage classique.
"""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from .compat import sobel_energy, to_gray


@dataclass
class Detection:
    bbox: tuple[int, int, int, int]
    score: float
    label: str = "smoke"


class BaseDetector(ABC):
    """Un détecteur note une ROI ; il ne décide jamais seul."""

    name: str = "base"

    @abstractmethod
    def score_roi(self, roi: np.ndarray) -> float:
        """Probabilité que la ROI contienne de la fumée, dans [0, 1]."""

    def detect(self, frame: np.ndarray) -> list[Detection]:  # pragma: no cover - optionnel
        """Détection plein cadre. Non requise : le pipeline fonctionne en mode
        ROI-first, ce qui divise la charge par un ordre de grandeur."""
        return []

    def warmup(self) -> None:
        return None


class NullDetector(BaseDetector):
    """Score neutre. Utile pour mesurer ce que l'étage classique fait seul."""

    name = "null"

    def score_roi(self, roi: np.ndarray) -> float:
        return 0.5


class ClassicalDetector(BaseDetector):
    """Classifieur sans dépendance ML, sur features manuelles.

    Inspiré des approches HOG/HOOF + forêt aléatoire, réduit à ce qui est
    calculable en quelques millisecondes sur un cœur ARM. La fumée est
    caractérisée par : faible saturation, texture pauvre en contours francs,
    histogramme resserré, et gradient dominant vertical diffus.
    """

    name = "classical"

    def __init__(self, weights: dict[str, float] | None = None, bias: float = -1.0) -> None:
        self.weights = weights or {
            "low_saturation": 1.4,
            "edge_poverty": 1.6,
            "hist_narrow": 0.8,
            "vertical_diffusion": 1.0,
        }
        self.bias = bias

    @staticmethod
    def features(roi: np.ndarray) -> dict[str, float]:
        arr = np.asarray(roi, dtype=np.float32)
        gray = to_gray(arr)
        if gray.size < 16:
            return {"low_saturation": 0.0, "edge_poverty": 0.0, "hist_narrow": 0.0, "vertical_diffusion": 0.0}

        if arr.ndim == 3 and arr.shape[2] >= 3:
            mx = arr[..., :3].max(axis=2)
            mn = arr[..., :3].min(axis=2)
            sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
            low_sat = float(np.clip(1.0 - sat.mean() * 4.0, 0.0, 1.0))
        else:
            low_sat = 0.7  # pas d'information chromatique (nuit / mono)

        edges = sobel_energy(gray)
        edge_poverty = float(np.clip(1.0 - edges.mean() / 40.0, 0.0, 1.0))

        std = float(gray.std())
        hist_narrow = float(np.clip(1.0 - std / 60.0, 0.0, 1.0))

        gy = np.diff(gray, axis=0)
        gx = np.diff(gray, axis=1)
        vy, vx = float(np.abs(gy).mean()), float(np.abs(gx).mean())
        vertical = vy / (vy + vx) if (vy + vx) > 1e-6 else 0.5
        vertical_diffusion = float(np.clip((vertical - 0.35) / 0.4, 0.0, 1.0))

        return {
            "low_saturation": low_sat,
            "edge_poverty": edge_poverty,
            "hist_narrow": hist_narrow,
            "vertical_diffusion": vertical_diffusion,
        }

    def score_roi(self, roi: np.ndarray) -> float:
        f = self.features(roi)
        z = self.bias + sum(self.weights.get(k, 0.0) * v for k, v in f.items())
        z = min(max(z, -60.0), 60.0)
        return float(1.0 / (1.0 + np.exp(-z)))


class OnnxDetector(BaseDetector):  # pragma: no cover - dépend d'onnxruntime
    """Classifieur ROI ONNX. Backend recommandé pour les tiers MEDIUM et FULL."""

    name = "onnx"

    def __init__(self, model_path: str, input_size: int = 224, providers: list[str] | None = None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("backend onnx: `pip install onnxruntime`") from exc
        self.session = ort.InferenceSession(model_path, providers=providers or ["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.input_size = input_size

    def _preprocess(self, roi: np.ndarray) -> np.ndarray:
        from .compat import HAS_CV2, cv2

        arr = np.asarray(roi, dtype=np.float32)
        if arr.ndim == 2:
            arr = np.repeat(arr[..., None], 3, axis=2)
        if HAS_CV2:
            arr = cv2.resize(arr, (self.input_size, self.input_size))
        else:
            yi = np.linspace(0, arr.shape[0] - 1, self.input_size).astype(int)
            xi = np.linspace(0, arr.shape[1] - 1, self.input_size).astype(int)
            arr = arr[np.ix_(yi, xi)]
        arr = arr / 255.0
        return arr.transpose(2, 0, 1)[None].astype(np.float32)

    def score_roi(self, roi: np.ndarray) -> float:
        out = self.session.run(None, {self.input_name: self._preprocess(roi)})[0]
        v = np.asarray(out).ravel()
        if v.size == 1:
            return float(1.0 / (1.0 + np.exp(-v[0])))
        e = np.exp(v - v.max())
        return float((e / e.sum())[-1])


class UltralyticsDetector(BaseDetector):  # pragma: no cover - greffon AGPL optionnel
    """Greffon AGPL-3.0. Permet d'utiliser directement les poids Pyronear.

    ⚠️ Installer ``ultralytics`` place votre déploiement sous AGPL-3.0. Pour un
    projet open source c'est sans difficulté ; c'est documenté ici pour que le
    choix soit conscient.
    """

    name = "ultralytics"

    def __init__(self, model_path: str, conf: float = 0.1) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "backend ultralytics: `pip install ultralytics` (AGPL-3.0)"
            ) from exc
        self.model = YOLO(model_path)
        self.conf = conf

    def score_roi(self, roi: np.ndarray) -> float:
        res = self.model.predict(np.asarray(roi), conf=self.conf, verbose=False)
        if not res or res[0].boxes is None or len(res[0].boxes) == 0:
            return 0.0
        return float(max(float(b) for b in res[0].boxes.conf))

    def detect(self, frame: np.ndarray) -> list[Detection]:
        res = self.model.predict(np.asarray(frame), conf=self.conf, verbose=False)
        out: list[Detection] = []
        if not res or res[0].boxes is None:
            return out
        for box in res[0].boxes:
            x0, y0, x1, y1 = (int(v) for v in box.xyxy[0].tolist())
            out.append(Detection(bbox=(x0, y0, x1, y1), score=float(box.conf)))
        return out


class NnieDetector(BaseDetector):  # pragma: no cover - nécessite le SoC
    """Délègue à un binaire d'inférence NNIE sur HI3516AV300.

    Le binaire reçoit un chemin d'image et renvoie un JSON ``{"score": float}``.
    Cette indirection évite de lier le pipeline Python au SDK HiSilicon et
    permet de tester tout le reste sur PC.
    """

    name = "nnie"

    def __init__(self, binary: str = "/usr/bin/openvigie_nnie", timeout_s: float = 2.0) -> None:
        import os

        if not os.access(binary, os.X_OK):
            raise RuntimeError(
                f"backend nnie: binaire d'inférence introuvable ou non exécutable ({binary}). "
                "Sur une carte sans NNIE opérationnel, le pipeline se replie sur 'classical'."
            )
        self.binary = binary
        self.timeout_s = timeout_s

    def score_roi(self, roi: np.ndarray) -> float:
        import tempfile

        from .compat import HAS_CV2, cv2

        if not HAS_CV2:
            raise RuntimeError("backend nnie: OpenCV requis pour sérialiser la ROI")
        with tempfile.NamedTemporaryFile(suffix=".bmp", delete=True) as tmp:
            cv2.imwrite(tmp.name, np.asarray(roi).astype(np.uint8))
            try:
                proc = subprocess.run(
                    [self.binary, tmp.name], capture_output=True, timeout=self.timeout_s, check=True
                )
            except (OSError, subprocess.SubprocessError):
                return 0.0
        try:
            return float(json.loads(proc.stdout.decode()).get("score", 0.0))
        except (ValueError, json.JSONDecodeError):
            return 0.0


_REGISTRY = {
    "null": NullDetector,
    "classical": ClassicalDetector,
    "onnx": OnnxDetector,
    "ultralytics": UltralyticsDetector,
    "nnie": NnieDetector,
}


def get_detector(name: str, **kwargs) -> BaseDetector:
    """Fabrique de backends."""
    if name not in _REGISTRY:
        raise ValueError(f"backend inconnu '{name}' (disponibles: {sorted(_REGISTRY)})")
    return _REGISTRY[name](**kwargs)


def available_backends() -> list[str]:
    return sorted(_REGISTRY)
