"""Tests d'équipement et de configuration.

Ce module répond à la question de la phase 1 : *est-ce que ce site est
exploitable, et avec quels réglages ?* Chaque test renvoie un verdict
`ok / warn / fail` et une mesure chiffrée, jamais un simple booléen : c'est la
mesure qui sert ensuite à régler les seuils.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

import numpy as np

from .compat import sobel_energy, to_gray
from .geometry import (
    LensSpec,
    SensorSpec,
    coverage_gaps,
    hfov_deg,
    min_detectable_width_m,
    scan_budget,
)
from .registration import preset_repeatability, vibration_index


@dataclass
class CheckResult:
    name: str
    status: str            # ok | warn | fail | skip
    value: float | None = None
    unit: str = ""
    message: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in ("ok", "skip")

    def __str__(self) -> str:
        icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}[self.status]
        val = f" [{self.value:g} {self.unit}]" if self.value is not None else ""
        return f"[{icon}] {self.name}{val} — {self.message}"


# --------------------------------------------------------------------------- #
# Réseau et flux
# --------------------------------------------------------------------------- #
def check_host_reachable(host: str, port: int = 80, timeout_s: float = 2.0) -> CheckResult:
    """Joignabilité TCP d'une caméra."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            ms = (time.perf_counter() - t0) * 1000
        status = "ok" if ms < 200 else "warn"
        return CheckResult("reseau", status, round(ms, 1), "ms", f"{host}:{port} joignable")
    except OSError as exc:
        return CheckResult("reseau", "fail", None, "", f"{host}:{port} injoignable ({exc})")


def check_frame_sanity(frame: np.ndarray, expected_shape: tuple[int, int] | None = None) -> CheckResult:
    """Vérifie qu'une image n'est ni saturée, ni noire, ni figée."""
    gray = to_gray(frame)
    mean, std = float(gray.mean()), float(gray.std())
    if std < 1.0:
        return CheckResult("image", "fail", std, "ecart-type", "image uniforme (capot, panne capteur, nuit totale)")
    if mean > 240:
        return CheckResult("image", "warn", mean, "niveau", "image surexposée : revoir l'exposition/WDR")
    if mean < 8:
        return CheckResult("image", "warn", mean, "niveau", "image très sombre")
    if expected_shape and gray.shape != expected_shape:
        return CheckResult("image", "warn", None, "", f"résolution {gray.shape} != attendue {expected_shape}")
    return CheckResult("image", "ok", std, "ecart-type", "image exploitable")


def check_compression_artifacts(frame: np.ndarray, block: int = 8) -> CheckResult:
    """Détecte un blocking JPEG/H.26x marqué.

    Test critique et souvent oublié : la fumée fine est un signal de très faible
    amplitude, et une compression agressive la supprime avant tout algorithme.
    On compare l'énergie de gradient sur les frontières de blocs de 8 px à
    l'énergie moyenne ; un ratio élevé signale une compression destructrice.
    """
    gray = to_gray(frame)
    if min(gray.shape) < 4 * block:
        return CheckResult("compression", "skip", None, "", "image trop petite pour le test")
    energy = sobel_energy(gray)
    cols = np.arange(block, gray.shape[1] - 1, block)
    rows = np.arange(block, gray.shape[0] - 1, block)
    if cols.size == 0 or rows.size == 0:
        return CheckResult("compression", "skip", None, "", "image trop petite")
    border = (energy[:, cols].mean() + energy[rows, :].mean()) / 2.0
    overall = float(energy.mean()) or 1e-6
    ratio = float(border / overall)
    if ratio > 1.35:
        return CheckResult(
            "compression", "fail", round(ratio, 3), "ratio",
            "blocking marqué : passer aux snapshots JPEG q>=90, ne pas analyser le flux H.265",
        )
    if ratio > 1.15:
        return CheckResult("compression", "warn", round(ratio, 3), "ratio", "compression perceptible, augmenter le débit/qualité")
    return CheckResult("compression", "ok", round(ratio, 3), "ratio", "compression acceptable")


# --------------------------------------------------------------------------- #
# Mécanique et optique
# --------------------------------------------------------------------------- #
def check_preset_repeatability(
    frames: list[np.ndarray], max_p95_px: float = 8.0
) -> CheckResult:
    """Retour répété sur un même preset : mesure la dérive résiduelle.

    Au-delà de quelques pixels après recalage, un modèle de fond par preset
    devient inutilisable et il faut soit une tête à encodeurs absolus, soit
    passer aux caméras fixes.
    """
    if len(frames) < 3:
        return CheckResult("ptz_repetabilite", "skip", None, "", "au moins 3 retours nécessaires")
    stats = preset_repeatability(frames)
    p95 = stats["p95_px"]
    status = "ok" if p95 <= max_p95_px else ("warn" if p95 <= 3 * max_p95_px else "fail")
    msg = {
        "ok": "dérive compensable par recalage",
        "warn": "dérive élevée : vérifier le serrage, la charge et la vitesse d'approche",
        "fail": "dérive rédhibitoire : tête inadaptée à un modèle de fond par preset",
    }[status]
    return CheckResult("ptz_repetabilite", status, round(p95, 2), "px", msg, stats)


def check_vibration(frames: list[np.ndarray], max_std_px: float = 3.0) -> CheckResult:
    """Vibration du mât mesurée sur une rafale d'images consécutives."""
    if len(frames) < 4:
        return CheckResult("vibration", "skip", None, "", "au moins 4 images nécessaires")
    idx = vibration_index(frames)
    status = "ok" if idx <= max_std_px else ("warn" if idx <= 2 * max_std_px else "fail")
    msg = {
        "ok": "mât stable",
        "warn": "vibration notable : descendre le point de fixation, ou activer la porte anémométrique",
        "fail": "vibration incompatible avec la différence au fond",
    }[status]
    return CheckResult("vibration", status, round(idx, 2), "px", msg)


def check_window_cleanliness(frame: np.ndarray, baseline_energy: float | None = None) -> CheckResult:
    """Encrassement du hublot : toiles d'araignée, gouttes, buée, givre.

    Cause n°1 d'alertes fantômes en exploitation réelle sur une tour, et cause
    n°1 de dégradation silencieuse de la portée. La perte d'énergie de gradient
    par rapport à une image de référence propre est un proxy simple et efficace.
    """
    energy = float(sobel_energy(to_gray(frame)).mean())
    if baseline_energy is None:
        return CheckResult("hublot", "skip", round(energy, 2), "gradient", "pas de référence propre enregistrée")
    ratio = energy / max(baseline_energy, 1e-6)
    if ratio < 0.55:
        return CheckResult("hublot", "fail", round(ratio, 3), "ratio", "hublot très dégradé : nettoyage requis")
    if ratio < 0.8:
        return CheckResult("hublot", "warn", round(ratio, 3), "ratio", "netteté en baisse : encrassement ou dérive de mise au point")
    return CheckResult("hublot", "ok", round(ratio, 3), "ratio", "optique propre")


def check_focus_stability(frames: list[np.ndarray], max_drift: float = 0.15) -> CheckResult:
    """Dérive de mise au point sur un objectif motorisé (fréquente en cycle thermique)."""
    if len(frames) < 2:
        return CheckResult("focus", "skip", None, "", "au moins 2 images nécessaires")
    energies = [float(sobel_energy(to_gray(f)).mean()) for f in frames]
    ref = energies[0] or 1e-6
    drift = max(abs(e - ref) / ref for e in energies[1:])
    status = "ok" if drift <= max_drift else ("warn" if drift <= 2 * max_drift else "fail")
    return CheckResult("focus", status, round(drift, 3), "ratio", "stabilité de mise au point")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def check_coverage(views, max_gap_deg: float = 1.0) -> CheckResult:
    """Vérifie qu'il n'y a pas de secteur aveugle."""
    gaps = [g for g in coverage_gaps(views) if abs(g[1] - g[0]) > max_gap_deg]
    if not gaps:
        return CheckResult("couverture", "ok", 0.0, "deg", f"{len(views)} vues, 360° couverts")
    total = sum(abs(b - a) for a, b in gaps)
    return CheckResult(
        "couverture", "fail", round(total, 1), "deg",
        f"{len(gaps)} secteur(s) aveugle(s)", {"gaps": [(round(a, 1), round(b, 1)) for a, b in gaps]},
    )


def check_scan_budget(
    n_views: int, dwell_s: float, settle_s: float, is_ptz: bool,
    max_cycle_min: float = 3.0, max_moves_per_year: float = 500_000.0,
) -> CheckResult:
    """Vérifie que le temps de revisite et l'usure mécanique restent tenables."""
    b = scan_budget(n_views, dwell_s, settle_s, is_ptz)
    problems = []
    if b.cycle_s / 60 > max_cycle_min:
        problems.append(f"cycle {b.cycle_s / 60:.1f} min > {max_cycle_min} min")
    if b.moves_per_year > max_moves_per_year:
        problems.append(f"{b.moves_per_year:,.0f} mouvements/an")
    status = "ok" if not problems else ("warn" if len(problems) == 1 else "fail")
    return CheckResult(
        "budget_balayage", status, round(b.cycle_s / 60, 2), "min",
        "; ".join(problems) or "revisite et usure compatibles d'une exploitation continue",
        b.as_dict(),
    )


def check_range_budget(
    sensor: SensorSpec, focal_mm: float, target_range_m: float, target_plume_m: float = 30.0
) -> CheckResult:
    """Vérifie que la focale permet réellement d'accrocher un panache à la portée visée."""
    min_plume = min_detectable_width_m(sensor, focal_mm, target_range_m)
    ratio = min_plume / max(target_plume_m, 1e-6)
    status = "ok" if ratio <= 1.0 else ("warn" if ratio <= 1.6 else "fail")
    msg = (
        f"à {target_range_m / 1000:.1f} km, panache minimum détectable {min_plume:.0f} m "
        f"(objectif {target_plume_m:.0f} m, champ {hfov_deg(sensor, focal_mm):.1f}°)"
    )
    return CheckResult("budget_portee", status, round(min_plume, 1), "m", msg)


def check_lens_compat(lens: LensSpec, focal_mm: float) -> CheckResult:
    if lens.f_min_mm - 1e-6 <= focal_mm <= lens.f_max_mm + 1e-6:
        return CheckResult("objectif", "ok", focal_mm, "mm", "focale dans la plage de l'objectif")
    return CheckResult(
        "objectif", "fail", focal_mm, "mm",
        f"focale hors plage {lens.f_min_mm}-{lens.f_max_mm} mm",
    )


def check_platform_readiness(cfg) -> CheckResult:
    """Vérifie la combinaison SoC + capteur déclarée.

    AUDIT P0-05 (corrigé 0.4.0) : ``openvigie doctor`` ne regardait ni le matériel,
    ni le pilote, ni le backend, ni le modèle. Un tier MEDIUM avec un IMX675 dont
    le pilote OpenIPC n'existe pas obtenait « 4 ok, 0 avertissement ».
    """
    ready = cfg.readiness()
    status_map = {
        "ready": "ok", "porting_required": "warn", "unknown": "warn",
        "soc_unsupported": "fail", "unsupported": "fail", "resolution_exceeded": "fail",
    }
    return CheckResult(
        "plateforme", status_map.get(ready["status"], "warn"), None, "",
        ready["verdict"], ready,
    )


def check_detector_backend(cfg) -> CheckResult:
    """Le backend demandé est-il réellement disponible ?

    AUDIT P0-22 : un tier MEDIUM demandant NNIE, ou FULL demandant ONNX sans
    chemin de modèle, se repliait silencieusement sur l'étage classique tout en
    passant les contrôles. L'exploitant croyait disposer d'une capacité absente.
    """
    from .detectors import get_detector

    requested = cfg.pipeline.detector_backend
    try:
        detector = get_detector(requested, **cfg.pipeline.detector_kwargs)
    except (RuntimeError, OSError, ImportError, TypeError, ValueError) as exc:
        return CheckResult(
            "backend", "fail", None, "",
            f"backend '{requested}' indisponible ({exc}) — repli sur 'classical', "
            f"donc pas de classification apprise",
        )
    return CheckResult("backend", "ok", None, "", f"backend '{detector.name}' disponible")


def check_operating_mode(cfg) -> CheckResult:
    """Le site est-il autorisé à transmettre, et est-ce cohérent ?"""
    mode = cfg.operating.mode
    if mode == "measure":
        return CheckResult("mode", "ok", None, "", "mode 'measure' : aucune alerte émise")
    if mode == "shadow":
        return CheckResult("mode", "ok", None, "", "mode 'shadow' : alertes journalisées, non transmises")
    fitted = bool(cfg.fusion.get("fitted", False))
    if fitted:
        return CheckResult("mode", "ok", None, "", "mode 'alert' avec modèle de fusion calibré")
    if cfg.operating.allow_uncalibrated_alerts:
        return CheckResult(
            "mode", "warn", None, "",
            "mode 'alert' avec poids de fusion PROVISOIRES et dérogation active — "
            "acceptable en démonstration, pas en exploitation",
        )
    return CheckResult(
        "mode", "fail", None, "",
        "mode 'alert' demandé mais modèle de fusion non calibré : la transmission sera refusée",
    )


def check_masks(cfg) -> CheckResult:
    """Cohérence des masques de confidentialité (AUDIT P0-21)."""
    from .masking import validate_masks

    if not cfg.masks:
        return CheckResult(
            "confidentialite", "warn", None, "",
            "aucun masque déclaré : vérifier qu'aucune zone privée n'est dans le champ",
        )
    problems = validate_masks(cfg.masks)
    if problems:
        return CheckResult("confidentialite", "fail", None, "", "; ".join(problems[:3]))
    n = sum(len(v) for v in cfg.masks.values())
    return CheckResult("confidentialite", "ok", float(n), "zones", f"{n} zone(s) masquée(s)")


def capabilities(cfg) -> dict[str, tuple[bool, str]]:
    """Ce que le site peut RÉELLEMENT faire, par opposition à ce qu'il déclare.

    AUDIT P0-03/P1-11 : les drapeaux ``use_segmentation``, ``use_temporal_model``
    et ``use_ptz_confirmation`` étaient présents dans la configuration mais
    n'étaient consommés par aucun code. Cette fonction dit la vérité.
    """
    from .detectors import get_detector

    try:
        backend = get_detector(cfg.pipeline.detector_backend, **cfg.pipeline.detector_kwargs).name
        backend_ok = backend == cfg.pipeline.detector_backend
    except Exception:
        backend, backend_ok = "classical", False

    return {
        "acquisition JPEG": (True, "sources fichier, snapshot HTTP, RTSP"),
        "recalage": (True, "corrélation de phase, translation"),
        "modèle de fond": (True, "médiane glissante par vue × heure × saison"),
        "candidats classiques": (True, "seuillage MAD + morphologie"),
        "géoréférencement MNT": (True, "ray-casting, si un MNT est fourni"),
        "classification apprise": (backend_ok, f"backend effectif : {backend}"),
        "modèle temporel": (False, "non raccordé au pipeline (roadmap v0.6)"),
        "segmentation du candidat": (False, "non raccordée au pipeline (roadmap v0.6)"),
        "confirmation PTZ exécutée": (False, "tâches calculées, exécution absente (roadmap v0.6)"),
        "capture automatique des preuves": (False, "champs présents, capture absente (roadmap v0.4)"),
        "agent continu": (False, "openvigie run non implémenté (roadmap v0.4)"),
        "masques de confidentialité": (bool(cfg.masks), "appliqués à l'acquisition si déclarés"),
        "file hors ligne durable": (True, "outbox persistante avec dead letters"),
        "transport mTLS": (False, "jeton porteur seulement (roadmap v0.6)"),
        "transmission autorisée": (
            cfg.operating.mode == "alert"
            and (bool(cfg.fusion.get("fitted", False)) or cfg.operating.allow_uncalibrated_alerts),
            f"mode '{cfg.operating.mode}'",
        ),
    }


def run_config_checks(cfg) -> list[CheckResult]:
    """Batterie de vérifications purement statiques, exécutable sans matériel."""
    from .geometry import plan_uniform_ring

    sensor = cfg.optics.sensor_spec()
    lens = cfg.optics.lens_spec()
    views = plan_uniform_ring(
        sensor, lens, cfg.scan.n_views, cfg.scan.target_range_m, cfg.scan.overlap
    )
    return [
        check_lens_compat(lens, cfg.optics.focal_mm),
        check_coverage(views),
        check_scan_budget(
            cfg.scan.n_views, cfg.scan.dwell_s, cfg.scan.settle_s, cfg.scan.mode == "ptz"
        ),
        check_range_budget(sensor, views[0].focal_mm, cfg.scan.target_range_m),
        # AUDIT P0-05 : les contrôles de déployabilité font désormais partie du
        # diagnostic standard, pas d'une option.
        check_platform_readiness(cfg),
        check_detector_backend(cfg),
        check_operating_mode(cfg),
        check_masks(cfg),
    ]


def summarize(results: list[CheckResult]) -> dict:
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] += 1
    return {**counts, "all_passed": counts["fail"] == 0}
