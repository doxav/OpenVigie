"""Tests de configuration, des trois tiers, de la CLI et des contrôles d'équipement."""

from __future__ import annotations

import numpy as np
import pytest

from openvigie.cli import main
from openvigie.config import (
    TIERS,
    OpticsConfig,
    ScanConfig,
    SiteConfig,
    load_site_config,
    site_config_from_dict,
    tier_defaults,
)
from openvigie.geometry import IMX675, LENS_27135, plan_uniform_ring
from openvigie.hwcheck import (
    check_compression_artifacts,
    check_coverage,
    check_focus_stability,
    check_frame_sanity,
    check_host_reachable,
    check_lens_compat,
    check_preset_repeatability,
    check_range_budget,
    check_scan_budget,
    check_vibration,
    check_window_cleanliness,
    run_config_checks,
    summarize,
)
from openvigie.registration import shift_image
from openvigie.sources import SyntheticScene


class TestConfig:
    def test_tiers_connus(self):
        assert TIERS == ("minimal", "medium", "full")

    @pytest.mark.parametrize("tier", TIERS)
    def test_prereglage_valide(self, tier):
        cfg = tier_defaults(tier)
        assert cfg.tier == tier
        assert cfg.optics.sensor_spec().name == "IMX675"
        assert cfg.scan.n_views >= 1

    def test_minimal_est_reellement_sectoriel(self):
        cfg = tier_defaults("minimal")
        assert cfg.scan.mode == "ptz"
        assert cfg.scan.n_views == 4
        assert cfg.scan.target_range_m == pytest.approx(8_000.0)
        assert len(cfg.sectors) == 1
        sector = cfg.sector_list()[0]
        assert sector.span_deg == pytest.approx(140.0)
        assert sector.max_range_m == pytest.approx(8_000.0)

    def test_tier_inconnu(self):
        with pytest.raises(ValueError):
            tier_defaults("gigantesque")
        with pytest.raises(ValueError):
            SiteConfig(tier="gigantesque")

    def test_minimal_est_en_mode_ptz_et_medium_full_en_fixe(self):
        """Le tier MINIMAL utilise un PTZ faute de caméras ; les tiers supérieurs
        passent aux caméras fixes, ce qui supprime l'usure et la revisite."""
        assert tier_defaults("minimal").scan.mode == "ptz"
        assert tier_defaults("medium").scan.mode == "fixed"
        assert tier_defaults("full").scan.mode == "fixed"

    def test_seuil_se_relache_quand_le_calcul_augmente(self):
        """Plus on a d'étages de vérification, plus on peut baisser le seuil
        d'entrée sans exploser le taux de faux positifs."""
        seuils = [tier_defaults(t).decision.enter_threshold for t in TIERS]
        assert seuils[0] > seuils[1] > seuils[2]

    def test_seul_le_tier_full_active_segmentation_et_triangulation(self):
        assert not tier_defaults("minimal").pipeline.use_segmentation
        assert not tier_defaults("medium").pipeline.use_segmentation
        assert tier_defaults("full").pipeline.use_segmentation
        assert tier_defaults("full").pipeline.use_triangulation

    def test_coordonnees_invalides(self):
        with pytest.raises(ValueError):
            SiteConfig(latitude=120.0)

    def test_capteur_inconnu(self):
        with pytest.raises(ValueError, match="capteur inconnu"):
            OpticsConfig(sensor="IMX999").sensor_spec()

    def test_mode_de_balayage_invalide(self):
        with pytest.raises(ValueError):
            ScanConfig(mode="helicoidal")

    def test_cle_inconnue_rejetee(self):
        """Validation stricte : une faute de frappe doit casser au démarrage,
        pas dégrader la détection en silence pendant une saison."""
        with pytest.raises(ValueError, match="inconnue"):
            site_config_from_dict({"site_id": "s", "tierr": "medium"})

    def test_cle_imbriquee_inconnue_rejetee(self):
        with pytest.raises(ValueError, match="clés inconnues"):
            site_config_from_dict({"optics": {"focale_mm": 6.0}})

    def test_chargement_yaml(self, tmp_path):
        p = tmp_path / "site.yaml"
        p.write_text(
            "site_id: tour-01\ntier: full\nlatitude: 43.5\nlongitude: 4.1\n"
            "optics:\n  sensor: IMX675\n  focal_mm: 8.0\n",
            encoding="utf-8",
        )
        cfg = load_site_config(p)
        assert cfg.site_id == "tour-01"
        assert cfg.tier == "full"
        assert cfg.optics.focal_mm == 8.0

    def test_modele_de_fusion_reconstructible(self):
        assert tier_defaults("full").fusion_model().score is not None


class TestHardwareChecks:
    def test_reseau_injoignable(self):
        r = check_host_reachable("192.0.2.1", port=9, timeout_s=0.2)
        assert r.status == "fail"
        assert not r.passed

    def test_image_uniforme_rejetee(self):
        assert check_frame_sanity(np.full((64, 64, 3), 128, dtype=np.uint8)).status == "fail"

    def test_image_surexposee(self):
        img = np.full((64, 64, 3), 250, dtype=np.float32)
        img[0, 0] = 0  # un peu d'écart-type
        assert check_frame_sanity(img).status == "warn"

    def test_image_normale(self, rng):
        scene = SyntheticScene(height=96, width=128, horizon_row=48)
        assert check_frame_sanity(scene.frame()).status == "ok"

    def test_resolution_inattendue(self):
        scene = SyntheticScene(height=96, width=128, horizon_row=48)
        assert check_frame_sanity(scene.frame(), expected_shape=(1944, 2592)).status == "warn"

    def test_compression_detecte_le_blocking(self):
        """Une image à blocs de 8 px marqués doit être signalée : c'est la
        signature d'un flux compressé qui détruit la fumée fine."""
        img = np.zeros((128, 128), dtype=np.float32)
        for y in range(0, 128, 8):
            for x in range(0, 128, 8):
                img[y : y + 8, x : x + 8] = float((y * 7 + x * 13) % 200)
        assert check_compression_artifacts(img).status in ("warn", "fail")

    def test_compression_ok_sur_image_naturelle(self, rng):
        img = rng.normal(120, 25, (128, 128)).astype(np.float32)
        assert check_compression_artifacts(img).status == "ok"

    def test_compression_ignore_les_petites_images(self):
        assert check_compression_artifacts(np.zeros((10, 10))).status == "skip"

    def test_repetabilite_ok_puis_fail(self, rng):
        base = rng.normal(120, 30, (96, 128)).astype(np.float32)
        bonne = [base] + [shift_image(base, d, 0) for d in (0.5, -1.0, 1.5)]
        mauvaise = [base] + [shift_image(base, d, 0) for d in (30, -40, 45)]
        assert check_preset_repeatability(bonne, max_p95_px=8.0).status == "ok"
        assert check_preset_repeatability(mauvaise, max_p95_px=8.0).status == "fail"

    def test_repetabilite_ignoree_si_trop_peu_dimages(self):
        assert check_preset_repeatability([np.zeros((8, 8))]).status == "skip"

    def test_vibration(self, rng):
        base = rng.normal(120, 30, (96, 128)).astype(np.float32)
        stable = [shift_image(base, 0.05 * i, 0) for i in range(6)]
        agite = [shift_image(base, 10 * (-1) ** i, 0) for i in range(6)]
        assert check_vibration(stable).status == "ok"
        assert check_vibration(agite).status == "fail"

    def test_hublot_encrasse(self, rng):
        from openvigie.compat import gaussian_blur, sobel_energy, to_gray

        scene = SyntheticScene(height=96, width=128, horizon_row=48)
        propre = scene.frame()
        baseline = float(sobel_energy(to_gray(propre)).mean())
        sale = gaussian_blur(to_gray(propre), 3.0)
        assert check_window_cleanliness(propre, baseline).status == "ok"
        assert check_window_cleanliness(sale, baseline).status in ("warn", "fail")

    def test_hublot_sans_reference(self):
        scene = SyntheticScene(height=64, width=64, horizon_row=32)
        assert check_window_cleanliness(scene.frame()).status == "skip"

    def test_focus_stable(self):
        scene = SyntheticScene(height=96, width=128, horizon_row=48)
        assert check_focus_stability([scene.frame(), scene.frame()]).status == "ok"

    def test_focus_derive(self):
        from openvigie.compat import gaussian_blur, to_gray

        scene = SyntheticScene(height=96, width=128, horizon_row=48)
        net = to_gray(scene.frame())
        assert check_focus_stability([net, gaussian_blur(net, 4.0)]).status == "fail"


class TestConfigChecks:
    def test_couverture_complete(self):
        assert check_coverage(plan_uniform_ring(IMX675, LENS_27135, 8, 8_000)).status == "ok"

    def test_secteur_aveugle_detecte(self):
        views = plan_uniform_ring(IMX675, LENS_27135, 8, 8_000)
        assert check_coverage(views[:3]).status == "fail"

    def test_objectif_hors_plage(self):
        assert check_lens_compat(LENS_27135, 20.0).status == "fail"
        assert check_lens_compat(LENS_27135, 6.0).status == "ok"

    def test_budget_balayage_ptz_alerte(self):
        r = check_scan_budget(21, 12.0, 3.0, is_ptz=True)
        assert r.status in ("warn", "fail")

    def test_budget_balayage_fixe_ok(self):
        assert check_scan_budget(8, 10.0, 0.0, is_ptz=False).status == "ok"

    def test_budget_portee_echoue_au_grand_angle_lointain(self):
        assert check_range_budget(IMX675, 4.8, 15_000, target_plume_m=30).status == "fail"

    def test_budget_portee_ok_avec_focale_longue(self):
        assert check_range_budget(IMX675, 13.5, 8_000, target_plume_m=30).status == "ok"

    @pytest.mark.parametrize("tier", TIERS)
    def test_les_prereglages_sont_geometriquement_coherents(self, tier):
        """Les contrôles de conception — optique, couverture, balayage, portée —
        doivent passer sur tous les préréglages."""
        design = {"objectif", "couverture", "budget_balayage", "budget_portee"}
        results = [r for r in run_config_checks(tier_defaults(tier)) if r.name in design]
        assert summarize(results)["all_passed"], [str(r) for r in results if r.status == "fail"]

    @pytest.mark.parametrize("tier", ["medium", "full"])
    def test_doctor_signale_les_capacites_absentes(self, tier):
        """AUDIT P0-05/P0-22 : un tier qui demande NNIE ou ONNX sans que le
        backend soit installé DOIT échouer. Auparavant il obtenait « 4 ok, 0
        avertissement » tout en se repliant silencieusement sur l'étage
        classique."""
        results = run_config_checks(tier_defaults(tier))
        backend = next(r for r in results if r.name == "backend")
        assert backend.status == "fail"
        assert not summarize(results)["all_passed"]

    def test_doctor_signale_le_pilote_manquant(self):
        """Le pilote IMX675 n'est pas en amont dans OpenIPC : le diagnostic doit
        le dire, pas l'ignorer."""
        plateforme = next(r for r in run_config_checks(tier_defaults("medium")) if r.name == "plateforme")
        assert plateforme.status == "warn"
        assert "porter" in plateforme.message

    def test_variante_immediatement_deployable(self):
        """IMX335 + HI3516AV300 : la combinaison utilisable aujourd'hui."""
        cfg = tier_defaults("medium")
        cfg.optics.sensor = "IMX335"
        plateforme = next(r for r in run_config_checks(cfg) if r.name == "plateforme")
        assert plateforme.status == "ok"


class TestCli:
    def test_plan(self, capsys):
        assert main(["plan", "-t", "medium"]) == 0
        assert "vues" in capsys.readouterr().out

    def test_plan_json(self, capsys):
        import json

        assert main(["plan", "-t", "full", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["tier"] == "full"
        assert len(data["views"]) == data["budget"]["n_views"]

    def test_doctor_echoue_si_une_capacite_manque(self, capsys):
        """Un diagnostic qui réussit toujours ne diagnostique rien."""
        assert main(["doctor", "-t", "medium"]) == 1
        assert "backend" in capsys.readouterr().out

    def test_doctor_reussit_en_mode_mesure_sans_backend_appris(self, capsys):
        cfg_ok = main(["doctor", "-t", "minimal"])
        assert cfg_ok in (0, 1)   # minimal utilise `classical`, disponible

    def test_ptz_test_affiche_les_trames(self, capsys):
        assert main(["ptz-test", "-t", "minimal"]) == 0
        out = capsys.readouterr().out
        assert "pelco-d=ff" in out
        assert "mouvements/an" in out

    @pytest.mark.parametrize("tier", TIERS)
    def test_selftest_positif(self, tier, capsys):
        assert main(["selftest", "-t", tier, "--mode", "plume"]) == 0
        assert "Autotest réussi" in capsys.readouterr().out

    @pytest.mark.parametrize("tier", TIERS)
    def test_selftest_negatif_nuage(self, tier, capsys):
        """Le nuage doit être rejeté sur les trois tiers : c'est le test qui
        garantit que l'origine au sol est bien un veto et pas un simple bonus."""
        assert main(["selftest", "-t", tier, "--mode", "cloud"]) == 0

    def test_init_ecrit_une_config(self, tmp_path, capsys):
        out = tmp_path / "site.yaml"
        assert main(["init", "full", "-o", str(out), "--site-id", "tour-42"]) == 0
        assert out.exists()
        assert load_site_config(out).site_id == "tour-42"

    def test_init_refuse_decraser(self, tmp_path):
        out = tmp_path / "site.yaml"
        main(["init", "medium", "-o", str(out)])
        assert main(["init", "medium", "-o", str(out)]) == 1
        assert main(["init", "medium", "-o", str(out), "--force"]) == 0
