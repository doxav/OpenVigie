"""Tests de la couche matérielle OpenIPC.

Le projet ne doit pas être lié à une seule référence de module. Ces tests
vérifient que la même configuration reste exploitable sur les différentes
familles de SoC supportées par OpenIPC, et que le dimensionnement dégrade
proprement (repli sur l'étage classique) au lieu d'échouer quand le SoC n'a pas
de moteur neuronal.
"""

from __future__ import annotations

import pytest

from openvigie.cli import main
from openvigie.config import PlatformConfig, tier_defaults
from openvigie.geometry import SENSORS
from openvigie.platform import (
    MAJESTIC_DETECTION_PROFILE,
    MAJESTIC_WARNINGS,
    SENSOR_DRIVER_STATUS,
    SOC_MATRIX,
    OpenIpcCamera,
    PlatformInfo,
    board_readiness,
    compatibility_report,
    detect_platform,
    detection_profile_commands,
    get_capabilities,
    parse_ipctool,
    parse_os_release,
    select_backend,
    sensor_driver_status,
    supported_sensors,
    supported_socs,
)

IPCTOOL_SAMPLE = """
---
chipName: hi3516av300
chipVendor: HiSilicon
sensors:
  - vendor: Sony
    model: IMX335
    control:
      bus: 0
      type: i2c
"""

OS_RELEASE_SAMPLE = 'NAME="OpenIPC"\nVERSION="2.4.05.02"\nID=openipc\n'


class TestMatrix:
    def test_familles_couvertes(self):
        """Quatre familles de SoC : le projet n'est pas mono-fournisseur."""
        familles = {c.family.split("-")[0] for c in SOC_MATRIX.values()}
        assert {"hisilicon", "goke", "sigmastar", "ingenic"} <= familles

    def test_soc_inconnu_degrade_proprement(self):
        caps = get_capabilities("soc-du-futur")
        assert caps.soc == "unknown"
        assert not caps.openipc_supported
        assert caps.recommended_backend == "classical"

    def test_soc_none(self):
        assert get_capabilities(None).soc == "unknown"

    def test_casse_et_espaces_tolerees(self):
        assert get_capabilities("  HI3516AV300 ").soc == "hi3516av300"

    def test_seuls_les_socs_a_moteur_neuronal_annoncent_du_cnn_local(self):
        for soc, caps in SOC_MATRIX.items():
            assert caps.can_run_cnn_locally == (caps.accelerator in ("nnie", "npu")), soc

    def test_backend_conseille_coherent_avec_laccelerateur(self):
        for soc, caps in SOC_MATRIX.items():
            if caps.recommended_backend == "nnie":
                assert caps.accelerator == "nnie", soc

    def test_listes_publiques(self):
        assert "hi3516av300" in supported_socs()
        assert "IMX675" in supported_sensors()


class TestSensorDrivers:
    def test_starvis1_en_amont(self):
        for s in ("IMX307", "IMX327", "IMX335", "IMX415"):
            assert sensor_driver_status(s) == "upstream"

    def test_starvis2_a_porter(self):
        for s in ("IMX662", "IMX664", "IMX675", "IMX678", "IMX585"):
            assert sensor_driver_status(s) == "porting"

    def test_capteur_inconnu(self):
        assert sensor_driver_status("IMX999") == "unknown"

    def test_insensible_a_la_casse(self):
        assert sensor_driver_status("imx675") == "porting"

    def test_tous_les_capteurs_du_registre_ont_une_geometrie(self):
        """Un capteur listé sans spécification optique produirait un budget de
        portée impossible à calculer."""
        assert set(SENSOR_DRIVER_STATUS) <= set(SENSORS)


class TestReadiness:
    def test_soc_supporte_et_pilote_en_amont(self):
        r = board_readiness("gk7605v100", "IMX335")
        assert r["status"] == "ready"
        assert r["recommended_backend"] == "classical"

    def test_portage_requis_pour_starvis2(self):
        r = board_readiness("hi3516av300", "IMX675")
        assert r["status"] == "porting_required"
        assert r["soc_supported"] and r["sensor_driver"] == "porting"
        assert r["can_run_cnn_locally"]

    def test_soc_hors_openipc(self):
        assert board_readiness("rk3588", "IMX335")["status"] == "soc_unsupported"

    def test_ni_lun_ni_lautre(self):
        assert board_readiness("rk3588", "IMX999")["status"] == "unsupported"

    def test_rapport_signale_le_portage(self):
        report = compatibility_report("hi3516av300", "IMX675")
        assert any("portage" in n for n in report["notes"])

    def test_rapport_sans_portage(self):
        report = compatibility_report("ssc338q", "IMX335")
        assert not any("portage" in n for n in report["notes"])


class TestDetection:
    def test_parse_ipctool(self):
        soc, sensor = parse_ipctool(IPCTOOL_SAMPLE)
        assert soc == "hi3516av300"
        assert sensor == "IMX335"

    def test_parse_ipctool_vide(self):
        assert parse_ipctool("") == (None, None)

    def test_parse_os_release(self):
        is_openipc, version = parse_os_release(OS_RELEASE_SAMPLE)
        assert is_openipc and version == "2.4.05.02"

    def test_parse_os_release_autre_distro(self):
        assert parse_os_release('NAME="Ubuntu"\nVERSION="24.04"\n')[0] is False

    def test_detection_injectee(self):
        info = detect_platform(
            read_file=lambda p: OS_RELEASE_SAMPLE if p == "/etc/os-release" else None,
            run=lambda cmd, timeout=5.0: IPCTOOL_SAMPLE if cmd[0] == "ipctool" else None,
        )
        assert info.is_openipc
        assert info.soc == "hi3516av300"
        assert info.sensor == "IMX335"
        assert info.capabilities.accelerator == "nnie"

    def test_detection_hors_camera(self):
        info = detect_platform(read_file=lambda p: None, run=lambda cmd, timeout=5.0: None)
        assert not info.is_openipc
        assert info.soc is None
        assert info.capabilities.recommended_backend == "classical"

    def test_repli_cpuinfo(self):
        info = detect_platform(
            read_file=lambda p: "Hardware : ssc338q board" if p == "/proc/cpuinfo" else None,
            run=lambda cmd, timeout=5.0: None,
        )
        assert info.soc == "ssc338q"

    def test_serialisation(self):
        assert "capabilities" in detect_platform(
            read_file=lambda p: None, run=lambda cmd, timeout=5.0: None
        ).as_dict()


class TestBackendSelection:
    def _info(self, soc: str) -> PlatformInfo:
        return PlatformInfo(soc=soc, capabilities=get_capabilities(soc))

    def test_auto_suit_le_soc(self):
        backend, _ = select_backend(self._info("hi3516av300"), "auto")
        assert backend == "nnie"
        backend, _ = select_backend(self._info("ssc338q"), "auto")
        assert backend == "classical"

    def test_nnie_demande_sur_soc_sans_nnie_est_refuse(self):
        """Erreur de configuration classique quand on réutilise une config
        d'un site HiSilicon sur un site SigmaStar."""
        backend, why = select_backend(self._info("ssc338q"), "nnie")
        assert backend == "classical"
        assert "n'a pas de NNIE" in why

    def test_backend_externe_toujours_honore(self):
        backend, _ = select_backend(self._info("gk7605v100"), "onnx")
        assert backend == "onnx"

    def test_sans_demande_explicite(self):
        backend, why = select_backend(self._info("t31"), None)
        assert backend == "classical"
        assert "déduit" in why


class TestOpenIpcCamera:
    def test_urls(self):
        cam = OpenIpcCamera(host="192.168.1.64")
        assert cam.snapshot_url == "http://192.168.1.64:80/image.jpg"
        assert cam.rtsp_url == "rtsp://192.168.1.64/stream0"

    def test_port_personnalise(self):
        assert OpenIpcCamera(host="10.0.0.5", http_port=8080).snapshot_url.endswith(":8080/image.jpg")

    def test_profil_detection_couvre_la_qualite_snapshot(self):
        assert ".jpeg.qfactor" in MAJESTIC_DETECTION_PROFILE
        assert int(MAJESTIC_DETECTION_PROFILE[".jpeg.qfactor"][0]) >= 90

    def test_profil_desactive_losd(self):
        """L'incrustation d'horodatage crée un candidat permanent."""
        assert MAJESTIC_DETECTION_PROFILE[".osd.enabled"][0] == "false"

    def test_chaque_reglage_est_justifie(self):
        for key, (value, reason) in MAJESTIC_DETECTION_PROFILE.items():
            assert value and len(reason) > 20, key

    def test_avertissements_couvrent_le_debruitage_temporel(self):
        assert ".isp.3dnr" in MAJESTIC_WARNINGS
        assert "fumée fine" in MAJESTIC_WARNINGS[".isp.3dnr"]

    def test_commandes_generees(self):
        cmds = detection_profile_commands(OpenIpcCamera(host="10.0.0.5"))
        assert len(cmds) == len(MAJESTIC_DETECTION_PROFILE)
        assert all(c.startswith("ssh root@10.0.0.5 cli -s ") for c in cmds)


class TestConfigIntegration:
    def test_platform_par_defaut_est_openipc(self):
        assert PlatformConfig().firmware == "openipc"
        assert PlatformConfig().soc == "auto"

    def test_compute_invalide(self):
        with pytest.raises(ValueError):
            PlatformConfig(compute="cloud")

    def test_tiers_declarent_leur_plateforme(self):
        assert tier_defaults("medium").platform.compute == "onboard"
        assert tier_defaults("full").platform.compute == "external"
        assert tier_defaults("full").platform.external_device

    def test_readiness_depuis_la_config(self):
        assert tier_defaults("medium").readiness()["status"] == "porting_required"

    def test_readiness_en_auto(self):
        cfg = tier_defaults("medium")
        cfg.platform.soc = "auto"
        assert cfg.readiness()["status"] == "unknown"

    def test_variante_starvis1_immediatement_utilisable(self):
        """Le repli sans portage : même carte, capteur IMX335 en amont."""
        cfg = tier_defaults("medium")
        cfg.optics.sensor = "IMX335"
        assert cfg.readiness()["status"] == "ready"

    def test_config_yaml_accepte_le_bloc_platform(self, tmp_path):
        from openvigie.config import load_site_config

        p = tmp_path / "s.yaml"
        p.write_text(
            "site_id: t1\ntier: medium\nplatform:\n  soc: ssc338q\n  compute: onboard\n"
            "optics:\n  sensor: IMX335\n",
            encoding="utf-8",
        )
        cfg = load_site_config(p)
        assert cfg.platform.soc == "ssc338q"
        assert cfg.readiness()["status"] == "ready"


class TestCliHardware:
    def test_matrice(self, capsys):
        assert main(["hw", "--matrix"]) == 0
        out = capsys.readouterr().out
        assert "hi3516av300" in out and "ssc338q" in out and "IMX675" in out

    def test_evaluation_dune_combinaison(self, capsys):
        assert main(["hw", "--soc", "ssc338q", "--sensor", "IMX335"]) == 0
        assert "utilisable immédiatement" in capsys.readouterr().out

    def test_combinaison_non_supportee_renvoie_1(self):
        assert main(["hw", "--soc", "rk3588", "--sensor", "IMX999"]) == 1

    def test_json(self, capsys):
        import json

        assert main(["hw", "--soc", "hi3516av300", "--sensor", "IMX675", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "porting_required"

    def test_detection_locale_ne_plante_pas_hors_camera(self, capsys):
        assert main(["hw"]) == 0
        assert "Backend retenu" in capsys.readouterr().out

    def test_profil_majestic(self, capsys):
        assert main(["majestic", "--host", "10.0.0.5"]) == 0
        out = capsys.readouterr().out
        assert "image.jpg" in out
        assert "cli -s .jpeg.qfactor 90" in out
        assert "3dnr" in out
