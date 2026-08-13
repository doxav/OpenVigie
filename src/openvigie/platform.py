"""Couche matérielle OpenIPC.

Le projet cible **OpenIPC** plutôt qu'un SoC particulier, pour une raison
simple : lier la détection à HI3516AV300 reviendrait à lier le projet à une
seule référence de module, d'un seul fournisseur, sur une génération de SoC déjà
figée. OpenIPC couvre HiSilicon, Goke, SigmaStar et Ingenic avec la même
interface (majestic pour le flux, `cli` pour la configuration, `ipctool` pour
l'inventaire matériel) : c'est cette interface qu'OpenVigie utilise.

Conséquence pratique : une carte devient utilisable dès que **(SoC supporté par
OpenIPC) ET (pilote capteur présent)**. Ces deux conditions sont indépendantes,
et ce module les expose séparément — c'est exactement la distinction qui décide
si une référence est achetable aujourd'hui ou nécessite un portage.

Les chiffres d'accélérateur sont **indicatifs** et doivent être confirmés sur
cible : ils servent à choisir un backend, pas à dimensionner un contrat.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Matrice de capacités des SoC supportés par OpenIPC
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SocCapabilities:
    """Ce qu'un SoC permet réellement de faire tourner localement."""

    soc: str
    family: str
    accelerator: str          # nnie | npu | none
    accel_note: str
    has_ive: bool             # moteur de vision matériel (diff, morpho, gradients)
    max_sensor_mp: float
    recommended_backend: str  # backend de détection conseillé
    openipc_supported: bool = True

    @property
    def can_run_cnn_locally(self) -> bool:
        return self.accelerator in ("nnie", "npu")

    def as_dict(self) -> dict:
        return {
            "soc": self.soc,
            "family": self.family,
            "accelerator": self.accelerator,
            "has_ive": self.has_ive,
            "max_sensor_mp": self.max_sensor_mp,
            "recommended_backend": self.recommended_backend,
            "can_run_cnn_locally": self.can_run_cnn_locally,
            "note": self.accel_note,
        }


SOC_MATRIX: dict[str, SocCapabilities] = {
    # --- HiSilicon ---------------------------------------------------------- #
    "hi3516av300": SocCapabilities(
        "hi3516av300", "hisilicon-cv500", "nnie",
        "NNIE + IVE présents (génération CV500). Chaîne d'outils RuyiStudio figée "
        "depuis ~2020 : jeu d'opérateurs restreint, INT8/INT16, pas de LSTM. "
        "Prévoir un budget de portage non négligeable.",
        has_ive=True, max_sensor_mp=8.0, recommended_backend="nnie",
    ),
    "hi3516cv500": SocCapabilities(
        "hi3516cv500", "hisilicon-cv500", "nnie",
        "Même génération que AV300, NNIE disponible.",
        has_ive=True, max_sensor_mp=5.0, recommended_backend="nnie",
    ),
    "hi3516ev300": SocCapabilities(
        "hi3516ev300", "hisilicon-ev300", "none",
        "Pas de NNIE sur cette déclinaison : étage classique uniquement.",
        has_ive=True, max_sensor_mp=5.0, recommended_backend="classical",
    ),
    "hi3516cv300": SocCapabilities(
        "hi3516cv300", "hisilicon-cv300", "none",
        "Génération antérieure, sans moteur neuronal.",
        has_ive=True, max_sensor_mp=4.0, recommended_backend="classical",
    ),
    # --- Goke (dérivés HiSilicon) ------------------------------------------- #
    "gk7605v100": SocCapabilities(
        "gk7605v100", "goke", "none",
        "Pas de moteur neuronal exploitable : la carte sert de capteur, "
        "la classification part sur un calculateur externe.",
        has_ive=True, max_sensor_mp=5.0, recommended_backend="classical",
    ),
    "gk7205v300": SocCapabilities(
        "gk7205v300", "goke", "none",
        "Idem gk7605v100.",
        has_ive=True, max_sensor_mp=4.0, recommended_backend="classical",
    ),
    # --- SigmaStar ---------------------------------------------------------- #
    "ssc338q": SocCapabilities(
        "ssc338q", "sigmastar", "none",
        "CPU nettement plus véloce que les HiSilicon d'entrée de gamme, "
        "mais pas de NPU : très bon support de l'étage classique.",
        has_ive=False, max_sensor_mp=5.0, recommended_backend="classical",
    ),
    "ssc30kq": SocCapabilities(
        "ssc30kq", "sigmastar", "none",
        "Sans NPU.",
        has_ive=False, max_sensor_mp=4.0, recommended_backend="classical",
    ),
    # --- Ingenic ------------------------------------------------------------ #
    "t31": SocCapabilities(
        "t31", "ingenic", "npu",
        "NPU Ingenic présent, SDK propriétaire (MagikTrainer). Conversion de "
        "modèle à valider avant de compter dessus.",
        has_ive=False, max_sensor_mp=4.0, recommended_backend="classical",
    ),
    "t41": SocCapabilities(
        "t41", "ingenic", "npu",
        "NPU plus capable que T31, même contrainte de SDK.",
        has_ive=False, max_sensor_mp=8.0, recommended_backend="classical",
    ),
}

UNKNOWN_SOC = SocCapabilities(
    "unknown", "unknown", "none",
    "SoC non répertorié : OpenVigie se replie sur l'étage classique et le calcul externe.",
    has_ive=False, max_sensor_mp=8.0, recommended_backend="classical", openipc_supported=False,
)


# --------------------------------------------------------------------------- #
# Support capteur : indépendant du support SoC
# --------------------------------------------------------------------------- #
#   "upstream"  = pilote présent dans OpenIPC aujourd'hui
#   "porting"   = capteur documenté par Sony, pilote à porter
SENSOR_DRIVER_STATUS: dict[str, str] = {
    "IMX307": "upstream",
    "IMX327": "upstream",
    "IMX335": "upstream",
    "IMX415": "upstream",
    # STARVIS 2 : non présents en amont à ce jour, un portage par capteur.
    "IMX662": "porting",
    "IMX664": "porting",
    "IMX675": "porting",
    "IMX678": "porting",
    "IMX585": "porting",
}


def sensor_driver_status(sensor: str) -> str:
    """Statut du pilote capteur dans OpenIPC (``upstream`` / ``porting`` / ``unknown``)."""
    return SENSOR_DRIVER_STATUS.get(sensor.upper(), "unknown")


# Résolution des capteurs, en mégapixels. AUDIT P0-04 : ``max_sensor_mp``
# figurait dans la matrice mais n'était comparé à rien ; une carte plafonnée à
# 4 MP pouvait donc être déclarée « prête » avec un capteur 8 MP.
SENSOR_MEGAPIXELS: dict[str, float] = {
    "IMX307": 2.1, "IMX327": 2.1, "IMX335": 5.0, "IMX415": 8.5,
    "IMX662": 2.1, "IMX664": 4.1, "IMX675": 5.0, "IMX678": 8.3, "IMX585": 8.4,
}


def sensor_megapixels(sensor: str) -> float | None:
    return SENSOR_MEGAPIXELS.get(sensor.upper())


def board_readiness(soc: str, sensor: str) -> dict:
    """Verdict d'achat pour une combinaison SoC + capteur.

    Sépare volontairement les deux conditions : une carte peut être parfaitement
    supportée côté SoC et inutilisable faute de pilote capteur, et inversement.
    """
    caps = get_capabilities(soc)
    driver = sensor_driver_status(sensor)
    megapixels = sensor_megapixels(sensor)

    # AUDIT P0-04 : contrôle de résolution avant tout autre verdict. Un pilote
    # présent en amont ne dit rien de la capacité du SoC à traiter le flux :
    # gk7605v100 + IMX415 était annoncé « prêt » alors que la matrice plafonne
    # ce SoC à 5 MP pour un capteur de 8,5 MP.
    if megapixels is not None and megapixels > caps.max_sensor_mp + 0.05:
        return {
            "soc": caps.soc, "sensor": sensor.upper(),
            "soc_supported": caps.openipc_supported, "sensor_driver": driver,
            "sensor_mp": megapixels, "max_sensor_mp": caps.max_sensor_mp,
            "status": "resolution_exceeded",
            "verdict": (
                f"capteur {megapixels:.1f} MP au-delà de la limite {caps.max_sensor_mp:.1f} MP "
                f"du SoC {caps.soc} : combinaison non retenue"
            ),
            "recommended_backend": caps.recommended_backend,
            "can_run_cnn_locally": caps.can_run_cnn_locally,
        }

    if caps.openipc_supported and driver == "upstream":
        status, verdict = "ready", "utilisable immédiatement sous OpenIPC"
    elif caps.openipc_supported and driver == "porting":
        status, verdict = "porting_required", f"SoC supporté, pilote {sensor.upper()} à porter"
    elif not caps.openipc_supported and driver == "upstream":
        status, verdict = "soc_unsupported", "capteur supporté mais SoC hors OpenIPC"
    else:
        status, verdict = "unsupported", "ni le SoC ni le capteur ne sont prêts"
    return {
        "soc": caps.soc,
        "sensor": sensor.upper(),
        "soc_supported": caps.openipc_supported,
        "sensor_driver": driver,
        "sensor_mp": megapixels,
        "max_sensor_mp": caps.max_sensor_mp,
        "status": status,
        "verdict": verdict,
        "recommended_backend": caps.recommended_backend,
        "can_run_cnn_locally": caps.can_run_cnn_locally,
    }


def get_capabilities(soc: str | None) -> SocCapabilities:
    if not soc:
        return UNKNOWN_SOC
    return SOC_MATRIX.get(soc.strip().lower(), UNKNOWN_SOC)


# --------------------------------------------------------------------------- #
# Détection à l'exécution
# --------------------------------------------------------------------------- #
@dataclass
class PlatformInfo:
    """Ce qu'OpenVigie a pu déterminer de la plateforme sur laquelle il tourne."""

    soc: str | None = None
    sensor: str | None = None
    is_openipc: bool = False
    firmware: str | None = None
    source: str = "unknown"
    capabilities: SocCapabilities = field(default_factory=lambda: UNKNOWN_SOC)

    def as_dict(self) -> dict:
        return {
            "soc": self.soc,
            "sensor": self.sensor,
            "is_openipc": self.is_openipc,
            "firmware": self.firmware,
            "source": self.source,
            "capabilities": self.capabilities.as_dict(),
        }


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        return out.stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None


def parse_ipctool(text: str) -> tuple[str | None, str | None]:
    """Extrait (soc, capteur) de la sortie de ``ipctool``.

    ``ipctool`` est l'outil d'inventaire d'OpenIPC ; sa sortie est du YAML.
    Le parsing est volontairement tolérant : le format a bougé entre versions.
    """
    soc = sensor = None
    m = re.search(r"^\s*chip(?:Name|_name)\s*:\s*([A-Za-z0-9_.-]+)", text, re.MULTILINE | re.IGNORECASE)
    if m:
        soc = m.group(1).strip().lower()
    m = re.search(r"^\s*(?:sensor|model)\s*:\s*([A-Za-z0-9_.-]+)", text, re.MULTILINE | re.IGNORECASE)
    if m:
        candidate = m.group(1).strip().upper()
        if candidate.startswith(("IMX", "OS", "SC", "GC")):
            sensor = candidate
    return soc, sensor


def parse_os_release(text: str) -> tuple[bool, str | None]:
    """Détecte OpenIPC et sa version depuis ``/etc/os-release``."""
    is_openipc = "openipc" in text.lower()
    m = re.search(r'^\s*(?:VERSION_ID|VERSION)\s*=\s*"?([^"\n]+)"?', text, re.MULTILINE)
    return is_openipc, (m.group(1).strip() if m else None)


def detect_platform(read_file=None, run=None) -> PlatformInfo:
    """Identifie la plateforme locale.

    ``read_file`` et ``run`` sont injectables pour rendre la détection testable
    sans matériel — un principe appliqué partout dans ce dépôt, parce qu'un code
    qui n'est testable que sur pylône n'est pas testé.
    """
    read_file = read_file or _read_file_default
    run = run or _run

    info = PlatformInfo()

    os_release = read_file("/etc/os-release")
    if os_release:
        info.is_openipc, info.firmware = parse_os_release(os_release)
        if info.is_openipc:
            info.source = "os-release"

    ipctool_out = run(["ipctool"])
    if ipctool_out:
        soc, sensor = parse_ipctool(ipctool_out)
        info.soc = soc or info.soc
        info.sensor = sensor or info.sensor
        info.source = "ipctool"
        info.is_openipc = True

    if not info.soc:
        cpuinfo = read_file("/proc/cpuinfo") or ""
        for known in SOC_MATRIX:
            if known in cpuinfo.lower():
                info.soc = known
                info.source = info.source if info.source != "unknown" else "cpuinfo"
                break

    info.capabilities = get_capabilities(info.soc)
    return info


def _read_file_default(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def select_backend(platform: PlatformInfo, requested: str | None = None) -> tuple[str, str]:
    """Choisit le backend de détection adapté à la plateforme.

    Renvoie ``(backend, justification)``. Une demande explicite est honorée si
    elle est réaliste, refusée sinon — demander ``nnie`` sur un SigmaStar est une
    erreur de configuration, pas une préférence.
    """
    caps = platform.capabilities
    if requested and requested not in ("auto", None):
        if requested == "nnie" and caps.accelerator != "nnie":
            return caps.recommended_backend, (
                f"backend 'nnie' demandé mais le SoC '{caps.soc}' n'a pas de NNIE "
                f"→ repli sur '{caps.recommended_backend}'"
            )
        return requested, "backend imposé par la configuration"
    return caps.recommended_backend, f"backend déduit du SoC '{caps.soc}'"


# --------------------------------------------------------------------------- #
# Adaptateur caméra OpenIPC (majestic)
# --------------------------------------------------------------------------- #
@dataclass
class OpenIpcCamera:
    """Accès à une caméra OpenIPC via les points d'entrée standard de majestic.

    Le même code fonctionne sur n'importe quelle carte flashée en OpenIPC,
    quel que soit le SoC — c'est tout l'intérêt de viser le firmware plutôt que
    la puce.
    """

    host: str
    user: str = "root"
    password: str = ""
    http_port: int = 80

    @property
    def snapshot_url(self) -> str:
        """Snapshot JPEG — **le chemin d'acquisition recommandé**."""
        return f"http://{self.host}:{self.http_port}/image.jpg"

    @property
    def rtsp_url(self) -> str:
        """Flux RTSP — repli seulement : la compression détruit la fumée fine."""
        return f"rtsp://{self.host}/stream0"

    def cli_get(self, key: str, run=None) -> str | None:
        """Lit une clé de configuration majestic (``cli -g .video0.fps``)."""
        run = run or _run
        out = run(["ssh", f"{self.user}@{self.host}", "cli", "-g", key])
        return out.strip() if out else None

    def cli_set(self, key: str, value: str, run=None) -> bool:
        run = run or _run
        return run(["ssh", f"{self.user}@{self.host}", "cli", "-s", key, str(value)]) is not None


# Réglages majestic recommandés pour la détection de fumée.
# Chaque entrée est justifiée : ce sont les réglages par défaut « vidéosurveillance »
# qui détruisent le signal, pas le capteur.
MAJESTIC_DETECTION_PROFILE: dict[str, tuple[str, str]] = {
    ".jpeg.qfactor": ("90", "qualité snapshot élevée : la fumée naissante est un signal de faible amplitude"),
    ".jpeg.fps": ("1", "1 image/s suffit largement ; le facteur limitant est la revisite, pas le débit"),
    ".isp.slowShutter": ("disabled", "l'obturateur lent introduit du flou de mouvement et fausse les features temporelles"),
    ".image.contrast": ("50", "contraste neutre : ne pas écraser les faibles écarts"),
    ".nightMode.enabled": ("true", "commutation ICR gérée par la caméra, mais signalée au pipeline via l'état jour/nuit"),
    ".osd.enabled": ("false", "l'incrustation d'horodatage crée un candidat permanent dans un coin de l'image"),
    ".video0.fps": ("5", "faible débit vidéo : le flux ne sert qu'à la levée de doute humaine"),
}

# Réglages à surveiller explicitement — ils ne se règlent pas à l'aveugle.
MAJESTIC_WARNINGS: dict[str, str] = {
    ".isp.3dnr": (
        "La réduction de bruit temporelle (3DNR) agressive efface une fumée fine en "
        "mouvement lent : c'est exactement le signal recherché. Baisser au minimum "
        "acceptable et vérifier sur des séquences réelles."
    ),
    ".isp.drc": (
        "Le DRC/WDR modifie le mapping tonal image par image : le modèle de fond voit "
        "un changement global à chaque bascule. Verrouiller ou compenser."
    ),
    ".image.mirror": (
        "Un miroir ou une rotation invalide la relation colonne → azimut : "
        "recalibrer le relèvement après tout changement."
    ),
}


def detection_profile_commands(camera: OpenIpcCamera) -> list[str]:
    """Commandes ``cli`` à appliquer pour passer une caméra en profil détection."""
    return [
        f"ssh {camera.user}@{camera.host} cli -s {key} {value}"
        for key, (value, _reason) in MAJESTIC_DETECTION_PROFILE.items()
    ]


def compatibility_report(soc: str, sensor: str) -> dict:
    """Rapport complet pour une référence de carte envisagée."""
    caps = get_capabilities(soc)
    ready = board_readiness(soc, sensor)
    return {
        **ready,
        "capabilities": caps.as_dict(),
        "notes": [caps.accel_note] + ([
            f"Le pilote {sensor.upper()} n'est pas en amont : prévoir un portage "
            "(un seul portage couvre ensuite toutes les cartes du même capteur)."
        ] if ready["sensor_driver"] == "porting" else []),
    }


def supported_socs() -> list[str]:
    return sorted(SOC_MATRIX)


def supported_sensors() -> list[str]:
    return sorted(SENSOR_DRIVER_STATUS)
