"""Connectivité : store-and-forward, transports et supervision.

Une tour perd son lien. C'est un fait, pas un incident exceptionnel : liaison
radio saturée l'été, coupure d'alimentation, opérateur en maintenance. Le
système doit continuer à détecter et à **conserver** ses alertes, puis les
rejouer dans l'ordre au retour du réseau.

Trois briques, toutes sans dépendance obligatoire :

  ``Outbox``      file durable sur disque, réémission à intervalles croissants,
                  suivi des accusés de réception, purge bornée ;
  ``Transport``   destination des événements (fichier, HTTP, mémoire) ;
  ``HealthMonitor`` battement de cœur et état du site — savoir qu'une caméra est
                  sale, floue ou hors ligne vaut mieux qu'un faux sentiment de
                  sécurité.

Aucun flux vidéo permanent ne remonte : 8 caméras envoyant un JPEG de 500 ko
toutes les 30 s représentent déjà ~11 Go/jour. L'architecture est donc analyse
locale, remontée **événementielle**.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .events import DetectionEvent


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
class Transport(ABC):
    """Destination d'un événement. ``send`` renvoie True si l'envoi est acquitté."""

    name = "base"

    @abstractmethod
    def send(self, event: DetectionEvent) -> bool:
        ...

    def send_health(self, snapshot: dict) -> bool:
        return False

    def close(self) -> None:
        return None


class MemoryTransport(Transport):
    """Transport en mémoire, pour les tests et le mode autonome."""

    name = "memory"

    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[DetectionEvent] = []
        self.health: list[dict] = []
        self.fail_times = fail_times
        self.attempts = 0

    def send(self, event: DetectionEvent) -> bool:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            return False
        self.sent.append(event)
        return True

    def send_health(self, snapshot: dict) -> bool:
        self.health.append(snapshot)
        return True


class FileTransport(Transport):
    """Écriture en JSONL local.

    Utile bien au-delà des tests : un site autonome sans plateforme centrale
    reste parfaitement exploitable si ses événements sont journalisés et lus par
    un script, un tableau de bord local ou une astreinte.
    """

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, event: DetectionEvent) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(event.to_json() + "\n")
            return True
        except OSError:
            return False


class HttpTransport(Transport):  # pragma: no cover - I/O réseau
    """POST HTTPS avec jeton porteur.

    Aucune caméra n'est jamais exposée : c'est la passerelle du site qui sort
    vers la plateforme, jamais l'inverse. Voir docs/CONNECTIVITE.md.
    """

    name = "http"

    def __init__(
        self,
        url: str,
        token: str = "",
        timeout_s: float = 10.0,
        health_url: str | None = None,
        verify: bool | str = True,
    ) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("HttpTransport: `pip install requests`") from exc
        self._requests = requests
        self.url = url
        self.health_url = health_url
        self.timeout_s = timeout_s
        self.verify = verify
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _post(self, url: str, payload: dict) -> bool:
        try:
            r = self._requests.post(
                url, json=payload, headers=self.headers,
                timeout=self.timeout_s, verify=self.verify,
            )
            return 200 <= r.status_code < 300
        except Exception:
            return False

    def send(self, event: DetectionEvent) -> bool:
        return self._post(self.url, event.as_dict())

    def send_health(self, snapshot: dict) -> bool:
        return self._post(self.health_url or self.url, snapshot) if self.health_url else False


# --------------------------------------------------------------------------- #
# File d'attente durable
# --------------------------------------------------------------------------- #
@dataclass
class QueueEntry:
    """Un événement en attente d'émission."""

    event: DetectionEvent
    attempts: int = 0
    first_queued_at: float = field(default_factory=time.time)
    next_attempt_at: float = 0.0
    last_error: str = ""

    def as_dict(self) -> dict:
        return {
            "event": self.event.as_dict(),
            "attempts": self.attempts,
            "first_queued_at": self.first_queued_at,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> QueueEntry:
        return cls(
            event=DetectionEvent.from_dict(data["event"]),
            attempts=int(data.get("attempts", 0)),
            first_queued_at=float(data.get("first_queued_at", time.time())),
            next_attempt_at=float(data.get("next_attempt_at", 0.0)),
            last_error=data.get("last_error", ""),
        )


class Outbox:
    """File d'attente persistante avec réémission à intervalles croissants.

    La persistance sur disque est le point important : une coupure de courant ne
    doit pas effacer une alerte détectée mais non transmise. Chaque entrée est un
    fichier JSON distinct — plus robuste qu'un fichier unique réécrit, qui peut
    être tronqué par une coupure au mauvais moment.
    """

    def __init__(
        self,
        directory: str | Path,
        max_attempts: int = 12,
        base_backoff_s: float = 15.0,
        max_backoff_s: float = 900.0,
        max_entries: int = 5_000,
        clock=time.time,
    ) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self.max_entries = max_entries
        self.clock = clock
        # AUDIT P0-20 (corrigé 0.4.0) : les échecs définitifs n'existaient qu'en
        # mémoire et disparaissaient au redémarrage. Ils sont désormais écrits
        # sur disque, avec leur motif, et rejouables.
        self.dead_letter_dir = self.dir / "dead"
        self.dead_letter_dir.mkdir(parents=True, exist_ok=True)
        # AUDIT P0-19 : la saturation supprimait silencieusement les entrées les
        # plus anciennes. Elle est désormais comptée et remontée à la supervision.
        self.dropped_on_overflow = 0
        self.last_overflow_at: float | None = None

    # -- primitives --------------------------------------------------------- #
    def _path(self, event_id: str) -> Path:
        return self.dir / f"{event_id}.json"

    def _entries(self) -> list[QueueEntry]:
        out: list[QueueEntry] = []
        for p in sorted(self.dir.glob("*.json")):
            if p.parent != self.dir:
                continue
            try:
                out.append(QueueEntry.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                # Une entrée corrompue ne doit pas bloquer la file entière.
                p.rename(p.with_suffix(".corrupt"))
        return sorted(out, key=lambda e: e.first_queued_at)

    def _write(self, entry: QueueEntry) -> None:
        tmp = self._path(entry.event.event_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(entry.as_dict(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(entry.event.event_id))

    def _remove(self, event_id: str) -> None:
        self._path(event_id).unlink(missing_ok=True)

    # -- API ---------------------------------------------------------------- #
    def enqueue(self, event: DetectionEvent) -> bool:
        """Met un événement en file. Idempotent sur ``event_id``."""
        if self._path(event.event_id).exists():
            return False
        entries = self._entries()
        if len(entries) >= self.max_entries:
            # Saturation : on sacrifie les plus anciens, jamais les plus récents.
            # Une alerte d'il y a trois jours a beaucoup moins de valeur qu'une
            # alerte d'il y a trois minutes.
            for old in entries[: len(entries) - self.max_entries + 1]:
                # On archive avant de retirer : une alerte sacrifiée doit rester
                # auditable, même si elle n'est plus transmise (AUDIT P0-19).
                self._to_dead_letter(old, reason="saturation de la file")
                self._remove(old.event.event_id)
                self.dropped_on_overflow += 1
                self.last_overflow_at = self.clock()
        self._write(QueueEntry(event=event, next_attempt_at=self.clock()))
        return True

    def pending(self) -> list[QueueEntry]:
        return self._entries()

    def due(self) -> list[QueueEntry]:
        now = self.clock()
        return [e for e in self._entries() if e.next_attempt_at <= now]

    def __len__(self) -> int:
        return len(self._entries())

    def backoff_s(self, attempts: int) -> float:
        """Intervalle avant nouvelle tentative : doublement borné."""
        return min(self.base_backoff_s * (2 ** max(0, attempts - 1)), self.max_backoff_s)

    def flush(self, transport: Transport, limit: int = 50) -> dict:
        """Tente d'émettre les entrées échues. Ne lève jamais.

        Renvoie un compte-rendu exploitable par la supervision.
        """
        sent = failed = dropped = 0
        for entry in self.due()[:limit]:
            ok = False
            try:
                ok = transport.send(entry.event)
            except Exception as exc:  # un transport défaillant ne doit rien casser
                entry.last_error = f"{type(exc).__name__}: {exc}"
            if ok:
                entry.event.mark_transmitted()
                self._remove(entry.event.event_id)
                sent += 1
                continue
            entry.attempts += 1
            entry.next_attempt_at = self.clock() + self.backoff_s(entry.attempts)
            if entry.attempts >= self.max_attempts:
                self._to_dead_letter(entry, reason=f"{entry.attempts} tentatives échouées")
                self._remove(entry.event.event_id)
                dropped += 1
            else:
                self._write(entry)
                failed += 1
        return {
            "sent": sent,
            "retried": failed,
            "dead_lettered": dropped,
            "remaining": len(self),
        }

    def _to_dead_letter(self, entry: QueueEntry, reason: str) -> None:
        payload = entry.as_dict()
        payload["dead_letter_reason"] = reason
        payload["dead_lettered_at"] = self.clock()
        path = self.dead_letter_dir / f"{entry.event.event_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @property
    def dead_letters(self) -> list[QueueEntry]:
        """Échecs définitifs, relus depuis le disque."""
        out: list[QueueEntry] = []
        for p in sorted(self.dead_letter_dir.glob("*.json")):
            try:
                out.append(QueueEntry.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return out

    def replay_dead_letters(self, limit: int = 100) -> int:
        """Remet des échecs définitifs en file — reprise manuelle après incident."""
        replayed = 0
        for p in sorted(self.dead_letter_dir.glob("*.json"))[:limit]:
            try:
                entry = QueueEntry.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
            entry.attempts = 0
            entry.next_attempt_at = self.clock()
            self._write(entry)
            p.unlink(missing_ok=True)
            replayed += 1
        return replayed

    def stats(self) -> dict:
        entries = self._entries()
        now = self.clock()
        oldest = min((now - e.first_queued_at for e in entries), default=0.0)
        return {
            "pending": len(entries),
            "due": sum(1 for e in entries if e.next_attempt_at <= now),
            "oldest_age_s": round(oldest, 1),
            "dead_letters": len(list(self.dead_letter_dir.glob("*.json"))),
            "dropped_on_overflow": self.dropped_on_overflow,
            "max_attempts_reached": sum(1 for e in entries if e.attempts >= self.max_attempts - 1),
        }


# --------------------------------------------------------------------------- #
# Supervision
# --------------------------------------------------------------------------- #
@dataclass
class CameraHealth:
    """État d'une caméra, tel que remonté à la supervision."""

    camera_id: str
    online: bool = True
    last_frame_at: str | None = None
    frames_last_hour: int = 0
    image_status: str = "unknown"       # ok | warn | fail
    optics_status: str = "unknown"      # propreté du hublot
    alignment_px: float = 0.0
    background_ready: bool = False
    note: str = ""

    @property
    def degraded(self) -> bool:
        return (
            not self.online
            or self.image_status == "fail"
            or self.optics_status == "fail"
            or not self.background_ready
        )


@dataclass
class SiteHealth:
    """Battement de cœur d'un site."""

    site_id: str
    at: str
    software_version: str = "unknown"
    pipeline_tier: str = "unknown"
    uptime_s: float = 0.0
    cameras: list[CameraHealth] = field(default_factory=list)
    outbox: dict = field(default_factory=dict)
    detector_backend: str = "unknown"
    degraded_reason: str | None = None
    visibility_m: float | None = None
    disk_free_mb: float | None = None
    clock_source: str = "system"

    @property
    def status(self) -> str:
        """`ok` / `degraded` / `down`, dans l'esprit d'un indicateur de NOC."""
        if not self.cameras:
            return "down"
        offline = sum(1 for c in self.cameras if not c.online)
        if offline == len(self.cameras):
            return "down"
        if offline or any(c.degraded for c in self.cameras) or self.degraded_reason:
            return "degraded"
        return "ok"

    def as_dict(self) -> dict:
        return {**asdict(self), "status": self.status}


class HealthMonitor:
    """Suit l'état des caméras d'un site et produit les battements de cœur.

    Un site silencieux est indistinguable d'un site sans feu : c'est pourquoi le
    battement de cœur est aussi important que l'alerte elle-même.
    """

    def __init__(
        self,
        site_id: str,
        heartbeat_interval_s: float = 300.0,
        offline_after_s: float = 900.0,
        clock=time.time,
    ) -> None:
        self.site_id = site_id
        self.heartbeat_interval_s = heartbeat_interval_s
        self.offline_after_s = offline_after_s
        self.clock = clock
        self.started_at = clock()
        self._last_heartbeat = 0.0
        self._cameras: dict[str, dict] = {}

    def record_frame(
        self,
        camera_id: str,
        *,
        image_status: str = "ok",
        optics_status: str = "unknown",
        alignment_px: float = 0.0,
        background_ready: bool = False,
        note: str = "",
    ) -> None:
        state = self._cameras.setdefault(camera_id, {"times": []})
        now = self.clock()
        state["times"] = [t for t in state["times"] if now - t <= 3600.0] + [now]
        state.update(
            last_seen=now,
            image_status=image_status,
            optics_status=optics_status,
            alignment_px=alignment_px,
            background_ready=background_ready,
            note=note,
        )

    def camera_health(self) -> list[CameraHealth]:
        now = self.clock()
        out: list[CameraHealth] = []
        for cam_id, s in sorted(self._cameras.items()):
            last = s.get("last_seen", 0.0)
            out.append(
                CameraHealth(
                    camera_id=cam_id,
                    online=(now - last) <= self.offline_after_s,
                    last_frame_at=dt.datetime.fromtimestamp(last, dt.timezone.utc).isoformat()
                    if last else None,
                    frames_last_hour=len(s.get("times", [])),
                    image_status=s.get("image_status", "unknown"),
                    optics_status=s.get("optics_status", "unknown"),
                    alignment_px=round(float(s.get("alignment_px", 0.0)), 2),
                    background_ready=bool(s.get("background_ready", False)),
                    note=s.get("note", ""),
                )
            )
        return out

    def due(self) -> bool:
        return (self.clock() - self._last_heartbeat) >= self.heartbeat_interval_s

    def snapshot(self, **extra) -> SiteHealth:
        return SiteHealth(
            site_id=self.site_id,
            at=dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(),
            uptime_s=round(self.clock() - self.started_at, 1),
            cameras=self.camera_health(),
            **extra,
        )

    def beat(self, transport: Transport, **extra) -> dict | None:
        """Émet un battement de cœur si l'intervalle est écoulé."""
        if not self.due():
            return None
        snap = self.snapshot(**extra).as_dict()
        try:
            transport.send_health(snap)
        except Exception:
            pass  # un battement perdu ne doit jamais interrompre la détection
        self._last_heartbeat = self.clock()
        return snap


def free_disk_mb(path: str | Path = "/") -> float | None:
    """Espace disque libre, en Mo. ``None`` si indisponible."""
    try:
        import shutil as _sh

        return round(_sh.disk_usage(str(path)).free / (1024 * 1024), 1)
    except OSError:
        return None
