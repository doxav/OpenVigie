"""Configuration du site et des trois niveaux de déploiement.

Les trois tiers ne sont pas trois qualités de logiciel : c'est le même pipeline,
avec des étages activés ou non selon le calcul disponible. C'est volontaire —
cela garantit qu'un site MINIMAL peut être promu en FULL sans réécriture, et
que les mesures faites en phase 1 restent comparables.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .candidates import CandidateConfig
from .geometry import SENSORS, LensSpec, SensorSpec
from .platform import board_readiness, get_capabilities, sensor_driver_status
from .scoring import DecisionConfig, FusionModel

TIERS = ("minimal", "medium", "full")


@dataclass
class OpticsConfig:
    sensor: str = "IMX675"
    focal_mm: float = 6.25
    f_min_mm: float = 2.7
    f_max_mm: float = 13.5
    camera_height_m: float = 40.0
    tilt_deg: float = 1.5
    max_distance_m: float = 20_000.0

    def sensor_spec(self) -> SensorSpec:
        if self.sensor not in SENSORS:
            raise ValueError(f"capteur inconnu '{self.sensor}' (connus: {sorted(SENSORS)})")
        return SENSORS[self.sensor]

    def lens_spec(self) -> LensSpec:
        return LensSpec(self.f_min_mm, self.f_max_mm)


@dataclass
class SectorConfig:
    """Un secteur angulaire réellement utile (issue #1).

    Déclarer des secteurs dispense de la couverture 360° implicite : un site
    dont le nord est bouché par une crête n'a aucune raison d'y dépenser des
    caméras.
    """

    name: str = "S00"
    start_deg: float = 0.0
    end_deg: float = 360.0
    max_range_m: float = 8_000.0
    priority: float = 1.0
    target_plume_m: float = 30.0

    def to_sector(self):
        from .modules import Sector

        return Sector(
            name=self.name, start_deg=self.start_deg, end_deg=self.end_deg,
            max_range_m=self.max_range_m, priority=self.priority,
            target_plume_m=self.target_plume_m,
        )


@dataclass
class ScanConfig:
    mode: str = "fixed"          # fixed | ptz
    n_views: int = 8
    dwell_s: float = 12.0
    settle_s: float = 3.0
    overlap: float = 0.15
    target_range_m: float = 8_000.0
    priority_views: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in ("fixed", "ptz"):
            raise ValueError("scan.mode doit être 'fixed' ou 'ptz'")


@dataclass
class PipelineConfig:
    detector_backend: str = "classical"
    detector_kwargs: dict[str, Any] = field(default_factory=dict)
    use_segmentation: bool = False
    use_temporal_model: bool = False
    use_ptz_confirmation: bool = False
    use_triangulation: bool = False
    max_rois_per_frame: int = 12
    min_visits_before_alert: int = 3
    vibration_gate_px: float = 6.0


# AUDIT P0-06 (corrigé 0.4.0). Le dépôt recommandait une phase de mesure de
# plusieurs mois avant toute détection, mais rien dans le code ne l'imposait :
# une configuration par défaut, avec des poids de fusion explicitement marqués
# « provisoires », pouvait émettre une alerte opérationnelle. La règle est
# désormais un verrou logiciel, pas une phrase de documentation.
OPERATING_MODES = ("measure", "shadow", "alert")


@dataclass
class OperatingConfig:
    """Mode d'exploitation du site.

    ``measure``  détection exécutée, aucun événement émis. C'est le mode de la
                 phase 1 : on constitue les fonds et les négatifs du site.
    ``shadow``   événements produits et journalisés, mais jamais transmis. Sert à
                 mesurer le taux de fausses alertes avant d'exposer un opérateur.
    ``alert``    transmission autorisée. Refusé tant que le modèle de fusion n'a
                 pas été calibré sur les négatifs du site, sauf dérogation
                 explicite et tracée.

    ``allow_uncalibrated_alerts`` existe pour les démonstrations et les essais ;
    l'activer est une décision consciente, et elle apparaît dans le résumé et
    dans chaque événement.
    """

    mode: str = "measure"
    allow_uncalibrated_alerts: bool = False

    def __post_init__(self) -> None:
        if self.mode not in OPERATING_MODES:
            raise ValueError(f"operating.mode doit être l'un de {OPERATING_MODES}")


@dataclass
class CalibrationConfig:
    """Étalonnage géométrique par trafic aérien.

    Désactivé par défaut : c'est une option, et elle suppose une horloge
    correctement synchronisée et du trafic aérien visible depuis le site.
    ``bearing_sigma_deg`` est la valeur utilisée tant qu'aucun étalonnage n'a
    été produit — 0,5° suppose déjà un relevé soigné ; une boussole sur pylône
    treillis donne plutôt 2°.
    """

    enabled: bool = False
    adsb_source: str = "none"            # none | dump1090 | opensky | static
    adsb_url: str = "http://127.0.0.1:8080/data/aircraft.json"
    calibration_path: str = "data/calibration"
    bearing_sigma_deg: float = 0.5       # valeur de repli, sans étalonnage
    min_observations: int = 25
    drift_yaw_tolerance_deg: float = 0.15
    drift_pitch_tolerance_deg: float = 0.15
    fit_clock_offset: bool = True
    fit_altitude_offset: bool = False

    def __post_init__(self) -> None:
        if self.adsb_source not in ("none", "dump1090", "opensky", "static"):
            raise ValueError("calibration.adsb_source doit être none, dump1090, opensky ou static")
        if self.bearing_sigma_deg <= 0:
            raise ValueError("calibration.bearing_sigma_deg doit être > 0")


@dataclass
class NetworkConfig:
    """Connectivité du site.

    Le mode par défaut est ``file`` : un site sans plateforme centrale reste
    parfaitement exploitable, ses événements sont journalisés localement. La
    file d'attente est active dans tous les cas — c'est elle qui garantit
    qu'une coupure réseau n'efface pas une alerte.
    """

    transport: str = "file"            # file | http | memory | none
    url: str = ""
    health_url: str = ""
    token_env: str = "OPENVIGIE_TOKEN"     # jamais de secret dans le fichier de config
    events_path: str = "data/events.jsonl"
    outbox_dir: str = "data/outbox"
    heartbeat_interval_s: float = 300.0
    max_queue_entries: int = 5000
    max_attempts: int = 12
    verify_tls: bool = True

    def __post_init__(self) -> None:
        if self.transport not in ("file", "http", "memory", "none"):
            raise ValueError("network.transport doit être file, http, memory ou none")
        if self.transport == "http" and not self.url:
            raise ValueError("network.transport='http' exige une url")


@dataclass
class PlatformConfig:
    """Plateforme matérielle visée, au sens OpenIPC.

    ``soc`` vaut ``auto`` pour laisser OpenVigie détecter la carte au démarrage.
    Renseigner explicitement permet en revanche de valider un dimensionnement
    *avant* d'acheter, ce qui est l'usage principal.
    """

    firmware: str = "openipc"
    soc: str = "auto"
    compute: str = "onboard"   # onboard | external
    external_device: str = ""  # ex. "jetson-orin-nano-super", "rk3588"

    def __post_init__(self) -> None:
        if self.compute not in ("onboard", "external"):
            raise ValueError("platform.compute doit être 'onboard' ou 'external'")

    def capabilities(self):
        return get_capabilities(None if self.soc == "auto" else self.soc)


@dataclass
class SiteConfig:
    """Configuration complète d'un site."""

    site_id: str = "site-demo"
    tier: str = "minimal"
    latitude: float = 44.0
    longitude: float = 3.0
    sunrise_h: float = 7.0
    sunset_h: float = 21.0
    # AUDIT P0-13 (corrigé 0.4.0) : l'altitude du terrain était figée à 0 m dans
    # le chemin de configuration de l'étalonnage. Sur une tour de montagne à
    # 900 m, l'élévation calculée des aéronefs était fausse de plusieurs
    # dixièmes de degré — c'est-à-dire exactement la grandeur qu'on prétend
    # mesurer au millième près.
    site_altitude_m: float = 0.0
    operating: OperatingConfig = field(default_factory=OperatingConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    optics: OpticsConfig = field(default_factory=OpticsConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    fusion: dict[str, Any] = field(default_factory=lambda: FusionModel().as_dict())
    dem_path: str = ""
    survey_path: str = ""
    # Secteurs utiles. Vide = couverture 360° (comportement historique).
    sectors: list[dict] = field(default_factory=list)
    # AUDIT P0-21 (corrigé 0.4.0) : ces masques étaient déclarés dans la
    # configuration et présentés comme une protection dans la documentation,
    # mais aucun code ne les lisait. Ils sont désormais appliqués à l'image dès
    # l'acquisition, donc avant analyse, stockage et transmission.
    # Format : {view_id: [[x0, y0, x1, y1], ...]} en pixels de l'image analysée.
    masks: dict[str, list[list[int]]] = field(default_factory=dict)
    wind_bearing_deg: float | None = None
    peers: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier inconnu '{self.tier}' (attendus: {TIERS})")
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ValueError("coordonnées du site invalides")

    def sector_list(self):
        """Secteurs déclarés, ou un unique secteur 360° par défaut."""
        from .modules import Sector

        if not self.sectors:
            return [Sector(name="360", start_deg=0.0, end_deg=360.0,
                           max_range_m=self.scan.target_range_m)]
        return [SectorConfig(**s).to_sector() for s in self.sectors]

    def site(self):
        """Emplacement de la caméra, au sens du module d'étalonnage."""
        from .calibration import Site

        return Site(
            latitude=self.latitude,
            longitude=self.longitude,
            altitude_m=self.site_altitude_m,
            height_m=self.optics.camera_height_m,
        )

    def readiness_notes(self) -> list[str]:
        """Avertissements de configuration qui ne bloquent pas mais faussent."""
        notes: list[str] = []
        if self.site_altitude_m == 0.0:
            notes.append(
                "site_altitude_m vaut 0 m : renseigner l'altitude du terrain "
                "(l'étalonnage par trafic aérien en dépend directement)"
            )
        if not self.masks:
            notes.append("aucun masque de confidentialité déclaré")
        if not self.fusion.get("fitted", False):
            notes.append("modèle de fusion non calibré sur les négatifs du site")
        return notes

    def readiness(self) -> dict:
        """Verdict OpenIPC pour la combinaison SoC + capteur configurée."""
        if self.platform.soc == "auto":
            return {
                "status": "unknown",
                "verdict": "SoC en détection automatique : exécuter `openvigie hw` sur la cible",
                "sensor_driver": sensor_driver_status(self.optics.sensor),
            }
        return board_readiness(self.platform.soc, self.optics.sensor)

    def fusion_model(self) -> FusionModel:
        return FusionModel.from_dict(self.fusion)

    def as_dict(self) -> dict:
        return asdict(self)


def _build(cls, data: dict | None):
    if not data:
        return cls()
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"clés inconnues pour {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load_site_config(path: str | Path) -> SiteConfig:
    """Charge une configuration YAML avec validation stricte des clés.

    La validation stricte est volontaire : une faute de frappe dans un nom de
    paramètre de seuil doit faire échouer le démarrage, pas dégrader
    silencieusement la détection pendant une saison entière.
    """
    if yaml is None:  # pragma: no cover
        raise RuntimeError("PyYAML requis: `pip install pyyaml`")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return site_config_from_dict(data)


def site_config_from_dict(data: dict) -> SiteConfig:
    nested = {
        "operating": OperatingConfig,
        "platform": PlatformConfig,
        "network": NetworkConfig,
        "calibration": CalibrationConfig,
        "optics": OpticsConfig,
        "scan": ScanConfig,
        "pipeline": PipelineConfig,
        "candidates": CandidateConfig,
        "decision": DecisionConfig,
    }
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in nested:
            kwargs[key] = _build(nested[key], value)
        elif key in SiteConfig.__dataclass_fields__:
            kwargs[key] = value
        else:
            raise ValueError(f"clé de configuration inconnue: '{key}'")
    return SiteConfig(**kwargs)


def save_site_config(cfg: SiteConfig, path: str | Path) -> None:  # pragma: no cover - I/O
    if yaml is None:
        raise RuntimeError("PyYAML requis")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg.as_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Préréglages par tier
# --------------------------------------------------------------------------- #
def tier_defaults(tier: str) -> SiteConfig:
    """Configuration de référence pour chaque niveau matériel."""
    if tier not in TIERS:
        raise ValueError(f"tier inconnu '{tier}'")

    if tier == "minimal":
        # 1 module fixe + 1 bloc 30x sur tête de laboratoire.
        # Objectif : mesurer, pas détecter. Aucune alerte automatique.
        return SiteConfig(
            tier="minimal",
            # AUDIT P1 : le tier MINIMAL est présenté comme une campagne de
            # mesure ; il ne doit donc pas pouvoir produire d'alerte du tout.
            operating=OperatingConfig(mode="measure"),
            platform=PlatformConfig(soc="hi3516av300", compute="onboard"),
            network=NetworkConfig(transport="file", heartbeat_interval_s=600.0),
            optics=OpticsConfig(sensor="IMX675", focal_mm=6.25),
            scan=ScanConfig(mode="ptz", n_views=5, dwell_s=15.0, settle_s=4.0, target_range_m=3_500.0),
            pipeline=PipelineConfig(
                detector_backend="classical",
                use_segmentation=False,
                use_temporal_model=False,
                use_ptz_confirmation=False,
                min_visits_before_alert=4,
            ),
            decision=DecisionConfig(enter_threshold=0.85, min_persistence_visits=4),
        )

    if tier == "medium":
        # 6-8 modules fixes couvrant 360°, calcul dans les caméras (IVE/NNIE),
        # 1 bloc 30x pour la confirmation. Aucun calculateur externe.
        return SiteConfig(
            tier="medium",
            # `shadow` par défaut : les événements sont produits et journalisés
            # localement, mais leur transmission reste une décision explicite.
            operating=OperatingConfig(mode="shadow"),
            platform=PlatformConfig(soc="hi3516av300", compute="onboard"),
            network=NetworkConfig(transport="file", heartbeat_interval_s=300.0),
            optics=OpticsConfig(sensor="IMX675", focal_mm=6.25),
            scan=ScanConfig(mode="fixed", n_views=8, dwell_s=10.0, settle_s=0.0, target_range_m=6_500.0),
            pipeline=PipelineConfig(
                detector_backend="nnie",
                use_segmentation=False,
                use_temporal_model=False,
                use_ptz_confirmation=True,
                max_rois_per_frame=6,
                min_visits_before_alert=3,
            ),
            decision=DecisionConfig(enter_threshold=0.78, min_persistence_visits=3),
        )

    # full : réseau fixe + PTZ + calculateur externe
    return SiteConfig(
        tier="full",
        operating=OperatingConfig(mode="shadow"),
        platform=PlatformConfig(
            soc="hi3516av300", compute="external", external_device="jetson-orin-nano-super"
        ),
        network=NetworkConfig(transport="file", heartbeat_interval_s=120.0),
        calibration=CalibrationConfig(enabled=True, adsb_source="dump1090"),
        optics=OpticsConfig(sensor="IMX675", focal_mm=6.25),
        scan=ScanConfig(mode="fixed", n_views=14, dwell_s=6.0, settle_s=0.0, target_range_m=11_500.0),
        pipeline=PipelineConfig(
            detector_backend="onnx",
            detector_kwargs={},
            use_segmentation=True,
            use_temporal_model=True,
            use_ptz_confirmation=True,
            use_triangulation=True,
            max_rois_per_frame=24,
            min_visits_before_alert=3,
        ),
        decision=DecisionConfig(enter_threshold=0.72, min_persistence_visits=3),
    )
