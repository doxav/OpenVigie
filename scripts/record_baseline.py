#!/usr/bin/env python3
"""Enregistrement de la référence d'un site (phase 1, en continu).

Ce script constitue les deux actifs les plus précieux du projet, et les seuls
qu'aucun jeu de données public ne peut remplacer :

  1. **la banque de fonds** du site — ce à quoi chaque vue ressemble
     habituellement, par heure, par saison, en jour/crépuscule/nuit ;
  2. **la bibliothèque de négatifs** — 30 jours de flux réel, 24/24, qui servira
     à fixer le seuil de décision sur un budget de fausses alertes par jour.

À faire tourner dès l'installation du mât, **avant** toute mise en service de la
détection. Sans ces 30 jours, tout seuil est deviné.

Usage :
  python scripts/record_baseline.py --config config/tiers/medium.yaml \\
      --camera V00=http://192.168.1.64/image.jpg \\
      --camera V01=http://192.168.1.65/image.jpg \\
      --period 30 --days 30 --out data/site-01

  # sans matériel, pour valider la chaîne :
  python scripts/record_baseline.py --config config/tiers/medium.yaml --simulate --cycles 200
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openvigie.background import BackgroundBank, BackgroundKey  # noqa: E402
from openvigie.compat import HAS_CV2, cv2  # noqa: E402
from openvigie.config import load_site_config, tier_defaults  # noqa: E402
from openvigie.geometry import estimate_visibility_m  # noqa: E402
from openvigie.hwcheck import check_compression_artifacts, check_frame_sanity  # noqa: E402
from openvigie.registration import phase_correlate  # noqa: E402
from openvigie.sources import SnapshotHttpSource, SyntheticScene  # noqa: E402

_STOP = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True
    print("\n-- arrêt demandé, sauvegarde en cours…")


def save_frame(frame: np.ndarray, path: Path, quality: int = 92) -> bool:
    """Enregistre un snapshot. Qualité élevée : c'est la donnée d'entraînement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if HAS_CV2:
        bgr = cv2.cvtColor(np.asarray(frame).astype(np.uint8), cv2.COLOR_RGB2BGR)
        return bool(cv2.imwrite(str(path), bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality]))
    np.save(path.with_suffix(".npy"), np.asarray(frame).astype(np.uint8))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Enregistrement de la référence d'un site")
    ap.add_argument("--config", help="configuration YAML du site")
    ap.add_argument("--tier", default="medium")
    ap.add_argument("--camera", action="append", default=[],
                    help="VUE=URL_SNAPSHOT, répétable (ex. V00=http://ip/image.jpg)")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--period", type=float, default=30.0, help="période d'échantillonnage (s)")
    ap.add_argument("--days", type=float, default=30.0, help="durée de la campagne (jours)")
    ap.add_argument("--cycles", type=int, help="nombre de cycles (prioritaire sur --days)")
    ap.add_argument("--out", default="data/baseline")
    ap.add_argument("--keep-every", type=int, default=4,
                    help="conserver 1 image sur N sur disque (la banque de fonds les voit toutes)")
    ap.add_argument("--simulate", action="store_true")
    args = ap.parse_args()

    cfg = load_site_config(args.config) if args.config else tier_defaults(args.tier)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # -- sources ------------------------------------------------------------- #
    sources: dict[str, object] = {}
    if args.simulate:
        scene = SyntheticScene(height=240, width=320, horizon_row=120)
        for i in range(min(cfg.scan.n_views, 4)):
            sources[f"V{i:02d}"] = scene
    else:
        if not args.camera:
            print("--camera VUE=URL requis (ou --simulate)", file=sys.stderr)
            return 2
        for spec in args.camera:
            if "=" not in spec:
                print(f"format attendu VUE=URL, reçu '{spec}'", file=sys.stderr)
                return 2
            view, url = spec.split("=", 1)
            sources[view] = SnapshotHttpSource(url, args.user, args.password, args.period)

    bank = BackgroundBank(buffer_size=12, min_samples=4)
    n_cycles = args.cycles if args.cycles else int(args.days * 86400 / max(args.period, 1e-6))
    signal.signal(signal.SIGINT, _handle_signal)

    print(f"Site {cfg.site_id} — {len(sources)} vue(s), {n_cycles} cycles de {args.period:.0f} s")
    print(f"Sortie : {out.resolve()}\n")

    journal: list[dict] = []
    previous: dict[str, np.ndarray] = {}
    kept = 0

    for cycle in range(n_cycles):
        if _STOP:
            break
        now = dt.datetime.now()
        for view, source in sources.items():
            frame = source.frame() if args.simulate else (source.read() or (None,))[0]
            if frame is None:
                journal.append({"cycle": cycle, "view": view, "status": "no_frame"})
                continue

            key = BackgroundKey.build(view, now)
            bank.update(key, frame)

            entry = {
                "cycle": cycle,
                "view": view,
                "timestamp": now.isoformat(),
                "state": key.daynight,
                "season": key.season,
                "sanity": check_frame_sanity(frame).status,
            }
            if cycle % 20 == 0:
                entry["compression"] = check_compression_artifacts(frame).status
            if view in previous:
                entry["drift_px"] = round(phase_correlate(previous[view], frame).magnitude_px, 2)
            previous[view] = frame
            journal.append(entry)

            if cycle % max(args.keep_every, 1) == 0:
                stamp = now.strftime("%Y%m%dT%H%M%S")
                save_frame(frame, out / "frames" / view / key.daynight / f"{stamp}.jpg")
                kept += 1

        if cycle % 50 == 0:
            bank.save(out / "background_bank.npz")
            (out / "journal.jsonl").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in journal), encoding="utf-8"
            )
            ready = sum(1 for k in bank.maturity() if bank.buffer_size)
            print(f"  cycle {cycle:6d}  {now:%Y-%m-%d %H:%M}  clés de fond: {ready:3d}  images conservées: {kept}")

        if not args.simulate:
            time.sleep(args.period)

    bank.save(out / "background_bank.npz")
    (out / "journal.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in journal), encoding="utf-8"
    )

    states = {}
    for e in journal:
        states[e.get("state", "?")] = states.get(e.get("state", "?"), 0) + 1
    summary = {
        "site_id": cfg.site_id,
        "cycles": cycle + 1,
        "views": sorted(sources),
        "frames_kept": kept,
        "background_keys": len(bank.maturity()),
        "state_distribution": states,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nBanque de fonds : {len(bank.maturity())} clés")
    print(f"Images conservées : {kept}")
    print(f"Répartition jour/nuit : {states}")
    print(f"\nÉtapes suivantes :\n"
          f"  1. annoter les faux candidats de {out}/frames comme négatifs du site ;\n"
          f"  2. calibrer le seuil avec scoring.threshold_for_fp_budget() sur ces négatifs ;\n"
          f"  3. seulement ensuite, activer la détection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
