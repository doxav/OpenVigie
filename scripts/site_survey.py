#!/usr/bin/env python3
"""Campagne de mesure d'un site (phase 1).

Ce script ne détecte rien : il **mesure** ce que le site permet réellement.
C'est le livrable de la phase 1 et il conditionne tous les réglages ensuite.

Il produit un rapport JSON avec :
  - joignabilité et latence réseau de chaque caméra ;
  - qualité d'image et niveau de compression (le flux détruit-il la fumée fine ?) ;
  - répétabilité de preset PTZ, mesurée par recalage sur retours répétés ;
  - vibration du mât sur rafale ;
  - propreté de l'optique par rapport à une référence propre ;
  - budget de portée et de balayage effectifs.

Sans caméra, ``--simulate`` exécute la même batterie sur une scène synthétique :
utile pour valider la chaîne d'analyse avant de monter sur pylône.

Usage :
  python scripts/site_survey.py --config config/tiers/medium.yaml --simulate
  python scripts/site_survey.py --config site.yaml --host 192.168.1.64 \
      --snapshot-url http://192.168.1.64/cgi-bin/snapshot.cgi --ptz-returns 8
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openvigie.compat import sobel_energy, to_gray  # noqa: E402
from openvigie.config import load_site_config, tier_defaults  # noqa: E402
from openvigie.hwcheck import (  # noqa: E402
    check_compression_artifacts,
    check_focus_stability,
    check_frame_sanity,
    check_host_reachable,
    check_preset_repeatability,
    check_vibration,
    check_window_cleanliness,
    run_config_checks,
    summarize,
)
from openvigie.registration import shift_image  # noqa: E402
from openvigie.sources import SnapshotHttpSource, SyntheticScene  # noqa: E402


def grab(source, n: int, delay_s: float = 1.0) -> list[np.ndarray]:
    frames = []
    for _ in range(n):
        item = source.read()
        if item is None:
            break
        frames.append(item[0])
        time.sleep(delay_s)
    return frames


def simulated_frames(n: int, jitter_px: float = 0.8) -> list[np.ndarray]:
    scene = SyntheticScene(height=240, width=320, horizon_row=120)
    rng = np.random.default_rng(7)
    out = []
    for _ in range(n):
        f = scene.frame().astype(np.float32)
        out.append(shift_image(f, rng.normal(0, jitter_px), rng.normal(0, jitter_px)).astype(np.uint8))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Campagne de mesure d'un site OpenVigie")
    ap.add_argument("--config", help="configuration YAML du site")
    ap.add_argument("--tier", default="minimal", help="préréglage si pas de config")
    ap.add_argument("--host", help="adresse IP de la caméra")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--snapshot-url", help="URL de snapshot JPEG (recommandé, pas de RTSP)")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--ptz-returns", type=int, default=6, help="nombre de retours sur le même preset")
    ap.add_argument("--burst", type=int, default=8, help="images consécutives pour la vibration")
    ap.add_argument("--baseline", help="image de référence optique propre (npy)")
    ap.add_argument("--simulate", action="store_true", help="exécuter sans matériel")
    ap.add_argument("--output", default="site_survey.json")
    args = ap.parse_args()

    cfg = load_site_config(args.config) if args.config else tier_defaults(args.tier)
    results = []

    # 1. Configuration (toujours exécutable)
    results += run_config_checks(cfg)

    # 2. Acquisition
    if args.simulate:
        burst = simulated_frames(args.burst, jitter_px=0.6)
        returns = simulated_frames(args.ptz_returns, jitter_px=2.5)
    else:
        if not args.snapshot_url:
            print("--snapshot-url requis hors mode --simulate", file=sys.stderr)
            return 2
        results.append(check_host_reachable(args.host or args.snapshot_url.split("/")[2], args.port))
        source = SnapshotHttpSource(args.snapshot_url, args.user, args.password)
        print(f"-- rafale de {args.burst} images…")
        burst = grab(source, args.burst, delay_s=1.0)
        print(
            f"-- {args.ptz_returns} retours sur preset 1 "
            "(déplacer manuellement le PTZ entre chaque retour, ou brancher le backend Pelco-D)…"
        )
        returns = grab(source, args.ptz_returns, delay_s=8.0)

    if not burst:
        print("aucune image acquise", file=sys.stderr)
        return 1

    # 3. Mesures
    results.append(check_frame_sanity(burst[0]))
    results.append(check_compression_artifacts(burst[0]))
    results.append(check_vibration(burst))
    results.append(check_focus_stability(burst))
    if cfg.scan.mode == "ptz":
        results.append(check_preset_repeatability(returns))

    baseline = None
    if args.baseline and Path(args.baseline).exists():
        baseline = float(sobel_energy(to_gray(np.load(args.baseline))).mean())
    results.append(check_window_cleanliness(burst[0], baseline))

    # 4. Rapport
    for r in results:
        print(r)
    summary = summarize(results)
    print(f"\n{summary['ok']} ok, {summary['warn']} avertissements, {summary['fail']} échecs")

    report = {
        "site_id": cfg.site_id,
        "tier": cfg.tier,
        "simulated": args.simulate,
        "summary": summary,
        "checks": [
            {"name": r.name, "status": r.status, "value": r.value, "unit": r.unit,
             "message": r.message, "detail": r.detail}
            for r in results
        ],
    }
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Rapport écrit dans {args.output}")

    if summary["fail"]:
        print("\nDes contrôles ont échoué : corriger avant la mise en service.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
