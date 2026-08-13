"""Contrôle PTZ et ordonnancement du balayage.

Deux règles non négociables encodées ici :

1. **Aucune analyse pendant un mouvement.** Le scheduler expose un état
   ``settling`` pendant lequel le pipeline refuse les images : un flux optique
   ou une différence au fond calculés pendant un déplacement ne mesurent que le
   déplacement.

2. **Le budget de mouvements est compté.** ``ScanScheduler.moves_per_year``
   remonte le chiffre qui condamne la plupart des têtes PTZ grand public
   (~2 millions de mouvements/an pour 8 presets à 2 min de cycle).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .geometry import ViewPlan, scan_budget


# --------------------------------------------------------------------------- #
# Pelco-D
# --------------------------------------------------------------------------- #
def pelco_d_frame(address: int, cmd1: int, cmd2: int, data1: int, data2: int) -> bytes:
    """Construit une trame Pelco-D de 7 octets.

    Format : 0xFF, adresse, cmd1, cmd2, data1, data2, checksum
    Le checksum est la somme des octets 2..6 modulo 256.
    """
    if not 1 <= address <= 255:
        raise ValueError("adresse Pelco-D hors plage (1-255)")
    for value, label in ((cmd1, "cmd1"), (cmd2, "cmd2"), (data1, "data1"), (data2, "data2")):
        if not 0 <= value <= 255:
            raise ValueError(f"{label} hors plage (0-255)")
    body = bytes([address, cmd1, cmd2, data1, data2])
    checksum = sum(body) % 256
    return bytes([0xFF]) + body + bytes([checksum])


def pelco_d_goto_preset(address: int, preset: int) -> bytes:
    """Rappel de preset (cmd2 = 0x07)."""
    if not 1 <= preset <= 255:
        raise ValueError("numéro de preset hors plage (1-255)")
    return pelco_d_frame(address, 0x00, 0x07, 0x00, preset)


def pelco_d_set_preset(address: int, preset: int) -> bytes:
    """Enregistrement de preset (cmd2 = 0x03)."""
    if not 1 <= preset <= 255:
        raise ValueError("numéro de preset hors plage (1-255)")
    return pelco_d_frame(address, 0x00, 0x03, 0x00, preset)


def pelco_d_stop(address: int) -> bytes:
    return pelco_d_frame(address, 0x00, 0x00, 0x00, 0x00)


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class PtzBackend(ABC):
    name = "base"

    @abstractmethod
    def goto_preset(self, preset: int) -> bool:
        ...

    def set_zoom(self, focal_mm: float) -> bool:
        return False

    def position(self) -> tuple[float, float] | None:
        return None

    def close(self) -> None:
        return None


class SimulatedPtz(PtzBackend):
    """Tête simulée, avec erreur de répétabilité configurable.

    Sert aux tests et à la mise au point du scheduler sans matériel. La
    répétabilité par défaut (0,2°) est un ordre de grandeur réaliste pour une
    tête à vis sans fin sans encodeur absolu.
    """

    name = "simulated"

    def __init__(self, repeatability_deg: float = 0.2, slew_speed_deg_s: float = 40.0, seed: int = 0) -> None:
        import random

        self.repeatability_deg = repeatability_deg
        self.slew_speed_deg_s = slew_speed_deg_s
        self._rng = random.Random(seed)
        self.current_preset: int | None = None
        self.move_count = 0
        self.commanded: list[int] = []

    def goto_preset(self, preset: int) -> bool:
        self.current_preset = preset
        self.move_count += 1
        self.commanded.append(preset)
        return True

    def actual_error_deg(self) -> float:
        return self._rng.gauss(0.0, self.repeatability_deg / 2.0)

    def slew_time_s(self, from_az: float, to_az: float) -> float:
        delta = abs((to_az - from_az + 180) % 360 - 180)
        return delta / max(self.slew_speed_deg_s, 1e-6)


class SerialPelcoPtz(PtzBackend):  # pragma: no cover - nécessite le matériel
    """Tête pilotée en Pelco-D sur RS485 (positionneurs industriels de la BOM)."""

    name = "pelco_d"

    def __init__(self, port: str = "/dev/ttyS1", baudrate: int = 2400, address: int = 1) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("backend pelco_d: `pip install pyserial`") from exc
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=1.0)
        self.address = address

    def goto_preset(self, preset: int) -> bool:
        self._serial.write(pelco_d_goto_preset(self.address, preset))
        return True

    def close(self) -> None:
        self._serial.close()


class CgiPtz(PtzBackend):  # pragma: no cover - nécessite le matériel
    """Blocs SMTSEC / caméras ONVIF pilotées par CGI HTTP."""

    name = "cgi"

    def __init__(self, base_url: str, user: str = "admin", password: str = "", timeout_s: float = 3.0) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("backend cgi: `pip install requests`") from exc
        self._requests = requests
        self.base_url = base_url.rstrip("/")
        self.auth = (user, password)
        self.timeout_s = timeout_s

    def goto_preset(self, preset: int) -> bool:
        url = f"{self.base_url}/cgi-bin/ptz.cgi?action=goto&preset={preset}"
        try:
            r = self._requests.get(url, auth=self.auth, timeout=self.timeout_s)
            return r.status_code == 200
        except Exception:
            return False

    def set_zoom(self, focal_mm: float) -> bool:
        url = f"{self.base_url}/cgi-bin/ptz.cgi?action=zoom&focal={focal_mm:.1f}"
        try:
            return self._requests.get(url, auth=self.auth, timeout=self.timeout_s).status_code == 200
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# Ordonnanceur
# --------------------------------------------------------------------------- #
@dataclass
class ScanSlot:
    view: ViewPlan
    preset: int
    t_arrive_s: float
    t_settled_s: float
    t_leave_s: float

    @property
    def analysis_window_s(self) -> tuple[float, float]:
        """Fenêtre pendant laquelle les images sont exploitables."""
        return (self.t_settled_s, self.t_leave_s)


@dataclass
class ScanScheduler:
    """Planifie la ronde d'un PTZ sur une liste de vues.

    ``priority_views`` permet de doubler la fréquence de visite de certains
    secteurs (par exemple ceux orientés vers l'interface habitat-forêt, ou ceux
    sous le vent, ou ceux en risque rouge selon l'indice de danger du jour).
    """

    views: list[ViewPlan]
    dwell_s: float = 12.0
    settle_s: float = 3.0
    priority_views: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.views:
            raise ValueError("aucune vue à balayer")
        if self.dwell_s <= 0 or self.settle_s < 0:
            raise ValueError("dwell_s doit être > 0 et settle_s >= 0")

    @property
    def preset_map(self) -> dict[str, int]:
        """Numéro de preset **physique** de chaque vue.

        AUDIT P0-14 (corrigé 0.4.0) : le preset était auparavant déduit du rang
        dans la séquence de visite. Or les vues prioritaires y sont dupliquées :
        une même vue recevait donc plusieurs numéros de preset, et commandait la
        tête vers des positions qui n'avaient jamais été enregistrées. Le preset
        est désormais une propriété de la vue, indépendante de l'ordonnancement.
        """
        return {v.view_id: i + 1 for i, v in enumerate(self.views)}

    @property
    def sequence(self) -> list[ViewPlan]:
        """Ordre de visite, en intercalant les vues prioritaires."""
        if not self.priority_views:
            return list(self.views)
        prio = [v for v in self.views if v.view_id in self.priority_views]
        out: list[ViewPlan] = []
        for i, v in enumerate(self.views):
            out.append(v)
            if prio:
                out.append(prio[i % len(prio)])
        return out

    @property
    def budget(self):
        return scan_budget(len(self.sequence), self.dwell_s, self.settle_s, is_ptz=True)

    @property
    def cycle_s(self) -> float:
        return self.budget.cycle_s

    @property
    def moves_per_year(self) -> float:
        return self.budget.moves_per_year

    def plan_cycle(self, t0: float = 0.0) -> list[ScanSlot]:
        """Déroule un cycle complet, horodaté."""
        slots: list[ScanSlot] = []
        presets = self.preset_map
        t = t0
        for view in self.sequence:
            arrive = t
            settled = arrive + self.settle_s
            leave = settled + self.dwell_s
            slots.append(ScanSlot(
                view=view, preset=presets[view.view_id],
                t_arrive_s=arrive, t_settled_s=settled, t_leave_s=leave,
            ))
            t = leave
        return slots

    def run(self, backend: PtzBackend, on_view, cycles: int = 1, sleep=time.sleep) -> None:
        """Exécute ``cycles`` rondes.

        ``on_view(view, preset)`` est appelé **après** stabilisation uniquement.
        ``sleep`` est injectable pour rendre la boucle testable sans attendre.
        """
        for _ in range(cycles):
            for slot in self.plan_cycle():
                backend.goto_preset(slot.preset)
                sleep(self.settle_s)
                on_view(slot.view, slot.preset)
                sleep(self.dwell_s)


def health_warnings(scheduler: ScanScheduler, max_moves_per_year: float = 500_000.0) -> list[str]:
    """Avertissements de conception sur un plan de balayage."""
    warns: list[str] = []
    b = scheduler.budget
    if b.moves_per_year > max_moves_per_year:
        warns.append(
            f"{b.moves_per_year:,.0f} mouvements/an : au-delà de la tenue d'une tête PTZ "
            f"standard ({max_moves_per_year:,.0f}). Préférer des caméras fixes pour la "
            f"détection et réserver le PTZ à la confirmation."
        )
    if b.cycle_s > 300:
        warns.append(
            f"cycle de {b.cycle_s / 60:.1f} min : la latence de détection sera dominée par "
            f"le balayage, pas par le modèle."
        )
    if scheduler.dwell_s < 9:
        warns.append(
            f"dwell de {scheduler.dwell_s:.0f} s : moins de 3 images exploitables par visite, "
            f"les features temporelles (croissance, ascendance) seront instables."
        )
    return warns
