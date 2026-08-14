"""OpenVigie — détection précoce de feux de forêt depuis des points hauts.

Projet open source (Apache-2.0). Le déployeur reste responsable de la conformité
réglementaire de son installation (RGPD/vidéoprotection, AI Act, autorisations
locales) : voir docs/RESPONSABILITE.md.
"""

__version__ = "0.6.0"
__license__ = "Apache-2.0"

from .config import SiteConfig, load_site_config, tier_defaults  # noqa: E402
from .geometry import scan_budget  # noqa: E402

__all__ = ["SiteConfig", "load_site_config", "tier_defaults", "scan_budget", "__version__"]
