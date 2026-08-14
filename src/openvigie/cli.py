"""Interface en ligne de commande."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import TIERS, load_site_config, tier_defaults
from .geometry import plan_uniform_ring, scan_budget
from .hwcheck import run_config_checks, summarize
from .ptz import ScanScheduler, health_warnings


def _load(args) -> object:
    if getattr(args, "config", None):
        return load_site_config(args.config)
    return tier_defaults(getattr(args, "tier", "minimal"))


def cmd_plan(args) -> int:
    """Calcule le plan de couverture et le budget de balayage."""
    cfg = _load(args)
    sensor = cfg.optics.sensor_spec()
    lens = cfg.optics.lens_spec()
    views = plan_uniform_ring(sensor, lens, cfg.scan.n_views, cfg.scan.target_range_m, cfg.scan.overlap)
    budget = scan_budget(cfg.scan.n_views, cfg.scan.dwell_s, cfg.scan.settle_s, cfg.scan.mode == "ptz")

    out = {
        "tier": cfg.tier,
        "sensor": sensor.name,
        "mode": cfg.scan.mode,
        "views": [v.as_dict() for v in views],
        "budget": budget.as_dict(),
    }
    if cfg.scan.mode == "ptz":
        out["warnings"] = health_warnings(
            ScanScheduler(views, cfg.scan.dwell_s, cfg.scan.settle_s, cfg.scan.priority_views)
        )
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print(f"Tier {cfg.tier} — capteur {sensor.name} — mode {cfg.scan.mode}")
    print(f"{len(views)} vues, champ {views[0].hfov_deg:.1f}°, focale {views[0].focal_mm:.2f} mm")
    print(
        f"Panache minimum détectable à {cfg.scan.target_range_m / 1000:.1f} km : "
        f"{views[0].min_plume_m:.0f} m"
    )
    print(f"Cycle : {budget.cycle_s / 60:.2f} min — plancher de latence {budget.detection_latency_floor_s / 60:.2f} min")
    if budget.moves_per_year:
        print(f"Usure : {budget.moves_per_year:,.0f} mouvements/an")
    for w in out.get("warnings", []):
        print(f"  ! {w}")
    return 0


def cmd_doctor(args) -> int:
    """Vérifications statiques de configuration (sans matériel)."""
    cfg = _load(args)
    results = run_config_checks(cfg)
    for r in results:
        print(r)
    summary = summarize(results)
    print(f"\n{summary['ok']} ok, {summary['warn']} avertissements, {summary['fail']} échecs")
    return 0 if summary["all_passed"] else 1


def cmd_selftest(args) -> int:
    """Exécute le pipeline de bout en bout sur une scène synthétique."""
    from .geometry import horizon_row
    from .pipeline import DetectionPipeline
    from .sources import SyntheticScene, SyntheticSource

    cfg = _load(args)
    # L'autotest vérifie la chaîne de détection, pas la politique d'émission :
    # on force le mode `shadow` pour que les événements soient produits sans
    # jamais être transmis (AUDIT P0-06).
    cfg.operating.mode = args.mode_operating
    pipe = DetectionPipeline(cfg)
    state = pipe.register_view("V00", azimuth_deg=0.0, focal_mm=cfg.optics.focal_mm)

    # La scène de test est construite avec le MÊME horizon que le modèle
    # géométrique : c'est tout l'intérêt du test négatif « nuage ».
    sensor = cfg.optics.sensor_spec()
    scene_h, scene_w = 180, 320
    hr = int(round(horizon_row(sensor, state.focal_mm, cfg.optics.tilt_deg) * scene_h / sensor.height_px))
    scene = SyntheticScene(height=scene_h, width=scene_w, horizon_row=max(10, min(scene_h - 30, hr)))
    src = SyntheticSource(scene=scene, mode=args.mode, n_background=6, n_plume=8)
    t = 0.0
    statuses: list[str] = []
    while True:
        item = src.read()
        if item is None:
            break
        frame, ts = item
        res = pipe.process_frame("V00", frame, ts, t_monotonic=t)
        statuses.append(res.status)
        for a in res.alerts:
            print(f"ALERTE {a.alert_id} azimut={a.bearing_deg}° score={a.score} visites={a.n_visits}")
        t += src.period_s

    print(json.dumps(pipe.summary(), indent=2, ensure_ascii=False))
    expected_alerts = args.mode == "plume" and args.mode_operating != "measure"
    got = pipe.stats["alerts"] > 0
    if got != expected_alerts:
        print(f"ÉCHEC: mode={args.mode}, alertes attendues={expected_alerts}, obtenues={got}", file=sys.stderr)
        return 1
    print("Autotest réussi.")
    return 0


def cmd_ptz_test(args) -> int:
    """Génère et affiche les trames Pelco-D d'un plan de balayage (sans matériel)."""
    from .ptz import SimulatedPtz, pelco_d_goto_preset

    cfg = _load(args)
    views = plan_uniform_ring(
        cfg.optics.sensor_spec(), cfg.optics.lens_spec(), cfg.scan.n_views,
        cfg.scan.target_range_m, cfg.scan.overlap,
    )
    sched = ScanScheduler(views, cfg.scan.dwell_s, cfg.scan.settle_s)
    backend = SimulatedPtz()
    for slot in sched.plan_cycle():
        frame = pelco_d_goto_preset(args.address, slot.preset)
        backend.goto_preset(slot.preset)
        print(
            f"{slot.view.view_id} az={slot.view.azimuth_deg:6.1f}° preset={slot.preset:3d} "
            f"pelco-d={frame.hex(' ')} fenêtre=[{slot.t_settled_s:.0f};{slot.t_leave_s:.0f}]s"
        )
    print(f"\nCycle {sched.cycle_s / 60:.2f} min, {sched.moves_per_year:,.0f} mouvements/an")
    for w in health_warnings(sched):
        print(f"  ! {w}")
    return 0


def cmd_hw(args) -> int:
    """Inventaire matériel OpenIPC et verdict de compatibilité."""
    from .platform import (
        SENSOR_DRIVER_STATUS,
        SOC_MATRIX,
        compatibility_report,
        detect_platform,
        select_backend,
    )

    if args.matrix:
        print(f"{'SoC':<14} {'famille':<18} {'accél.':<8} {'IVE':<4} backend conseillé")
        for soc, caps in sorted(SOC_MATRIX.items()):
            print(f"{soc:<14} {caps.family:<18} {caps.accelerator:<8} "
                  f"{'oui' if caps.has_ive else 'non':<4} {caps.recommended_backend}")
        print(f"\n{'Capteur':<10} pilote OpenIPC")
        for sensor, status in sorted(SENSOR_DRIVER_STATUS.items()):
            print(f"{sensor:<10} {status}")
        return 0

    if args.soc and args.sensor:
        report = compatibility_report(args.soc, args.sensor)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        print(f"SoC {report['soc']} + capteur {report['sensor']}")
        print(f"  statut  : {report['status']} — {report['verdict']}")
        print(f"  backend : {report['recommended_backend']}"
              f" (CNN local {'possible' if report['can_run_cnn_locally'] else 'impossible'})")
        for note in report["notes"]:
            print(f"  note    : {note}")
        return 0 if report["status"] in ("ready", "porting_required") else 1

    info = detect_platform()
    backend, why = select_backend(info, args.backend)
    if args.json:
        print(json.dumps({**info.as_dict(), "backend": backend, "reason": why}, indent=2, ensure_ascii=False))
        return 0
    print(f"Firmware OpenIPC : {'oui' if info.is_openipc else 'non détecté'}"
          + (f" ({info.firmware})" if info.firmware else ""))
    print(f"SoC              : {info.soc or 'inconnu'} (source: {info.source})")
    print(f"Capteur          : {info.sensor or 'inconnu'}")
    print(f"Accélérateur     : {info.capabilities.accelerator}"
          f" — {info.capabilities.accel_note}")
    print(f"Backend retenu   : {backend} ({why})")
    return 0


def cmd_majestic(args) -> int:
    """Profil de configuration majestic recommandé pour la détection."""
    from .platform import (
        MAJESTIC_DETECTION_PROFILE,
        MAJESTIC_WARNINGS,
        OpenIpcCamera,
        detection_profile_commands,
    )

    cam = OpenIpcCamera(host=args.host, user=args.user)
    print(f"# Snapshot (chemin recommandé) : {cam.snapshot_url}")
    print(f"# RTSP (repli seulement)       : {cam.rtsp_url}\n")
    for cmd, (_key, (_v, reason)) in zip(
        detection_profile_commands(cam), MAJESTIC_DETECTION_PROFILE.items(), strict=True
    ):
        print(f"{cmd}\n    # {reason}")
    print("\n# Réglages à ne pas appliquer à l'aveugle :")
    for key, warn in MAJESTIC_WARNINGS.items():
        print(f"#   {key}: {warn}")
    return 0


def cmd_sectors(args) -> int:
    """Secteurs utiles et comparaison d'architectures (issue #1).

    La planification ne suppose plus une couverture 360° : elle part des
    secteurs réellement exploitables et compare les architectures qui peuvent
    les couvrir.
    """
    from .modules import CostModel, compare_architectures, recommend, sectors_from_viewshed, total_span_deg

    cfg = _load(args)
    sensor = cfg.optics.sensor_spec()
    lens = cfg.optics.lens_spec()

    if args.from_viewshed:
        from .dem import DEM, synthetic_dem, viewshed_ranges

        dem = synthetic_dem() if args.synthetic else DEM.from_npy(args.from_viewshed)
        ranges = viewshed_ranges(dem, cfg.latitude, cfg.longitude,
                                 cfg.optics.camera_height_m, n_sectors=args.viewshed_sectors,
                                 step_m=100.0)
        sectors = sectors_from_viewshed(ranges, min_useful_range_m=args.min_range)
        if not sectors:
            print("Aucun secteur utile : le relief masque tout au-delà de "
                  f"{args.min_range:.0f} m depuis ce point.", file=sys.stderr)
            return 1
    else:
        sectors = cfg.sector_list()

    evaluations = compare_architectures(
        sectors, sensor, lens, CostModel(), overlap=cfg.scan.overlap
    )

    if args.json:
        print(json.dumps({
            "sectors": [s.as_dict() for s in sectors],
            "total_span_deg": round(total_span_deg(sectors), 1),
            "architectures": [e.as_dict() for e in evaluations],
            "recommendation": recommend(evaluations, args.max_latency * 60.0),
        }, indent=2, ensure_ascii=False))
        return 0

    span = total_span_deg(sectors)
    print(f"Secteurs utiles — {span:.0f}° au total "
          f"({span / 360 * 100:.0f} % d'un tour d'horizon)\n")
    for s in sectors:
        print(f"  {s.name:<8} {s.start_deg:6.1f}° → {s.end_deg:6.1f}°  "
              f"({s.span_deg:5.1f}°)  portée utile {s.max_range_m / 1000:5.1f} km")

    print(f"\n{'Architecture':<32}{'cam.':>5}{'pos.':>6}{'portée':>9}{'latence':>10}"
          f"{'mvts/an':>12}{'coût':>9}")
    for e in evaluations:
        d = e.as_dict()
        flag = "" if d["covers_all"] else "  (couverture incomplète)"
        print(f"{d['name']:<32}{d['cameras']:>5}{d['presets']:>6}"
              f"{d['detection_range_km']:>7.1f}km{d['mean_latency_min']:>8.1f}min"
              f"{d['ptz_moves_per_year']:>12,.0f}{d['cost_usd']:>8.0f}${flag}")
    print()
    for e in evaluations:
        for note in e.notes:
            print(f"  ! [{e.name}] {note}")
    print(f"\nRecommandation : {recommend(evaluations, args.max_latency * 60.0)}")
    print("Aucune architecture n'est meilleure dans l'absolu : le classement dépend\n"
          "de ce qu'on accepte de perdre — portée, latence, usure ou coût.")
    return 0


def cmd_survey(args) -> int:
    """Relevé d'installation : contrôle et pose déduite (issue #2)."""
    from .survey import InstallationSurvey, SurveyError, check_survey

    cfg = _load(args)
    sensor = cfg.optics.sensor_spec()

    if args.file:
        try:
            survey = InstallationSurvey.load(args.file)
        except SurveyError as exc:
            print(f"Relevé invalide : {exc}", file=sys.stderr)
            return 1
    else:
        if args.declination is None:
            print("--declination requis : un smartphone donne le nord MAGNÉTIQUE. "
                  "Sans conversion, l'azimut est biaisé de 1 à 3° en France.", file=sys.stderr)
            return 2
        try:
            survey = InstallationSurvey(
                view_id=args.view, latitude=args.lat, longitude=args.lon,
                ground_altitude_m=args.altitude, camera_height_m=args.height,
                azimuth_magnetic_deg=args.azimuth, magnetic_declination_deg=args.declination,
                tilt_deg=args.tilt, roll_deg=args.roll, mounting=args.mounting,
            )
        except SurveyError as exc:
            print(f"Relevé invalide : {exc}", file=sys.stderr)
            return 1

    problems = check_survey(survey)
    pose = survey.to_pose(sensor, cfg.optics.focal_mm, args.width, args.height_px)
    axes = survey.gate_axes_px(sensor, cfg.optics.focal_mm, args.width)

    if args.json:
        print(json.dumps({
            "survey": survey.as_dict(), "pose": pose.as_dict(),
            "prior_sigma": survey.prior_sigma(), "gate_axes_px": axes,
            "problems": problems,
        }, indent=2, ensure_ascii=False))
        return 0 if not problems else 1

    print(f"Relevé — vue {survey.view_id}, montage '{survey.mounting}'\n")
    print(f"  position       : {survey.latitude:.5f}, {survey.longitude:.5f}")
    print(f"  altitude       : {survey.ground_altitude_m:.0f} m terrain "
          f"+ {survey.camera_height_m:.0f} m mât = {survey.camera_altitude_m:.0f} m")
    print(f"  azimut magnét. : {survey.azimuth_magnetic_deg:.1f}°")
    print(f"  déclinaison    : {survey.magnetic_declination_deg:+.1f}°")
    print(f"  → azimut vrai  : {survey.azimuth_true_deg:.1f}°  ± {survey.azimuth_sigma_deg:.1f}°")
    print(f"  assiette       : {survey.tilt_deg:+.2f}°  ± {survey.tilt_sigma_deg:.1f}°  (accéléromètre)")
    print(f"  roulis         : {survey.roll_deg:+.2f}°  ± {survey.roll_sigma_deg:.1f}°")
    print(f"\n  fenêtre d'appariement : {axes['horizontal_px']:.0f} px horizontal, "
          f"{axes['vertical_px']:.0f} px vertical")
    ratio = axes["horizontal_px"] / max(axes["vertical_px"], 1e-6)
    print(f"  soit un facteur {ratio:.0f} entre les deux axes — l'accéléromètre mesure la")
    print("  gravité, que rien ne perturbe ; le magnétomètre mesure un champ que")
    print("  l'acier d'un pylône déforme massivement.")
    if problems:
        print()
        for p in problems:
            print(f"  ! {p}")
    print("\n  Ce relevé sert d'amorce : l'étalonnage par trafic aérien affine ensuite")
    print("  l'azimut, que le relevé mesure justement mal. Voir docs/CALIBRATION.md.")
    return 0 if not problems else 1


def cmd_sensor_validate(args) -> int:
    """Valide un portage capteur sur cible (voir docs/PORTAGE_IMX675.md).

    À exécuter par qui dispose du matériel : c'est le seul moyen de vérifier
    qu'un pilote fraîchement porté livre bien ce que le budget optique suppose.
    """
    import time

    from .hwcheck import (
        check_field_of_view,
        check_frame_interval,
        check_frame_sanity,
        check_sensor_resolution,
        summarize,
    )
    from .platform import sensor_driver_status

    cfg = _load(args)
    sensor = cfg.optics.sensor_spec()
    results = []

    driver = sensor_driver_status(args.sensor or cfg.optics.sensor)
    results.append(CheckResultShim(
        "pilote", "warn" if driver == "porting" else ("ok" if driver == "upstream" else "fail"),
        f"pilote {args.sensor or cfg.optics.sensor} : {driver}",
    ).to_result())

    if args.simulate:
        import numpy as np

        frames = [np.random.default_rng(i).integers(0, 255, (sensor.height_px // 4,
                                                             sensor.width_px // 4, 3),
                                                    dtype=np.uint8) for i in range(6)]
        timestamps = [i / args.expect_fps for i in range(6)]
        if not args.json:
            print("Mode simulé : la résolution sera signalée non conforme, c'est attendu.\n")
    else:
        if not args.snapshot_url and not args.host:
            print("--host ou --snapshot-url requis (ou --simulate)", file=sys.stderr)
            return 2
        from .sources import SnapshotHttpSource

        url = args.snapshot_url or f"http://{args.host}/image.jpg"
        source = SnapshotHttpSource(url, args.user, args.password)
        frames, timestamps = [], []
        for _ in range(args.frames):
            item = source.read()
            if item is None:
                break
            frames.append(item[0])
            timestamps.append(time.time())
            time.sleep(1.0 / max(args.expect_fps, 1e-6))
        if not frames:
            print(f"aucune image obtenue depuis {url}", file=sys.stderr)
            return 1

    results.append(check_frame_sanity(frames[0]))
    results.append(check_sensor_resolution(frames[0], sensor))
    results.append(check_frame_interval(timestamps, args.expect_fps))
    if args.measured_hfov is not None:
        results.append(check_field_of_view(args.measured_hfov, sensor, cfg.optics.focal_mm))

    if args.json:
        print(json.dumps([{"name": r.name, "status": r.status, "value": r.value,
                           "unit": r.unit, "message": r.message} for r in results],
                         indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(r)
        s = summarize(results)
        print(f"\n{s['ok']} ok, {s['warn']} avertissements, {s['fail']} échecs")
        if args.measured_hfov is None:
            print("\nAstuce : --measured-hfov <deg> vérifie d'un coup le pas de pixel, la")
            print("taille de matrice et la focale réelle. C'est le contrôle le plus")
            print("révélateur d'un portage.")
    return 0 if summarize(results)["fail"] == 0 else 1


class CheckResultShim:
    """Petit adaptateur pour produire un CheckResult sans dupliquer sa définition."""

    def __init__(self, name: str, status: str, message: str) -> None:
        self.name, self.status, self.message = name, status, message

    def to_result(self):
        from .hwcheck import CheckResult

        return CheckResult(self.name, self.status, None, "", self.message)


def cmd_capabilities(args) -> int:
    """Ce que le site peut RÉELLEMENT faire, par opposition à ce qu'il déclare.

    AUDIT P0-03/P1-11 : plusieurs drapeaux de configuration décrivaient des
    fonctions non raccordées au pipeline. Cette commande existe pour qu'aucun
    utilisateur ne confonde une bibliothèque de recherche avec un système
    opérationnel.
    """
    from .hwcheck import capabilities

    cfg = _load(args)
    caps = capabilities(cfg)
    if args.json:
        print(json.dumps({k: {"available": v[0], "detail": v[1]} for k, v in caps.items()},
                         indent=2, ensure_ascii=False))
        return 0
    print(f"Capacités effectives — tier {cfg.tier}, mode '{cfg.operating.mode}'\n")
    for name, (ok, detail) in caps.items():
        print(f"  {'✓' if ok else '✗'} {name:<34} {detail}")
    missing = [k for k, (ok, _) in caps.items() if not ok]
    print(f"\n  {len(caps) - len(missing)}/{len(caps)} disponibles.")
    if missing:
        print("  Les fonctions absentes sont suivies dans ROADMAP.md.")
    return 0


def cmd_schema(args) -> int:
    """Affiche le schéma d'événement — le contrat entre tous les composants."""
    from .events import OPERATOR_DECISIONS, SCHEMA_VERSION, STATES, TRANSITIONS, DetectionEvent

    if args.json:
        example = DetectionEvent(
            event_id="00000000-0000-0000-0000-000000000000",
            site_id="tour-01", camera_id="V03",
            detected_at="2026-08-12T14:31:07+00:00",
        )
        print(json.dumps(
            {"schema_version": SCHEMA_VERSION, "states": list(STATES),
             "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
             "operator_decisions": list(OPERATOR_DECISIONS),
             "example": example.as_dict()},
            indent=2, ensure_ascii=False))
        return 0

    print(f"Schéma d'événement v{SCHEMA_VERSION}\n")
    print("Cycle de vie :")
    for state in STATES:
        nxt = TRANSITIONS.get(state, ())
        print(f"  {state:<20} → {', '.join(nxt) if nxt else '(terminal)'}")
    print("\nDécisions opérateur (chacune devient une classe de négatifs) :")
    print("  " + ", ".join(OPERATOR_DECISIONS))
    return 0


def cmd_outbox(args) -> int:
    """Inspecte ou vide la file d'attente hors ligne."""
    from .transport import FileTransport, Outbox

    box = Outbox(args.dir)
    stats = box.stats()
    if args.flush:
        result = box.flush(FileTransport(args.to))
        print(json.dumps({**result, "flushed_to": args.to}, indent=2, ensure_ascii=False))
        return 0
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    print(f"File {args.dir}")
    print(f"  en attente     : {stats['pending']}")
    print(f"  échues         : {stats['due']}")
    print(f"  plus ancienne  : {stats['oldest_age_s'] / 60:.1f} min")
    print(f"  abandonnées    : {stats['dead_letters']}")
    for entry in box.pending()[: args.limit]:
        print(f"    {entry.event.summary_line()}  (tentatives: {entry.attempts})")
    return 0


def cmd_viewshed(args) -> int:
    """Portée utile par secteur, d'après le relief."""
    from .dem import DEM, synthetic_dem, viewshed_ranges

    cfg = _load(args)
    dem = synthetic_dem() if args.synthetic else DEM.from_npy(args.dem)
    ranges = viewshed_ranges(
        dem, cfg.latitude, cfg.longitude, cfg.optics.camera_height_m,
        n_sectors=args.sectors, max_distance_m=cfg.optics.max_distance_m, step_m=args.step,
    )
    if args.json:
        print(json.dumps({str(round(k, 1)): round(v) for k, v in ranges.items()}, indent=2))
        return 0
    values = sorted(ranges.values())
    print(f"Viewshed depuis {cfg.latitude:.4f}, {cfg.longitude:.4f} "
          f"à {cfg.optics.camera_height_m:.0f} m au-dessus du sol")
    for az, rng in sorted(ranges.items()):
        bar = "#" * int(rng / max(max(values), 1.0) * 40)
        print(f"  {az:6.1f}°  {rng / 1000:5.1f} km  {bar}")
    print(f"\nMédiane {values[len(values) // 2] / 1000:.1f} km · "
          f"min {values[0] / 1000:.1f} km · max {values[-1] / 1000:.1f} km")
    print("Les secteurs courts n'ont pas besoin d'une focale longue : "
          "utiliser plan_adaptive_ring plutôt qu'une couronne homogène.")
    return 0


def cmd_calibrate(args) -> int:
    """Étalonnage géométrique par trafic aérien (ADS-B)."""
    from .calibration import (
        CameraPose,
        StaticAdsbSource,
        calibrate,
        check_drift,
        synthesize_observations,
        synthesize_traffic,
    )

    cfg = _load(args)
    sensor = cfg.optics.sensor_spec()
    site = cfg.site()
    width = args.width or sensor.width_px
    height = args.height or sensor.height_px
    guess = CameraPose(
        yaw_deg=args.azimuth, pitch_deg=-cfg.optics.tilt_deg, roll_deg=0.0,
        focal_mm=cfg.optics.focal_mm, sensor=sensor, width_px=width, height_px=height,
    )

    fit = ["yaw_deg", "pitch_deg", "roll_deg", "focal_mm"]
    if cfg.calibration.fit_clock_offset:
        fit.append("clock_offset_s")
    if cfg.calibration.fit_altitude_offset:
        fit.append("altitude_offset_m")

    if args.simulate:
        # Vérification de bout en bout sans matériel : une pose vraie est
        # imposée, le trafic et les observations sont simulés avec bruit,
        # décalage d'horloge et fausses détections, puis on vérifie qu'on la
        # retrouve. C'est le test que doit passer toute modification du module.
        true_pose = guess.copy_with(
            yaw_deg=args.azimuth + args.true_yaw_error,
            pitch_deg=-cfg.optics.tilt_deg + args.true_pitch_error,
            roll_deg=0.6, focal_mm=cfg.optics.focal_mm * 1.02,
        )
        tracks = synthesize_traffic(site, n_aircraft=args.aircraft, seed=args.seed)
        observations = synthesize_observations(
            site, tracks, true_pose, noise_px=args.noise_px,
            clock_offset_s=args.clock_error, n_outliers=args.outliers, seed=args.seed + 8,
        )
    else:
        if not args.observations or not args.adsb:
            print("--observations et --adsb requis (ou --simulate)", file=sys.stderr)
            return 2
        observations = [
            __import__("openvigie.calibration", fromlist=["SkyObservation"]).SkyObservation(**o)
            for o in json.loads(Path(args.observations).read_text(encoding="utf-8"))
        ]
        tracks = StaticAdsbSource.from_jsonl(args.adsb).tracks(
            min(o.t for o in observations) - 120, max(o.t for o in observations) + 120
        )

    result = calibrate(site, observations, tracks, guess, gate_px=args.gate_px, fit=tuple(fit))

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"Étalonnage — {len(tracks)} aéronefs, {len(observations)} observations")
        print(f"  qualité        : {result.quality} ({result.n_used} points, "
              f"{result.n_rejected} écartés, RMS {result.rms_px:.2f} px = {result.rms_deg:.4f}°)")
        print(f"  lacet          : {result.pose.yaw_deg:9.4f}°  ± {result.sigma.get('yaw_deg', 0):.4f}°")
        print(f"  assiette       : {result.pose.pitch_deg:9.4f}°  ± {result.sigma.get('pitch_deg', 0):.4f}°"
              f"   (tilt {result.pose.tilt_deg:+.4f}°)")
        print(f"  roulis         : {result.pose.roll_deg:9.4f}°  ± {result.sigma.get('roll_deg', 0):.4f}°")
        print(f"  focale         : {result.pose.focal_mm:9.4f} mm ± {result.sigma.get('focal_mm', 0):.4f}")
        if "clock_offset_s" in result.fitted:
            print(f"  décalage horloge: {result.clock_offset_s:+.3f} s")
        if "altitude_offset_m" in result.fitted:
            print(f"  biais altitude : {result.altitude_offset_m:+.0f} m")
        print(f"  → sigma azimut à reporter dans les alertes : {result.bearing_sigma_deg:.4f}°")
        ident = result.identifiability
        print(f"  identifiabilité : dispersion des caps {ident.get('heading_spread_deg', 0):.1f}°, "
              f"étalement en azimut {ident.get('azimuth_spread_deg', 0):.1f}°, "
              f"plage d'élévation {ident.get('elevation_range_deg', 0):.1f}°")
        for note in result.notes:
            print(f"  ! {note}")

    if args.simulate:
        err_yaw = result.pose.yaw_deg - true_pose.yaw_deg
        err_pitch = result.pose.pitch_deg - true_pose.pitch_deg
        print(f"\n  vérification : erreur lacet {err_yaw:+.4f}°, assiette {err_pitch:+.4f}°")
        ok = abs(err_yaw) < args.tolerance and abs(err_pitch) < args.tolerance
        print("  " + ("recouvrement de la pose vraie : OK" if ok else "ÉCHEC : pose non retrouvée"))
        if not ok:
            return 1

    if args.output:
        result.save(args.output)
        print(f"\n  écrit dans {args.output}")

    if args.reference and Path(args.reference).exists():
        from .calibration import CalibrationResult

        ref_data = json.loads(Path(args.reference).read_text(encoding="utf-8"))
        ref_pose = guess.copy_with(
            yaw_deg=ref_data["pose"]["yaw_deg"], pitch_deg=ref_data["pose"]["pitch_deg"],
            roll_deg=ref_data["pose"]["roll_deg"], focal_mm=ref_data["pose"]["focal_mm"],
        )
        reference = CalibrationResult(
            pose=ref_pose, n_used=ref_data["n_used"], rms_px=ref_data["rms_px"], converged=True
        )
        drift = check_drift(
            reference, result,
            cfg.calibration.drift_yaw_tolerance_deg, cfg.calibration.drift_pitch_tolerance_deg,
        )
        print(f"\n  dérive : {drift['status']} — {drift['message']}")
        if drift["status"] == "drifted":
            return 1
    return 0


def cmd_init(args) -> int:
    """Écrit une configuration de départ pour un tier donné."""
    from .config import save_site_config

    cfg = tier_defaults(args.tier)
    cfg.site_id = args.site_id
    path = Path(args.output)
    if path.exists() and not args.force:
        print(f"{path} existe déjà (utiliser --force)", file=sys.stderr)
        return 1
    save_site_config(cfg, path)
    print(f"Configuration '{args.tier}' écrite dans {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openvigie", description="Détection précoce de feux de forêt depuis points hauts")
    p.add_argument("--version", action="version", version=f"OpenVigie {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("-c", "--config", help="fichier YAML de site")
        sp.add_argument("-t", "--tier", choices=TIERS, default="minimal", help="préréglage si pas de config")
        return sp

    sp = common(sub.add_parser("plan", help="plan de couverture et budget de balayage"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sp = common(sub.add_parser("doctor", help="vérifications de configuration"))
    sp.set_defaults(func=cmd_doctor)

    sp = common(sub.add_parser("selftest", help="pipeline de bout en bout sur scène synthétique"))
    sp.add_argument("--mode", choices=("plume", "cloud"), default="plume")
    sp.add_argument("--mode-operating", choices=("measure", "shadow", "alert"), default="shadow",
                    help="mode d'exploitation forcé pendant l'autotest")
    sp.set_defaults(func=cmd_selftest)

    sp = common(sub.add_parser("ptz-test", help="trames Pelco-D et fenêtres d'analyse"))
    sp.add_argument("--address", type=int, default=1)
    sp.set_defaults(func=cmd_ptz_test)

    sp = sub.add_parser("hw", help="inventaire OpenIPC et compatibilité SoC/capteur")
    sp.add_argument("--soc", help="évaluer un SoC sans être dessus")
    sp.add_argument("--sensor", help="capteur à évaluer avec --soc")
    sp.add_argument("--backend", default="auto")
    sp.add_argument("--matrix", action="store_true", help="afficher la matrice de compatibilité")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_hw)

    sp = sub.add_parser("majestic", help="profil majestic recommandé pour une caméra OpenIPC")
    sp.add_argument("--host", default="192.168.1.10")
    sp.add_argument("--user", default="root")
    sp.set_defaults(func=cmd_majestic)

    sp = common(sub.add_parser("sectors", help="secteurs utiles et comparaison d'architectures"))
    sp.add_argument("--from-viewshed", help="MNT .npy dont déduire les secteurs")
    sp.add_argument("--synthetic", action="store_true", help="relief de démonstration")
    sp.add_argument("--viewshed-sectors", type=int, default=36)
    sp.add_argument("--min-range", type=float, default=2000.0,
                    help="portée en deçà de laquelle une direction est jugée inutile")
    sp.add_argument("--max-latency", type=float, default=2.0, help="latence moyenne acceptable (min)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sectors)

    sp = common(sub.add_parser("survey", help="relevé d'installation GPS + orientation"))
    sp.add_argument("--file", help="relevé JSON existant")
    sp.add_argument("--view", default="V00")
    sp.add_argument("--lat", type=float, default=44.0)
    sp.add_argument("--lon", type=float, default=3.0)
    sp.add_argument("--altitude", type=float, default=0.0, help="altitude du TERRAIN (m)")
    sp.add_argument("--height", type=float, default=40.0, help="hauteur de la caméra au-dessus du sol")
    sp.add_argument("--azimuth", type=float, default=90.0, help="azimut MAGNÉTIQUE lu")
    sp.add_argument("--declination", type=float, help="déclinaison magnétique (positive vers l'est)")
    sp.add_argument("--tilt", type=float, default=1.5)
    sp.add_argument("--roll", type=float, default=0.0)
    sp.add_argument("--mounting", default="steel_tower", choices=("open", "steel_tower", "surveyed"))
    sp.add_argument("--width", type=int, default=0)
    sp.add_argument("--height-px", type=int, default=0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_survey)

    sp = common(sub.add_parser("sensor-validate", help="valider un portage capteur sur cible"))
    sp.add_argument("--host")
    sp.add_argument("--snapshot-url")
    sp.add_argument("--user", default="root")
    sp.add_argument("--password", default="")
    sp.add_argument("--sensor", help="capteur attendu (défaut : celui de la configuration)")
    sp.add_argument("--expect-fps", type=float, default=25.0)
    sp.add_argument("--frames", type=int, default=8)
    sp.add_argument("--measured-hfov", type=float,
                    help="champ horizontal mesuré sur amers, en degrés")
    sp.add_argument("--simulate", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sensor_validate)

    sp = common(sub.add_parser("capabilities", help="capacités réellement disponibles"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_capabilities)

    sp = sub.add_parser("schema", help="schéma d'événement et cycle de vie")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_schema)

    sp = sub.add_parser("outbox", help="file d'attente hors ligne")
    sp.add_argument("--dir", default="data/outbox")
    sp.add_argument("--flush", action="store_true")
    sp.add_argument("--to", default="data/events.jsonl")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_outbox)

    sp = common(sub.add_parser("viewshed", help="portée utile par secteur d'après le relief"))
    sp.add_argument("--dem", help="MNT .npy (avec .json de géoréférencement)")
    sp.add_argument("--synthetic", action="store_true", help="relief de démonstration")
    sp.add_argument("--sectors", type=int, default=24)
    sp.add_argument("--step", type=float, default=50.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_viewshed)

    sp = common(sub.add_parser("calibrate", help="étalonnage géométrique par trafic aérien"))
    sp.add_argument("--azimuth", type=float, default=90.0, help="azimut approximatif de la vue (boussole)")
    sp.add_argument("--width", type=int, help="largeur de l'image analysée")
    sp.add_argument("--height", type=int, help="hauteur de l'image analysée")
    sp.add_argument("--observations", help="JSON des points détectés dans le ciel")
    sp.add_argument("--adsb", help="JSONL des positions ADS-B")
    sp.add_argument("--gate-px", type=float, default=200.0)
    sp.add_argument("--output", help="fichier de sortie de l'étalonnage")
    sp.add_argument("--reference", help="étalonnage de référence, pour contrôler la dérive")
    sp.add_argument("--simulate", action="store_true", help="validation sans matériel")
    sp.add_argument("--aircraft", type=int, default=25)
    sp.add_argument("--noise-px", type=float, default=1.5)
    sp.add_argument("--outliers", type=int, default=5)
    sp.add_argument("--clock-error", type=float, default=0.0, help="décalage d'horloge injecté (s)")
    sp.add_argument("--true-yaw-error", type=float, default=-2.7, help="erreur de boussole simulée")
    sp.add_argument("--true-pitch-error", type=float, default=0.6)
    sp.add_argument("--tolerance", type=float, default=0.1)
    sp.add_argument("--seed", type=int, default=3)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("init", help="générer une configuration de site")
    sp.add_argument("tier", choices=TIERS)
    sp.add_argument("-o", "--output", default="site.yaml")
    sp.add_argument("--site-id", default="site-01")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
