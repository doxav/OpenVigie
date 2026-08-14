"""Tests des issues #1 (secteurs utiles) et #2 (relevé d'installation).

Les critères d'acceptation de l'issue #1 sont repris un par un, dans l'ordre,
pour qu'on puisse vérifier ce qui est réellement couvert.
"""

from __future__ import annotations

import json

import pytest

from openvigie.cli import main
from openvigie.config import SectorConfig, site_config_from_dict, tier_defaults
from openvigie.geometry import IMX675, LENS_27135
from openvigie.modules import (
    CostModel,
    Sector,
    compare_architectures,
    evaluate_fixed_ring,
    evaluate_ptz_sector,
    evaluate_wide_plus_ptz,
    plan_sector_views,
    recommend,
    sectors_from_viewshed,
    total_span_deg,
)
from openvigie.survey import (
    MOUNTING_AZIMUTH_SIGMA,
    InstallationSurvey,
    SurveyError,
    calibrate_from_survey,
    check_survey,
)


def survey(**kw) -> InstallationSurvey:
    base = {
        "view_id": "V00", "latitude": 44.0, "longitude": 3.0,
        "ground_altitude_m": 500.0, "camera_height_m": 40.0,
        "azimuth_magnetic_deg": 85.0, "magnetic_declination_deg": 2.1,
        "tilt_deg": 1.4, "roll_deg": 0.5,
    }
    base.update(kw)
    return InstallationSurvey(**base)


# =========================================================================== #
# Issue #1 — secteurs angulaires utiles
# =========================================================================== #
class TestSecteurs:
    def test_ouverture_simple(self):
        assert Sector("s", 170, 260, 8000).span_deg == pytest.approx(90.0)

    def test_franchissement_du_nord(self):
        """Un secteur 300° → 70° traverse le nord : 130°, pas 230°."""
        assert Sector("s", 300, 70, 8000).span_deg == pytest.approx(130.0)

    def test_secteur_complet(self):
        assert Sector("s", 0, 0, 8000).span_deg == pytest.approx(360.0)

    def test_centre(self):
        assert Sector("s", 300, 70, 8000).center_deg == pytest.approx(5.0)

    def test_appartenance(self):
        s = Sector("s", 300, 70, 8000)
        assert s.contains(350.0) and s.contains(10.0)
        assert not s.contains(180.0)

    def test_portee_nulle_refusee(self):
        with pytest.raises(ValueError, match="portée"):
            Sector("s", 0, 90, 0)

    def test_priorite_nulle_refusee(self):
        with pytest.raises(ValueError, match="priorité"):
            Sector("s", 0, 90, 8000, priority=0.0)

    def test_ouverture_cumulee_fusionne_les_recouvrements(self):
        """Deux secteurs qui se chevauchent ne comptent qu'une fois."""
        a = Sector("a", 0, 100, 8000)
        b = Sector("b", 50, 150, 8000)
        assert total_span_deg([a, b]) == pytest.approx(150.0, abs=1.0)

    def test_ouverture_cumulee_disjoints(self):
        a = Sector("a", 0, 90, 8000)
        b = Sector("b", 180, 270, 8000)
        assert total_span_deg([a, b]) == pytest.approx(180.0, abs=1.0)

    def test_ouverture_cumulee_ne_compte_pas_les_bornes_deux_fois(self):
        assert total_span_deg(
            [Sector("s", 170, 310, 8000)]
        ) == pytest.approx(140.0)

    def test_ouverture_cumulee_vide(self):
        assert total_span_deg([]) == 0.0


class TestSecteursDepuisViewshed:
    def test_directions_masquees_exclues(self):
        """Le cœur de l'issue #1 : cesser de dépenser des caméras sur ce que le
        relief masque."""
        ranges = {0.0: 10_000, 45.0: 500, 90.0: 400, 135.0: 9_000, 180.0: 11_000}
        sectors = sectors_from_viewshed(ranges, min_useful_range_m=2_000)
        span = total_span_deg(sectors)
        assert span < 360.0
        for s in sectors:
            assert not (s.contains(45.0) and s.contains(90.0))

    def test_tout_masque_ne_donne_aucun_secteur(self):
        ranges = dict.fromkeys(range(0, 360, 45), 300.0)
        assert sectors_from_viewshed(ranges, min_useful_range_m=2_000) == []

    def test_tout_visible_donne_une_couverture_quasi_complete(self):
        ranges = {float(az): 10_000.0 for az in range(0, 360, 10)}
        assert total_span_deg(sectors_from_viewshed(ranges)) >= 350.0

    def test_viewshed_vide(self):
        assert sectors_from_viewshed({}) == []

    def test_portee_du_secteur_est_la_plus_contraignante(self):
        ranges = {0.0: 10_000, 10.0: 4_000, 20.0: 9_000}
        sectors = sectors_from_viewshed(ranges, min_useful_range_m=2_000)
        assert sectors[0].max_range_m == pytest.approx(4_000)


# --------------------------------------------------------------------------- #
class TestCriteresAcceptationIssue1:
    """Les huit critères d'acceptation de l'issue #1, dans l'ordre."""

    def _sectors(self):
        return [Sector("sud", 170, 260, 8_000), Sector("ouest", 260, 310, 5_000)]

    def test_1_pas_de_couverture_360_implicite(self):
        """Une configuration sans secteur déclaré garde le comportement
        historique ; une configuration avec secteurs ne couvre qu'eux."""
        assert total_span_deg(tier_defaults("medium").sector_list()) == pytest.approx(360.0)
        cfg = site_config_from_dict({
            "site_id": "t", "tier": "medium",
            "sectors": [{"name": "sud", "start_deg": 170, "end_deg": 260, "max_range_m": 8000}],
        })
        assert total_span_deg(cfg.sector_list()) == pytest.approx(90.0, abs=1.0)

    def test_2_plusieurs_secteurs_definissables(self):
        cfg = site_config_from_dict({
            "site_id": "t", "tier": "medium",
            "sectors": [
                {"name": "A", "start_deg": 210, "end_deg": 310, "max_range_m": 8000},
                {"name": "B", "start_deg": 300, "end_deg": 70, "max_range_m": 6000},
                {"name": "C", "start_deg": 60, "end_deg": 150, "max_range_m": 4000},
            ],
        })
        assert len(cfg.sector_list()) == 3

    def test_3_couverture_dun_module_ptz_evaluee(self):
        e = evaluate_ptz_sector(self._sectors(), IMX675, LENS_27135)
        assert e.n_ptz == 1 and e.n_presets >= 1
        assert e.covers_all

    def test_4_temps_de_revisite_calcule(self):
        e = evaluate_ptz_sector(self._sectors(), IMX675, LENS_27135, dwell_s=15.0, settle_s=4.0)
        assert e.revisit_s == pytest.approx(e.n_presets * 19.0)
        assert e.mean_latency_s == pytest.approx(e.revisit_s / 2)

    def test_5_mouvements_ptz_calcules(self):
        e = evaluate_ptz_sector(self._sectors(), IMX675, LENS_27135)
        assert e.ptz_moves_per_year > 0
        # cohérence : positions × (secondes par an / cycle)
        expected = e.n_presets * (365.25 * 24 * 3600) / e.revisit_s
        assert e.ptz_moves_per_year == pytest.approx(expected, rel=1e-6)

    def test_6_module_avec_camera_grand_angle_optionnelle(self):
        e = evaluate_wide_plus_ptz(self._sectors(), IMX675, LENS_27135)
        assert e.n_fixed_cameras >= 1 and e.n_ptz == 1

    def test_7_plusieurs_modules_combinables(self):
        """Doubler l'ouverture à couvrir doit augmenter le nombre de positions."""
        petit = evaluate_ptz_sector([Sector("a", 0, 60, 8000)], IMX675, LENS_27135)
        grand = evaluate_ptz_sector([Sector("a", 0, 180, 8000)], IMX675, LENS_27135)
        assert grand.n_presets > petit.n_presets

    def test_8_comparaison_avec_lanneau_fixe(self):
        evaluations = compare_architectures(self._sectors(), IMX675, LENS_27135)
        assert len(evaluations) == 3
        noms = {e.name for e in evaluations}
        assert "Anneau fixe + PTZ de confirmation" in noms
        assert "Caméra zoom + tête PTZ par secteur" in noms
        assert "Grand-angle + PTZ à la demande" in noms


class TestArbitragesArchitecturaux:
    def test_lanneau_fixe_na_aucune_usure_ni_latence(self):
        e = evaluate_fixed_ring([Sector("s", 170, 260, 8000)], IMX675, LENS_27135)
        assert e.ptz_moves_per_year == 0.0
        assert e.mean_latency_s == 0.0

    def test_restreindre_les_secteurs_rend_le_balayage_ptz_viable(self):
        """Le résultat qui justifie l'issue #1 : à 360° une ronde PTZ est
        inexploitable ; sur 140° utiles elle redevient raisonnable."""
        tour = evaluate_ptz_sector([Sector("tour", 0, 360, 8000)], IMX675, LENS_27135)
        secteur = evaluate_ptz_sector([Sector("s", 170, 310, 8000)], IMX675, LENS_27135)
        assert tour.n_presets > 2 * secteur.n_presets
        assert secteur.revisit_s < tour.revisit_s / 2

    def test_le_grand_angle_limite_la_portee_pas_la_ptz(self):
        """Erreur d'intuition fréquente : ajouter une PTZ zoom ne fait pas voir
        plus loin, puisqu'on ne l'envoie que sur ce que le grand-angle a vu."""
        sectors = [Sector("s", 170, 260, 12_000)]
        wide = evaluate_wide_plus_ptz(sectors, IMX675, LENS_27135)
        fixe = evaluate_fixed_ring(sectors, IMX675, LENS_27135)
        assert wide.detection_range_m < fixe.detection_range_m

    def test_plan_sectoriel_8km_a_des_hypotheses_coherentes(self):
        """À 8 km / panache 30 m : 90° exige 3 vues, 140° en exige 4."""
        v90 = plan_sector_views([Sector("s", 0, 90, 8_000)], IMX675, LENS_27135)
        v140 = plan_sector_views([Sector("s", 0, 140, 8_000)], IMX675, LENS_27135)
        assert len(v90) == 3
        assert len(v140) == 4
        assert v140[0].focal_mm == pytest.approx(6.4, abs=0.05)

    def test_le_grand_angle_effondre_lusure_ptz(self):
        sectors = [Sector("s", 170, 310, 8_000)]
        balayage = evaluate_ptz_sector(sectors, IMX675, LENS_27135)
        demande = evaluate_wide_plus_ptz(sectors, IMX675, LENS_27135, candidates_per_day=12)
        assert demande.ptz_moves_per_year < balayage.ptz_moves_per_year / 100

    def test_usure_excessive_signalee(self):
        e = evaluate_ptz_sector([Sector("s", 0, 360, 8000)], IMX675, LENS_27135)
        assert any("mouvements/an" in n for n in e.notes)

    def test_objectif_insuffisant_signale(self):
        """Un objectif limité ne peut pas atteindre n'importe quelle portée :
        il faut le dire plutôt que de laisser croire à une couverture."""
        e = evaluate_fixed_ring([Sector("s", 0, 90, 30_000)], IMX675, LENS_27135)
        assert any("objectif limité" in n for n in e.notes)

    def test_focale_plus_longue_pour_secteur_plus_lointain(self):
        proche = evaluate_fixed_ring([Sector("s", 0, 90, 3_000)], IMX675, LENS_27135)
        loin = evaluate_fixed_ring([Sector("s", 0, 90, 10_000)], IMX675, LENS_27135)
        assert loin.focal_mm > proche.focal_mm
        assert loin.n_fixed_cameras >= proche.n_fixed_cameras

    def test_secteurs_prioritaires_ajoutent_des_visites(self):
        normal = evaluate_ptz_sector([Sector("a", 0, 120, 8000)], IMX675, LENS_27135)
        prio = evaluate_ptz_sector([Sector("a", 0, 120, 8000, priority=2.0)], IMX675, LENS_27135)
        assert prio.n_presets > normal.n_presets

    def test_aucun_secteur_refuse(self):
        for fn in (evaluate_fixed_ring, evaluate_ptz_sector, evaluate_wide_plus_ptz):
            with pytest.raises(ValueError, match="aucun secteur"):
                fn([], IMX675, LENS_27135)

    def test_recommandation_ecarte_la_ronde_ptz_usee(self):
        evaluations = compare_architectures([Sector("s", 170, 310, 8_000)], IMX675, LENS_27135)
        r = recommend(evaluations)
        assert "Caméra zoom + tête PTZ par secteur" not in r
        assert "PTZ de confirmation" in r

    def test_recommandation_motivee(self):
        r = recommend(compare_architectures([Sector("s", 170, 260, 8000)], IMX675, LENS_27135))
        assert "USD" in r

    def test_recommandation_impossible_est_dite(self):
        """Une portée hors d'atteinte de l'objectif doit être dite, pas
        contournée par une architecture qui couvre l'angle sans rien voir."""
        evaluations = compare_architectures([Sector("s", 0, 360, 30_000)], IMX675, LENS_27135)
        r = recommend(evaluations)
        assert "Aucune architecture" in r
        assert "portée" in r

    def test_couvrir_langle_ne_suffit_pas_a_etre_viable(self):
        """Défaut trouvé en écrivant les tests : une architecture pouvait être
        déclarée couvrante en balayant tout l'angle demandé sans jamais voir
        assez loin pour y détecter quoi que ce soit."""
        e = evaluate_fixed_ring([Sector("s", 0, 180, 30_000)], IMX675, LENS_27135)
        assert e.covers_all          # l'angle est bien couvert
        assert not e.meets_range     # mais la portée ne suit pas
        assert not e.is_viable

    def test_cout_croit_avec_le_nombre_de_cameras(self):
        petit = evaluate_fixed_ring([Sector("s", 0, 60, 5000)], IMX675, LENS_27135, CostModel())
        grand = evaluate_fixed_ring([Sector("s", 0, 300, 5000)], IMX675, LENS_27135, CostModel())
        assert grand.cost_usd > petit.cost_usd


# =========================================================================== #
# Issue #2 — relevé d'installation
# =========================================================================== #
class TestReleveInstallation:
    def test_declinaison_obligatoire(self):
        """Un smartphone donne le nord magnétique : sans conversion, biais
        systématique de 1 à 3° en France."""
        with pytest.raises(SurveyError, match="déclinaison"):
            InstallationSurvey(
                view_id="V", latitude=44.0, longitude=3.0, ground_altitude_m=500.0,
                camera_height_m=40.0, azimuth_magnetic_deg=85.0,
            )

    def test_declinaison_appliquee(self):
        assert survey(azimuth_magnetic_deg=85.0, magnetic_declination_deg=2.1) \
            .azimuth_true_deg == pytest.approx(87.1)

    def test_declinaison_negative(self):
        assert survey(azimuth_magnetic_deg=5.0, magnetic_declination_deg=-8.0) \
            .azimuth_true_deg == pytest.approx(357.0)

    def test_altitude_camera(self):
        assert survey().camera_altitude_m == pytest.approx(540.0)

    def test_convention_de_signe_tilt_pitch(self):
        """La configuration exprime un tilt positif vers le bas, la pose un
        pitch positif vers le haut — erreur de signe classique."""
        assert survey(tilt_deg=1.4).pitch_deg == pytest.approx(-1.4)

    def test_montage_inconnu_refuse(self):
        with pytest.raises(SurveyError, match="montage"):
            survey(mounting="drone")

    def test_coordonnees_invalides(self):
        with pytest.raises(SurveyError):
            survey(latitude=120.0)

    def test_incertitude_depend_du_montage(self):
        """L'apport principal du relevé n'est pas l'azimut mais l'assiette."""
        acier = survey(mounting="steel_tower")
        degage = survey(mounting="open")
        releve = survey(mounting="surveyed")
        assert acier.azimuth_sigma_deg > degage.azimuth_sigma_deg > releve.azimuth_sigma_deg
        # l'assiette, elle, ne dépend pas du montage : la gravité n'est pas perturbée
        assert acier.tilt_sigma_deg == degage.tilt_sigma_deg == releve.tilt_sigma_deg

    def test_incertitude_explicite_prioritaire(self):
        assert survey(mounting="steel_tower", azimuth_sigma_deg=3.0).azimuth_sigma_deg == 3.0

    def test_tous_les_montages_ont_une_incertitude(self):
        for m in MOUNTING_AZIMUTH_SIGMA:
            assert survey(mounting=m).azimuth_sigma_deg > 0


class TestPoseEtFenetre:
    def test_pose_derivee(self):
        p = survey().to_pose(IMX675, 6.25, 1296, 972)
        assert p.yaw_deg == pytest.approx(87.1)
        assert p.pitch_deg == pytest.approx(-1.4)
        assert p.roll_deg == pytest.approx(0.5)

    def test_site_derive(self):
        s = survey().to_site()
        assert s.altitude_m == 500.0 and s.height_m == 40.0

    def test_fenetre_asymetrique(self):
        """Le fait marquant : sur un pylône acier, la contrainte verticale est
        environ trente fois plus serrée que l'horizontale."""
        axes = survey(mounting="steel_tower").gate_axes_px(IMX675, 6.25, 1296)
        assert axes["horizontal_px"] > 20 * axes["vertical_px"]

    def test_fenetre_bornee_a_la_diagonale(self):
        """Une incertitude d'azimut énorme ne doit pas produire une fenêtre
        absurde : au-delà de l'image, elle n'exprime plus aucune contrainte."""
        g = survey(azimuth_sigma_deg=50.0).gate_px(IMX675, 6.25, 1296)
        assert g <= (1296**2 + 972**2) ** 0.5 + 1

    def test_meilleur_releve_donne_fenetre_plus_serree(self):
        assert survey(mounting="surveyed").gate_px(IMX675, 6.25, 1296) \
            < survey(mounting="steel_tower").gate_px(IMX675, 6.25, 1296)

    def test_prior_sigma(self):
        p = survey().prior_sigma()
        assert set(p) == {"yaw_deg", "pitch_deg", "roll_deg"}
        assert p["pitch_deg"] < p["yaw_deg"]


class TestControlesReleve:
    def test_releve_sain(self):
        assert check_survey(survey()) == []

    def test_altitude_nulle_signalee(self):
        assert any("altitude" in p for p in check_survey(survey(ground_altitude_m=0.0)))

    def test_declinaison_aberrante(self):
        assert any("déclinaison" in p for p in check_survey(survey(magnetic_declination_deg=45.0)))

    def test_assiette_aberrante(self):
        assert any("assiette" in p for p in check_survey(survey(tilt_deg=70.0)))

    def test_roulis_aberrant(self):
        assert any("roulis" in p for p in check_survey(survey(roll_deg=30.0)))

    def test_hauteur_aberrante(self):
        assert any("hauteur" in p for p in check_survey(survey(camera_height_m=500.0)))

    def test_azimut_trop_optimiste_sur_pylone(self):
        problems = check_survey(survey(mounting="steel_tower", azimuth_sigma_deg=1.0))
        assert any("optimiste" in p for p in problems)

    def test_incoherence_avec_le_mnt(self):
        from openvigie.dem import synthetic_dem

        problems = check_survey(survey(ground_altitude_m=50.0), dem=synthetic_dem())
        assert any("MNT" in p for p in problems)

    def test_hors_emprise_du_mnt(self):
        from openvigie.dem import synthetic_dem

        problems = check_survey(survey(latitude=10.0, longitude=10.0), dem=synthetic_dem())
        assert any("emprise" in p for p in problems)


class TestPersistanceReleve:
    def test_aller_retour_disque(self, tmp_path):
        s = survey(notes="pylône TDF, secteur sud")
        p = tmp_path / "survey.json"
        s.save(p)
        loaded = InstallationSurvey.load(p)
        assert loaded.azimuth_true_deg == pytest.approx(s.azimuth_true_deg)
        assert loaded.notes == "pylône TDF, secteur sud"

    def test_champs_derives_exportes(self):
        d = survey().as_dict()
        assert "azimuth_true_deg" in d and "camera_altitude_m" in d


class TestEtalonnageAmorce:
    def _scenario(self):
        from openvigie.calibration import (
            CameraPose,
            Site,
            synthesize_observations,
            synthesize_traffic,
        )

        site = Site(44.0, 3.0, 500.0, 40.0)
        true_pose = CameraPose(yaw_deg=87.3, pitch_deg=-1.42, roll_deg=0.6, focal_mm=6.25,
                               sensor=IMX675, width_px=1296, height_px=972)
        tracks = synthesize_traffic(site, n_aircraft=25, seed=3)
        obs = synthesize_observations(site, tracks, true_pose, noise_px=1.5,
                                      n_outliers=5, seed=11)
        return tracks, obs

    def test_le_releve_amorce_letalonnage(self):
        tracks, obs = self._scenario()
        result, boot = calibrate_from_survey(
            survey(), obs, tracks, IMX675, 6.25, 1296, 972,
            fit=("yaw_deg", "pitch_deg", "roll_deg", "focal_mm"),
        )
        assert result.quality in ("good", "usable")
        assert abs(result.pose.yaw_deg - 87.3) < 0.05
        assert boot.consistent

    def test_correction_dazimut_attendue_et_faible(self):
        """Le relevé donne 87,1° ; la vérité est 87,3°. La correction doit être
        petite et compatible avec l'incertitude annoncée."""
        tracks, obs = self._scenario()
        _, boot = calibrate_from_survey(
            survey(), obs, tracks, IMX675, 6.25, 1296, 972,
            fit=("yaw_deg", "pitch_deg", "roll_deg", "focal_mm"),
        )
        assert abs(boot.yaw_correction_deg) < 1.0

    def test_declinaison_oubliee_est_detectee(self):
        """Le cas d'erreur que le contrôle doit attraper : une déclinaison
        saisie à zéro alors que le site est en France."""
        tracks, obs = self._scenario()
        faux = survey(magnetic_declination_deg=0.0, azimuth_magnetic_deg=60.0,
                      mounting="surveyed")
        _, boot = calibrate_from_survey(
            faux, obs, tracks, IMX675, 6.25, 1296, 972,
            fit=("yaw_deg", "pitch_deg", "roll_deg", "focal_mm"),
        )
        assert not boot.consistent
        assert any("déclinaison" in w for w in boot.warnings)

    def test_erreur_de_signe_du_tilt_detectee(self):
        """L'accéléromètre ne se trompe pas de plusieurs degrés : un tel écart
        trahit une erreur de saisie, typiquement le signe du tilt."""
        tracks, obs = self._scenario()
        faux = survey(tilt_deg=-1.4)  # signe inversé
        _, boot = calibrate_from_survey(
            faux, obs, tracks, IMX675, 6.25, 1296, 972,
            fit=("yaw_deg", "pitch_deg", "roll_deg", "focal_mm"),
        )
        assert not boot.consistent
        assert any("assiette" in w for w in boot.warnings)

    def test_serialisation_du_bootstrap(self):
        tracks, obs = self._scenario()
        _, boot = calibrate_from_survey(
            survey(), obs, tracks, IMX675, 6.25, 1296, 972,
            fit=("yaw_deg", "pitch_deg", "roll_deg", "focal_mm"),
        )
        d = boot.as_dict()
        assert "yaw_correction_deg" in d and "gate_px" in d


# =========================================================================== #
class TestCli:
    def test_sectors_depuis_configuration(self, capsys):
        assert main(["sectors", "-t", "medium"]) == 0
        out = capsys.readouterr().out
        assert "Secteurs utiles" in out and "Recommandation" in out

    def test_sectors_depuis_viewshed(self, capsys):
        assert main(["sectors", "-t", "medium", "--from-viewshed", "x", "--synthetic"]) == 0
        out = capsys.readouterr().out
        assert "% d'un tour d'horizon" in out

    def test_sectors_json(self, capsys):
        assert main(["sectors", "-t", "medium", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["architectures"]) == 3
        assert "recommendation" in data

    def test_sectors_relief_totalement_bouche(self, capsys):
        rc = main(["sectors", "-t", "medium", "--from-viewshed", "x", "--synthetic",
                   "--min-range", "999999"])
        assert rc == 1
        assert "Aucun secteur utile" in capsys.readouterr().err

    def test_survey_declinaison_requise(self, capsys):
        assert main(["survey", "-t", "medium"]) == 2
        assert "MAGNÉTIQUE" in capsys.readouterr().err

    def test_survey_nominal(self, capsys):
        rc = main(["survey", "-t", "medium", "--declination", "2.1",
                   "--altitude", "500", "--azimuth", "85", "--tilt", "1.4"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "azimut vrai" in out and "facteur" in out

    def test_survey_signale_altitude_nulle(self, capsys):
        rc = main(["survey", "-t", "medium", "--declination", "2.1", "--altitude", "0"])
        assert rc == 1
        assert "altitude" in capsys.readouterr().out

    def test_survey_json(self, capsys):
        rc = main(["survey", "-t", "medium", "--declination", "2.1",
                   "--altitude", "500", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["pose"]["yaw_deg"] == pytest.approx(92.1, abs=0.01)
        assert data["gate_axes_px"]["vertical_px"] < data["gate_axes_px"]["horizontal_px"]

    def test_survey_depuis_fichier(self, tmp_path, capsys):
        p = tmp_path / "s.json"
        survey().save(p)
        assert main(["survey", "-t", "medium", "--file", str(p)]) == 0


class TestSectorConfig:
    def test_conversion(self):
        s = SectorConfig(name="sud", start_deg=170, end_deg=260, max_range_m=8000).to_sector()
        assert s.span_deg == pytest.approx(90.0)

    def test_yaml_accepte_les_secteurs(self, tmp_path):
        from openvigie.config import load_site_config

        p = tmp_path / "site.yaml"
        p.write_text(
            "site_id: t1\ntier: medium\nsectors:\n"
            "  - {name: sud, start_deg: 170, end_deg: 260, max_range_m: 8000}\n",
            encoding="utf-8",
        )
        assert len(load_site_config(p).sector_list()) == 1


# =========================================================================== #
# Harnais de validation d'un portage capteur (docs/PORTAGE_IMX675.md)
# =========================================================================== #
class TestValidationPortageCapteur:
    """Le portage IMX675 ne peut pas être fait sans matériel ni documentation
    constructeur. Ce qui peut l'être — et qui est testé ici — c'est le harnais
    qui permettra à quelqu'un disposant d'une carte de vérifier son pilote."""

    def test_resolution_conforme(self):
        import numpy as np

        from openvigie.hwcheck import check_sensor_resolution

        r = check_sensor_resolution(np.zeros((IMX675.height_px, IMX675.width_px)), IMX675)
        assert r.status == "ok"

    def test_image_rognee_detectee(self):
        """Un pilote peut « marcher » tout en livrant un quart des pixels —
        et tout le budget de portée devient faux d'autant."""
        import numpy as np

        from openvigie.hwcheck import check_sensor_resolution

        r = check_sensor_resolution(np.zeros((972, 1296)), IMX675)
        assert r.status == "fail"
        assert "25%" in r.message

    def test_cadence_stable(self):
        from openvigie.hwcheck import check_frame_interval

        assert check_frame_interval([i / 25 for i in range(8)], 25.0).status == "ok"

    def test_cadence_incorrecte(self):
        from openvigie.hwcheck import check_frame_interval

        r = check_frame_interval([i / 8 for i in range(8)], 25.0)
        assert r.status == "fail"

    def test_gigue_signalee(self):
        from openvigie.hwcheck import check_frame_interval

        r = check_frame_interval([0, 0.02, 0.10, 0.12, 0.20, 0.22], 25.0)
        assert r.status in ("warn", "fail")

    def test_horodatages_non_monotones(self):
        from openvigie.hwcheck import check_frame_interval

        assert check_frame_interval([0, 0.04, 0.02, 0.10], 25.0).status == "fail"

    def test_trop_peu_dimages(self):
        from openvigie.hwcheck import check_frame_interval

        assert check_frame_interval([0, 0.04], 25.0).status == "skip"

    def test_champ_conforme(self):
        from openvigie.geometry import hfov_deg
        from openvigie.hwcheck import check_field_of_view

        assert check_field_of_view(hfov_deg(IMX675, 5.2), IMX675, 5.2).status == "ok"

    def test_champ_incoherent_signale_une_fiche_fausse(self):
        """Le contrôle le plus révélateur : il vérifie d'un coup le pas de
        pixel, la taille de matrice et la focale réelle."""
        from openvigie.hwcheck import check_field_of_view

        assert check_field_of_view(60.0, IMX675, 5.2, tolerance_ratio=0.02).status == "fail"

    def test_cli_simule(self, capsys):
        assert main(["sensor-validate", "-t", "medium", "--simulate"]) == 1
        out = capsys.readouterr().out
        assert "pilote IMX675 : porting" in out

    def test_cli_exige_une_cible(self, capsys):
        assert main(["sensor-validate", "-t", "medium"]) == 2

    def test_cli_json(self, capsys):
        main(["sensor-validate", "-t", "medium", "--simulate", "--json"])
        out = capsys.readouterr().out
        data = json.loads(out[out.index("["):])
        assert any(r["name"] == "capteur_resolution" for r in data)

    def test_le_statut_du_pilote_reste_honnete(self):
        """Tant que le portage n'est pas fait, l'outil doit le dire."""
        from openvigie.platform import board_readiness, sensor_driver_status

        assert sensor_driver_status("IMX675") == "porting"
        assert board_readiness("hi3516av300", "IMX675")["status"] == "porting_required"
        assert board_readiness("hi3516av300", "IMX335")["status"] == "ready"
