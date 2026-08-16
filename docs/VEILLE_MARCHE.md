# Veille — Pyronear, contributions utiles et pistes de recherche

*État au 16 août 2026. Ce document privilégie la collaboration et les écarts
techniques démontrables. Les prix et caractéristiques matérielles restent à
revérifier avant achat.*

## 1. Décision

Après revue détaillée de l'écosystème Pyronear, **OpenVigie ne cherche plus à
construire une stack opérationnelle parallèle**. Pyronear couvre déjà les
caméras de points hauts, le PTZ, l'inférence edge, les séquences temporelles,
la localisation multi-caméras, les données/entraînement/évaluation, l'API et la
plateforme opérateur.

OpenVigie devient un **SDK/labo complémentaire, upstream-first** :

- mesurer les limites de la baseline Pyronear ;
- développer uniquement les briques manquantes qui montrent un gain ;
- proposer upstream les composants génériques ;
- conserver localement les expériences trop spécifiques ou non validées.

---

## 2. Caméras et PTZ Pyronear : le niveau réel

Le site Pyronear décrit des tours composées de **4/5 caméras haute résolution
et d'un micro-ordinateur**, avec capture périodique et analyse locale.

Références :
[Pyronear](https://pyronear.org/fr/) ·
[`pyro-engine`](https://github.com/pyronear/pyro-engine) ·
[`pyro-sys-setup`](https://github.com/pyronear/pyro-sys-setup).

| Matériel / interface | Capacités utiles | Prix indicatif | Support Pyronear |
|---|---|---:|---|
| **Reolink RLC-823S2** | 8 MP 3840×2160/25 fps, 1/2,8", **16× 5,3–86 mm**, PTZ 360°/90°, PoE, IP66 | **379,99 €** boutique Reolink FR au 16/08/2026 | support explicite dans les tables FOV et calibration PTZ |
| **Reolink RLC-823A 16X** | 8 MP, 16×, PTZ | ancien modèle, prix/disponibilité variables | support explicite, tables FOV/vitesse |
| **Reolink statique** | snapshot/RTSP/PoE selon modèle | selon modèle | provisionnement générique `static`; Pyronear n'impose pas un SKU unique public |
| **Linovision/Hikvision ISAPI** | snapshot, PTZ, focus/zoom, **lecture d'azimut matériel** | selon modèle | adaptateur natif |
| **RTSP / URL / REST** | intégration de caméras existantes | matériel existant | adaptateurs génériques |

Spécifications :
[RLC-823S2](https://reolink.com/fr/product/rlc-823s2/) ·
[RLC-823A 16X](https://reolink.com/us/product/rlc-823a-16x/).

### Contrôles déjà présents

Dans le code public inspecté, l'intégration Reolink Pyronear fournit :

- snapshot JPEG (`Snap`) ;
- PTZ, presets, mouvement temporisé ;
- zoom ;
- autofocus on/off ;
- focus manuel et recherche automatique du meilleur focus ;
- lecture du niveau zoom/focus ;
- configuration 4K / bitrate / fps / GOP via le script d'encodage ;
- tables FOV mesurées suivant le zoom ;
- calibration vitesses/biais pan/tilt.

La calibration PTZ Pyronear n'est donc pas superficielle : son outil mesure
automatiquement le déplacement par ORB, ajuste le modèle
`déplacement = vitesse × temps + biais`, calibre à plusieurs zooms et construit
la table FOV.

Sources :
[`reolink.py`](https://github.com/pyronear/pyro-engine/blob/develop/pyro_camera_api/pyro_camera_api/camera/adapters/reolink.py) ·
[`set_cam_encoding.py`](https://github.com/pyronear/pyro-engine/blob/develop/src/set_cam_encoding.py) ·
[PTZ calibration tools](https://github.com/pyronear/pyro-engine/blob/develop/tools/README.md).

### Réglages image avancés : manque logiciel limité, pas besoin d'OpenIPC par défaut

Le code Pyronear inspecté n'expose pas actuellement les réglages avancés
Reolink tels que exposition, backlight/dynamic-range, jour/nuit ou autres
paramètres ISP. **Ce n'est pas une incapacité de la caméra** : Reolink expose
ces réglages dans son client et son interface web, et documente CGI comme
interface d'automatisation/paramétrage.

Références :
[Advanced image settings](https://support.reolink.com/articles/4403930384025-How-to-Configure-Advanced-Image-Settings-on-Your-Reolink-Camera/) ·
[Exposure/backlight](https://support.reolink.com/articles/900003659266-How-to-Configure-Exposure-and-Backlight-Settings/) ·
[CGI/RTSP/ONVIF](https://support.reolink.com/articles/900000617826-Which-Reolink-Products-Support-CGI-RTSP-ONVIF/).

**Décision :** ne pas développer un port OpenIPC pour ce motif. D'abord mesurer
si un réglage ISP stable améliore réellement rappel/FP. Si oui, contribuer une
petite extension Reolink/provisioning à Pyronear.

---

## 3. Multi-caméras et localisation

`pyro_camera_api` gère statiques et PTZ. La boucle statique capture
périodiquement ; la boucle PTZ parcourt des poses et cède la priorité au live
opérateur. `pyro-api` groupe les détections en séquences, les valide
temporellement puis localise des alertes par recouvrement de cônes de plusieurs
caméras.

Sources :
[`patrol.py`](https://github.com/pyronear/pyro-engine/blob/develop/pyro_camera_api/pyro_camera_api/camera/patrol.py) ·
[`pyro-api`](https://github.com/pyronear/pyro-api).

**Conséquence :** « multi-camera », « PTZ », « calibration FOV » et
« triangulation/localisation » ne sont pas des différenciateurs suffisants
d'OpenVigie.

Une piste reste cependant utile : la chaîne automatique

`alerte localisée → choisir une autre caméra qui voit la zone → pointer/zoomer → reclassifier`

n'a pas été identifiée de bout en bout dans le code public inspecté.
OpenVigie possède déjà une primitive `MultiTowerCorrelator.confirmation_tasks`
qui sélectionne une tour selon visibilité, distance et angle de croisement.
C'est une bonne candidate de contribution, après validation avec l'équipe
Pyronear.

---

## Pyronear et OpenVigie

## 4. Calibration : ce qui existe déjà et ce qui reste à tester

Pyronear possède **deux calibrations complémentaires**.

### Calibration d'azimut absolu

`pyro-sys-setup/cam_calibration` :

1. balayage PTZ sur >360° ;
2. phase correlation pour mesurer le décalage réel entre vues ;
3. deux clics sur la zone de recouvrement pour obtenir `px/deg` ;
4. un clic sur un amer dont l'azimut est connu pour ancrer le nord ;
5. `calibration.csv` donnant l'azimut de chaque pose ;
6. SIFT + RANSAC/homographie pour estimer ensuite l'azimut d'une nouvelle
   image.

Source :
[`cam_calibration/README.md`](https://github.com/pyronear/pyro-sys-setup/blob/main/cam_calibration/README.md).

### Calibration PTZ / zoom

`pyro-engine/tools` calibre :

- vitesses et biais mécaniques pan/tilt ;
- micro-mouvements ;
- FOV horizontal/vertical selon le zoom ;
- click-to-move.

Source :
[`tools/README.md`](https://github.com/pyronear/pyro-engine/blob/develop/tools/README.md).

### Ce qu'OpenVigie ne doit pas dupliquer

À abandonner comme contribution autonome :

- calibration manuelle d'azimut par amer ;
- panorama 360° ;
- tables FOV/zoom ;
- calibration vitesse/biais PTZ.

### Extension qui peut encore avoir une valeur

OpenVigie possède un modèle de pose pinhole et un solveur ADS-B expérimental
capable d'estimer notamment yaw/pitch/roll/focale et des biais temporels/
altimétriques. La valeur potentielle n'est donc **pas** « calibrer l'azimut »,
déjà bien traité par Pyronear, mais :

- mesurer **tilt et roll absolus** ;
- vérifier/raffiner les intrinsics/focale ;
- détecter une **dérive lente de pose** après vent, maintenance ou mouvement ;
- propager cette erreur jusqu'à l'incertitude de localisation.

L'ADS-B reste une hypothèse de R&D : il doit être comparé à la calibration
Pyronear + amers fixes sur un vrai site avant toute proposition upstream.
L'issue [`pyro-engine #397`](https://github.com/pyronear/pyro-engine/issues/397)
montre un problème réel de précision sur de grands mouvements RLC-823A16, mais
cela ne prouve pas que l'ADS-B soit la bonne correction.

---

## 5. Contributions qui semblent réellement utiles

| Priorité | Contribution | Pourquoi |
|---:|---|---|
| **0** | **Baseline reproductible Pyronear** | avant toute invention : mesurer rappel/TTD par distance et visibilité, FP/jour, localisation, CPU/RAM, énergie, réseau, stabilité ISP/PTZ |
| **1** | **Petites fumées / haute résolution** | `pyro-predictor` travaille typiquement à `imgsz=1024`; tester candidate-first/tiling avant réduction peut préserver de petites fumées lointaines |
| **2** | **MNT/horizon par pixel au runtime** | apporte origine-sol, distance et visibilité terrain au-delà d'un simple cône |
| **3** | **Pose 3D + dérive + incertitude** | compléter, pas remplacer, les calibrations Pyronear existantes |
| **4** | **Localisation probabiliste** | ellipse/confidence issue de bbox + pose + MNT + angle de croisement |
| **5** | **Confirmation multi-camera/PTZ automatique** | transformer la localisation en levée de doute active |
| **6** | **Signaux physiques/contextuels** | croissance, ascendance, contraste, visibilité, vent comme validateurs additionnels |
| **7** | **Données/évaluation communes** | faux positifs terrain et scénarios de régression vers `pyro-dataset`, `pyro-eval`, `pyro-annotator` |
| **8** | **Optimisation globale avec OpenTrace** | rechercher automatiquement de meilleurs compromis de configuration/code à partir de feedbacks riches |

### Ce qui devient non prioritaire / à abandonner sauf preuve contraire

- port OpenIPC comme objectif en soi ;
- portage IMX675/STARVIS2 comme roadmap principale ;
- NNIE/calcul dans la caméra comme objectif par défaut ;
- nouvelle abstraction PTZ/Pelco concurrente ;
- API centrale, UI opérateur ou chaîne MLOps OpenVigie parallèles ;
- nouvelle calibration azimut/FOV/vitesse qui dupliquerait Pyronear.

Ces travaux peuvent rester dans le labo comme **options de recherche** si la
baseline met en évidence un besoin précis.

---

## 6. OpenTrace : optimisation riche, multi-objectifs et Pareto

La référence actuelle est
[`AgentOpt/OpenTrace`](https://github.com/AgentOpt/OpenTrace), active en 2026.
Le dépôt `microsoft/Trace` correspond à l'implémentation maintenue par les
auteurs lorsqu'ils étaient chez Microsoft ; il n'est plus la référence à
utiliser pour les nouveaux travaux.

OpenTrace trace un workflow Python comme un graphe de calcul et permet de rendre
des paramètres **ou des fonctions** entraînables. Les optimiseurs peuvent
recevoir du feedback très général : valeurs numériques, texte en langage
naturel, erreurs de tests/compilation, etc.

Cela correspond bien à OpenVigie/Pyronear, à condition de ne pas l'utiliser
comme auto-update de production.

### Feedbacks exploitables

| Source | Exemples |
|---|---|
| vérité terrain | recall, miss, temps depuis ignition, distance/visibilité |
| faux positifs | FP/jour + classe `fog`, `cloud`, `dust`, `industrial`… |
| géométrie | erreur localisation, taille ellipse, erreur azimut/tilt |
| performance | latency p50/p95, CPU, RAM, énergie, température |
| réseau | octets/alerte, débit moyen, taux de retry |
| PTZ | mouvements/jour, temps de confirmation, erreur de pointage |
| qualité code | pytest, ruff, type-checking, benchmarks, invariants |
| humain | commentaire opérateur : « trop sensible à la brume », « crop trop serré », « localisation crédible » |

### Cibles d'optimisation utiles aujourd'hui

- `conf_thresh`, `model_conf_thresh`, fenêtre temporelle ;
- résolution d'inférence / tiling / stratégie candidate-first ;
- cadence de capture ;
- taille et qualité JPEG des preuves ;
- seuils d'association de séquences ;
- règles de regroupement multi-caméras ;
- seuils et poids des features physiques ;
- stratégie de confirmation PTZ ;
- paramètres de visibilité/MNT ;
- éventuellement des fonctions de matching ou fusion, avec tests de
  non-régression.

Les issues Pyronear fournissent déjà d'excellents cas de régression :
[`pyro-api #662`](https://github.com/pyronear/pyro-api/issues/662) décrit un
matching de séquence où une grande bbox peut « voler » les détections d'un autre
feu ; [`#643`](https://github.com/pyronear/pyro-api/issues/643) porte sur le
raffinement d'azimut et le recalcul de triangulation ; `pyro-engine #397`
documente une calibration PTZ imparfaite à grands angles.

### Pareto : couche à ajouter autour d'OpenTrace

Aucun optimiseur Pareto multi-objectifs natif n'a été identifié dans le dépôt
OpenTrace inspecté. Il ne faut donc pas prétendre que le framework fournit
directement NSGA-II ou un front de Pareto.

Le schéma proposé est :

1. OpenTrace génère/modifie une configuration ou une fonction ;
2. une campagne reproductible l'évalue ;
3. on collecte un vecteur d'objectifs :
   `(recall, FP/jour, TTD, erreur_geo, CPU, RAM, Wh, réseau, PTZ)` ;
4. un petit contrôleur extérieur conserve les solutions **non dominées** ;
5. le feedback textuel complet, y compris commentaires humains et échecs de
   tests, repart vers OpenTrace ;
6. aucune solution n'est mergée ou déployée sans contraintes dures, holdout,
   régressions et revue humaine.

Les métriques de sûreté deviennent des **contraintes**, pas des objectifs que
l'optimiseur peut sacrifier : par exemple recall minimal et absence de
régression sur feux faibles avant d'optimiser énergie ou bande passante.

---

## 7. Décision hardware

La Reolink RLC-823S2 supportée par Pyronear est déjà une caméra intégrée très
compétitive : 8 MP, 16×, PTZ, PoE, IP66 et disponibilité française immédiate.
OpenVigie ne doit donc pas présenter un module nu IMX675/OpenIPC comme
amélioration par défaut.

OpenIPC/STARVIS2 ne redevient pertinent que si des essais A/B montrent un gain
mesurable, par exemple :

- meilleure détection de fumée faible/NIR ;
- contrôle ISP impossible à obtenir sur Reolink ;
- forte réduction de consommation/coût ;
- nécessité démontrée d'exécuter sans micro-ordinateur.

Sinon, le temps communautaire est mieux investi dans les huit contributions
ci-dessus.

---

## 8. Positionnement public

> **OpenVigie est un SDK/labo complémentaire de Pyronear, upstream-first.**
> Il sert à mesurer les limites du système existant et à expérimenter des
> améliorations de détection, géométrie, calibration, incertitude et
> optimisation. Lorsqu'une brique est générique et utile, la priorité est de la
> proposer à Pyronear plutôt que de maintenir une alternative parallèle.
