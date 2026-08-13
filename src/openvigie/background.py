"""Modèle de fond par vue.

Le principe qui fait toute la différence sur un site fixe : le fond n'est pas
« l'image précédente » mais « ce que cette vue ressemble habituellement à cette
heure-ci, à cette saison, dans cet état jour/nuit ». C'est ce qui apprend qu'un
banc de brouillard de vallée apparaît tous les matins à 6 h 30 dans le secteur
sud-ouest, et qu'il ne faut donc pas alerter dessus.

Stockage : médiane glissante sur un tampon circulaire, en uint8, par clé.
Empreinte mémoire ~ n_slots * n_buffer * H * W octets — dimensionner le sous-
échantillonnage en conséquence sur carte caméra.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .compat import to_gray


def season_of(ts: dt.datetime) -> str:
    """Saison météorologique (les distributions de faux positifs y sont liées :
    écobuage en hiver, pollen au printemps, moissons en été)."""
    m = ts.month
    if m in (12, 1, 2):
        return "hiver"
    if m in (3, 4, 5):
        return "printemps"
    if m in (6, 7, 8):
        return "ete"
    return "automne"


def daynight_of(ts: dt.datetime, sunrise_h: float = 7.0, sunset_h: float = 21.0) -> str:
    """État jour / crépuscule / nuit.

    Le crépuscule est traité comme un état à part entière : c'est la période où
    la commutation du filtre IR change brutalement les statistiques d'image et
    où la quasi-totalité des faux positifs nocturnes se déclenchent.
    """
    h = ts.hour + ts.minute / 60.0
    if sunrise_h - 0.75 <= h < sunrise_h + 0.75 or sunset_h - 0.75 <= h < sunset_h + 0.75:
        return "crepuscule"
    if sunrise_h + 0.75 <= h < sunset_h - 0.75:
        return "jour"
    return "nuit"


@dataclass(frozen=True)
class BackgroundKey:
    view_id: str
    daynight: str
    hour_bucket: int
    season: str

    def as_str(self) -> str:
        return f"{self.view_id}|{self.daynight}|{self.hour_bucket:02d}|{self.season}"

    @classmethod
    def build(
        cls,
        view_id: str,
        ts: dt.datetime,
        hour_bucket_size: int = 2,
        sunrise_h: float = 7.0,
        sunset_h: float = 21.0,
    ) -> BackgroundKey:
        """AUDIT P1-18 (corrigé 0.4.0) : ``sunrise_h``/``sunset_h`` figuraient
        dans la configuration du site mais n'étaient jamais transmis ici — un
        site alpin en décembre et un site corse en juin partageaient les mêmes
        bornes jour/nuit, donc des clés de fond mal classées."""
        return cls(
            view_id=view_id,
            daynight=daynight_of(ts, sunrise_h, sunset_h),
            hour_bucket=(ts.hour // hour_bucket_size) * hour_bucket_size,
            season=season_of(ts),
        )


class BackgroundBank:
    """Banque de références, une médiane glissante par clé.

    AUDIT P0-08 (corrigé 0.4.0). Deux défauts de dimensionnement mémoire :

    - le commentaire annonçait un stockage ``uint8`` mais l'implémentation
      convertissait en ``float32``, soit **quatre fois** l'empreinte annoncée :
      173 Mio pour une seule clé à 5 MP avec 9 images ;
    - aucune éviction n'était appliquée sur le nombre de clés, alors qu'il y en a
      une par vue × créneau horaire × saison × état jour/nuit — plusieurs
      centaines sur une année.

    Les images sont désormais stockées en ``uint8`` (la précision d'un capteur
    8 bits, donc aucune perte réelle) et le nombre de clés est borné par une
    éviction du moins récemment utilisé.
    """

    def __init__(
        self, buffer_size: int = 9, min_samples: int = 3, max_keys: int = 64
    ) -> None:
        if buffer_size < 1:
            raise ValueError("buffer_size doit être >= 1")
        if max_keys < 1:
            raise ValueError("max_keys doit être >= 1")
        self.buffer_size = buffer_size
        self.min_samples = min_samples
        self.max_keys = max_keys
        self._buffers: dict[str, list[np.ndarray]] = {}
        self._last_used: dict[str, int] = {}
        self._tick = 0

    def _touch(self, key_str: str) -> None:
        self._tick += 1
        self._last_used[key_str] = self._tick
        if len(self._buffers) > self.max_keys:
            oldest = min(self._last_used, key=lambda k: self._last_used[k])
            self._buffers.pop(oldest, None)
            self._last_used.pop(oldest, None)

    def update(self, key: BackgroundKey, frame: np.ndarray) -> None:
        gray = np.clip(to_gray(frame), 0, 255).astype(np.uint8)
        buf = self._buffers.setdefault(key.as_str(), [])
        buf.append(gray)
        if len(buf) > self.buffer_size:
            buf.pop(0)
        self._touch(key.as_str())

    def reference(self, key: BackgroundKey) -> np.ndarray | None:
        """Référence médiane, ou ``None`` si la clé n'est pas encore mûre.

        Renvoyer ``None`` plutôt qu'une référence bancale est volontaire : un
        modèle de fond immature produit des dizaines de faux candidats.
        """
        buf = self._buffers.get(key.as_str())
        if not buf or len(buf) < self.min_samples:
            return None
        self._touch(key.as_str())
        return np.median(np.stack(buf, axis=0), axis=0).astype(np.float32)

    def is_ready(self, key: BackgroundKey) -> bool:
        return self.reference(key) is not None

    def maturity(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._buffers.items()}

    # -- persistance -------------------------------------------------------- #
    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays: dict[str, np.ndarray] = {}
        meta: dict[str, int] = {}
        for i, (key, buf) in enumerate(self._buffers.items()):
            if not buf:
                continue
            arrays[f"a{i}"] = np.stack(buf, axis=0).astype(np.uint8)
            meta[f"a{i}"] = key
        np.savez_compressed(
            path,
            _meta=np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8),
            _cfg=np.array([self.buffer_size, self.min_samples], dtype=np.int32),
            **arrays,
        )

    @classmethod
    def load(cls, path: str | Path) -> BackgroundBank:
        data = np.load(path, allow_pickle=False)
        cfg = data["_cfg"]
        bank = cls(buffer_size=int(cfg[0]), min_samples=int(cfg[1]), max_keys=10_000)
        meta = json.loads(bytes(data["_meta"]).decode())
        for arr_name, key in meta.items():
            bank._buffers[key] = list(data[arr_name])
        return bank
