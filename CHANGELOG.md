# Journal des modifications

## Non publié — agent continu et achèvement du MINIMAL sectoriel

- **Agent de site continu.** Nouvelle commande `openvigie run` : topologie
  caméra/vue strictement configurée, sources snapshot/RTSP/fichiers, ronde PTZ
  après stabilisation, reprise indépendante avec backoff exponentiel, flush de
  l'outbox, heartbeat, arrêt `SIGINT`/`SIGTERM` et fermeture des ressources.
  `--dry-run`, `--once`, `--max-frames` et le résumé JSON rendent la recette
  reproductible. Les alertes `shadow` ont un journal local durable dédié et les
  secrets caméra restent dans l'environnement.
- L'analyse de faisabilité, l'arbitrage Pareto, le contrat de configuration, la
  machine d'état, la matrice de défaillances et les critères d'acceptation sont
  spécifiés dans `docs/AGENT_CONTINU.md`.
- `SIP-K675A-30X` est désormais appelé **bloc caméra zoom 30×** : il ne contient pas la tête Pan/Tilt.
- `plan`, `doctor` et `ptz-test` respectent les secteurs déclarés.
- `recommend()` n'ignore plus le budget d'usure PTZ.
- Le préréglage `minimal` est effectivement migré vers 140°/4 positions/8 km, conformément à l'intention 0.6.0.

## 0.6.1 — correctif de test (environnement conda/venv)

`TestScriptDeployPackaging.test_sequence_de_paquetage_produit_un_paquet_importable`
remplaçait tout l'environnement du sous-processus par
`env={"OPENVIGIE_FORCE_NUMPY": "1", "PATH": "/usr/bin:/bin"}` et invoquait le
binaire `python3` résolu via ce `PATH` tronqué. Ça passait par coïncidence
quand `/usr/bin/python3` avait NumPy installé au niveau système, et échouait
systématiquement sur une installation conda ou venv — c'est-à-dire
l'environnement de développement le plus courant. Signalé par un utilisateur
au premier `make test-all` sur sa machine.

Corrigé : `sys.executable` au lieu de la chaîne `"python3"`, pour garantir
qu'on relance le même interpréteur que celui qui exécute les tests (donc avec
les mêmes paquets) ; `{**os.environ, "OPENVIGIE_FORCE_NUMPY": "1"}` au lieu de
remplacer l'environnement, pour préserver `PATH`, `HOME` et les variables de
l'environnement virtuel actif.

Aucun changement de comportement du logiciel — uniquement du test.

Le projet suit un versionnement sémantique à partir de la 1.0. Avant, les
versions mineures peuvent introduire des ruptures ; elles sont listées ici.

## 0.6.0 — secteurs utiles, relevé d'installation, kit de portage

### Issue #1 — la planification ne suppose plus une couverture 360°

Nouveau module `modules` et commande `openvigie sectors`. L'unité de
dimensionnement devient le **secteur angulaire utile**, déduit du viewshed ou
déclaré dans la configuration (`sectors:`). Trois architectures sont évaluées
sur les mêmes secteurs : anneau fixe, module PTZ, grand-angle + PTZ à la
demande.

Les huit critères d'acceptation de l'issue sont couverts par des tests portant
leur numéro. Résultats notables, tous issus du calcul :

- **le balayage PTZ, inexploitable à 360° (21 positions, > 5 min de cycle),
  redevient raisonnable sur 140° utiles (4 positions, 1,3 min)** — c'est
  l'argument central de l'issue, et il se vérifie ;
- **la tête PTZ (~1 450 $) domine tout budget PTZ** : un anneau de quatre
  modules fixes coûte 744 $ contre 1 861 $ pour un module PTZ, sans latence ni
  usure. Le module PTZ se défend par le nombre d'appareils à installer et à
  maintenir, pas par son prix ;
- **grand-angle + PTZ ne voit pas plus loin** : la portée est celle du
  grand-angle. C'est une architecture de levée de doute, pas d'extension de
  portée.

Le tier MINIMAL est redéfini en conséquence : une caméra zoom montée sur
tête Pan/Tilt couvre par défaut un secteur utile de 140° avec 4 positions à
8 km. La mécanique Pan/Tilt est distincte du bloc caméra zoom.

*Défaut trouvé en écrivant les tests :* une architecture pouvait être déclarée
« couvrante » en balayant tout l'angle demandé sans jamais voir assez loin pour
y détecter quoi que ce soit. Nouvelles propriétés `meets_range` et `is_viable`.

### Issue #2 — relevé d'installation comme amorce de calibration

Nouveau module `survey` et commande `openvigie survey`. Un relevé au smartphone
à la pose fournit une pose de départ, avec une répartition d'incertitudes très
inégale et c'est ce qui la rend utile :

| Grandeur | Incertitude | Pourquoi |
|---|---|---|
| Assiette, roulis | ±0,5° | l'accéléromètre mesure la gravité, que rien ne perturbe |
| Azimut | ±15° sur pylône treillis | le magnétomètre subit l'acier |

Soit **un facteur trente entre les deux axes**, exposé par `gate_axes_px()`.
Exactement complémentaire de l'étalonnage par trafic aérien, qui excelle sur
l'azimut. `calibrate_from_survey()` enchaîne les deux et signale les
incohérences : déclinaison oubliée, signe du tilt inversé.

**La déclinaison magnétique est obligatoire** — le module refuse de l'approximer
plutôt que de produire un azimut biaisé de 1 à 3°.

*Défaut trouvé en écrivant les tests :* quand le relevé est très faux et la
fenêtre serrée, l'appariement ne trouve rien, l'ajustement renvoie la pose
initiale inchangée, et l'écart mesuré est donc nul — un relevé aberrant était
rapporté comme « cohérent ». Un étalonnage insuffisant est désormais signalé
comme non concluant.

*Second défaut :* une position hors emprise du MNT renvoyait `NaN` et passait
silencieusement le contrôle de cohérence d'altitude.

### Portage IMX675 — ce qui est livré, et ce qui ne l'est pas

**Le portage n'est pas fait.** Il demande la table de registres du capteur
(documentation Sony sous accord de confidentialité), la carte elle-même, et des
essais — un capteur mal initialisé ne renvoie pas une erreur mais une image.
Publier un pilote d'apparence plausible avec des registres reconstitués serait
exactement l'artefact convaincant et faux que ce projet refuse.

Ce qui est livré : [docs/PORTAGE_IMX675.md](docs/PORTAGE_IMX675.md) (procédure,
paramètres attendus, quatre garanties à vérifier) et la commande
`openvigie sensor-validate`, exécutable par qui dispose d'une carte —
résolution effective, cadence et gigue, champ mesuré contre champ calculé.

`openvigie hw --soc hi3516av300 --sensor IMX675` continue de répondre
`porting_required`, et c'est la bonne réponse.

### Divers

- URL du dépôt renseignées (`doxav/OpenVigie`).
- 95 nouveaux tests (660 au total), couverture maintenue.

## 0.5.0 — renommage en profondeur vigie → OpenVigie

### Rupture

Le paquet, la commande CLI, le répertoire source, les variables d'environnement
et toute la documentation sont renommés de `vigie` à `OpenVigie` (paquet et
commande : `openvigie`, minuscule ; nom du projet en prose : `OpenVigie`).

- `import vigie` → `import openvigie`
- commande `vigie` → `openvigie`
- `VIGIE_FORCE_NUMPY` → `OPENVIGIE_FORCE_NUMPY`
- `VIGIE_TOKEN` → `OPENVIGIE_TOKEN`
- `src/vigie/` → `src/openvigie/`

Aucun changement de comportement : cette version est fonctionnellement
identique à la 0.4.0, à l'exception des corrections ci-dessous, trouvées en
vérifiant le renommage ligne par ligne plutôt que par recherche/remplacement
aveugle.

### Corrections trouvées en vérifiant le renommage

Un renommage mécanique sur un dépôt de cette taille (243 occurrences, 59
fichiers) produit presque toujours des faux positifs. Trois étaient réels :

- **`scripts/openipc_deploy.sh` — bug critique.** Le script créait
  `$TMP/OpenVigie` (majuscule) mais copiait les fichiers dans
  `$TMP/openvigie/` (minuscule) : `cp` aurait échoué faute de répertoire
  cible, et `tar` aurait empaqueté un répertoire vide. Tout déploiement
  `--push-agent` aurait silencieusement échoué. Corrigé, et couvert par un
  test qui rejoue la séquence mkdir/cp/tar et vérifie que le paquet produit
  est réellement importable.
- **`pyproject.toml`.** Le nom du paquet et la clé `[project.scripts]`
  s'étaient retrouvés capitalisés (`name = "OpenVigie"`,
  `OpenVigie = "openvigie.cli:main"`) : l'exécutable installé aurait été
  `OpenVigie`, sensible à la casse, au lieu de `openvigie`.
- **`argparse.ArgumentParser(prog="OpenVigie")`** — incohérent avec la
  commande réellement installée ; l'aide (`--help`) aurait affiché un nom
  différent de ce que l'utilisateur tape réellement.

`Makefile` et `NOTICE` (sans extension de fichier) avaient également échappé
au script de renommage automatique et ont dû être corrigés séparément.

### Couverture de test

Couverture combinée (avec et sans OpenCV/SciPy) mesurée à 92 % avant cette
version. 94 nouveaux tests ciblent les lignes non couvertes classées à risque
réel — et non la totalité des lignes manquantes, dont une partie reste
légitimement hors périmètre (code strictement dépendant de matériel ou de
réseau réels). Couverture combinée après : **97 %**. Points notables :

- `NetworkConfig` n'était référencée par **aucun** test existant : une faute
  de frappe dans `network.transport` ou un transport `http` sans URL
  n'étaient détectés qu'à l'exécution sur site.
- `pipeline.flush()`/`heartbeat()` appelés sans `outbox`/`transport` — le cas
  le plus courant en pratique — n'étaient jamais exercés.
- Le chemin combiné pose étalonnée + triangulation multi-tours n'était jamais
  atteint en une seule fois (chaque brique testée isolément).
- Plusieurs seuils « warn » des contrôles matériel n'étaient vérifiés que par
  une assertion large (`status in ("warn", "fail")`), qui n'aurait pas
  détecté une inversion de seuil.

### README

Nouvelle section d'ouverture contextualisant le projet dans la saison 2026
(records de surface brûlée en France, étude d'attribution climatique) et
proposant une organisation de contribution par temps disponible, incluant
explicitement les profils non-développeurs (retour de terrain, sécurité
incendie) aux côtés des profils techniques.

## 0.4.0 — corrections issues de l'audit externe

Cette version répond à un audit systématique de la 0.3.0. Les défauts corrigés
partagent une propriété désagréable : **aucun ne provoquait d'erreur visible**.
Ils produisaient des résultats plausibles et faux — une localisation
convaincante mais décalée, un fond qui se dégrade lentement, une alerte émise
avec des poids que la documentation elle-même qualifiait de provisoires.

Chaque correction porte un identifiant d'audit, présent en commentaire dans le
code et couvert par `tests/test_audit_fixes.py`.

### Ruptures

- **Les horodatages naïfs sont refusés** (P0-12). `process_frame` exige un
  `datetime` avec fuseau. Deux tours dans des fuseaux différents, ou un passage
  à l'heure d'été, faussaient silencieusement la corrélation multi-tours.
- **Un site ne peut plus alerter par défaut** (P0-06). Nouveau bloc
  `operating.mode` : `measure` (aucun événement), `shadow` (journalisé, non
  transmis), `alert` (transmission, refusée si le modèle de fusion n'est pas
  calibré). Les préréglages passent en `measure` pour MINIMAL et `shadow` pour
  MEDIUM et FULL.
- **`openvigie doctor` échoue désormais** quand une capacité déclarée est absente
  (P0-05, P0-22). C'était l'objet du diagnostic ; il renvoyait « tout va bien »
  sur une configuration non déployable.
- `extract_candidates` peut renvoyer un tuple avec `return_change_fraction=True`.

### Corrections de justesse géométrique

- **P0-10 — Projection rectilinéaire.** `flat_earth_distance_map`,
  `horizon_row` et `distance_map_from_dem` répartissaient les angles
  linéairement sur les pixels, ce qui décrit une projection équirectangulaire.
  Au grand-angle (2,8 mm) l'écart atteignait **3,4° en bord de champ, soit
  ~300 m à 5 km**, et surtout ces fonctions étaient **incohérentes avec
  `pixel_to_bearing`**, déjà rectilinéaire : le relèvement et la distance d'une
  même alerte ne suivaient pas le même modèle optique.
- **P0-04 — ROI recalée.** La région soumise au classifieur était découpée dans
  l'image *brute* alors que la boîte provenait de l'image *recalée*. Dès que la
  caméra bougeait — c'est-à-dire en permanence sur un pylône — le classifieur
  examinait une zone décalée du candidat.
- **P0-11 — Cohérence au vent en repère géographique.** La dérive était mesurée
  en coordonnées image et comparée à un azimut absolu : la même fumée était
  déclarée cohérente ou incohérente selon l'orientation de la caméra. Seule la
  composante tangentielle est désormais utilisée, et le critère renvoie une
  valeur neutre quand le vent souffle dans l'axe de visée.
- **P0-09 — MNT projeté refusé.** Une dalle Lambert-93 interrogée en lat/lon ne
  renvoyait pas d'erreur : elle renvoyait un terrain arbitraire mais plausible.
- **P0-13 — Altitude du site.** Elle était figée à 0 m dans le chemin
  d'étalonnage. Sur une tour à 900 m, l'élévation des aéronefs était fausse de
  plusieurs dixièmes de degré, soit exactement la grandeur mesurée.

### Corrections d'hygiène du modèle de fond

- **P0-05** — le fond était alimenté avec l'image brute : il apprenait la
  vibration du mât et accumulait des contours fantômes.
- **P0-06** — un changement global (brouillard, bascule WDR) était appris comme
  nouveau fond. Nouveau statut `global_change`.
  *Défaut supplémentaire trouvé en écrivant le test* : le garde-fou
  `max_area_frac` ne se déclenchait **pratiquement jamais**, le seuil MAD
  s'adaptant à la dispersion de la différence elle-même. Le changement global se
  mesure désormais sur des statistiques globales — décalage de niveau médian et
  rapport d'énergie de gradient.
- **P0-07** — seules les pistes `CONFIRMED` gelaient l'apprentissage : un
  panache lent pouvait être absorbé avant d'être confirmé.

### Sûreté, confidentialité, durabilité

- **P0-21 — Masques de confidentialité appliqués.** Ils figuraient dans la
  configuration et la documentation les présentait comme une protection, mais
  aucun code ne les lisait. Ils sont maintenant appliqués à l'acquisition, donc
  avant analyse, stockage et transmission. Nouveau module `masking`.
- **P0-19 / P0-20 — File d'attente.** Les échecs définitifs n'existaient qu'en
  mémoire et disparaissaient au redémarrage ; la saturation supprimait
  silencieusement les entrées les plus anciennes. Les dead letters sont
  persistées avec leur motif et rejouables (`replay_dead_letters`), et la
  saturation est comptée et remontée.
- **P0-17 — Identifiants d'alerte uniques.** Ils étaient horodatés à la seconde :
  deux alertes de la même seconde sur la même vue se confondaient.
- **P0-14 — Presets PTZ.** Le numéro de preset était déduit du rang dans la
  séquence de visite, où les vues prioritaires sont dupliquées : une même vue
  commandait plusieurs presets physiques, dont certains n'avaient jamais été
  enregistrés.
- **P0-08 — Mémoire.** Les fonds sont stockés en `uint8` (le commentaire
  l'annonçait, l'implémentation utilisait `float32`, soit quatre fois
  l'empreinte : 173 Mio pour une seule clé à 5 MP). Le nombre de clés est borné
  par éviction LRU. Le cache `recent_frames`, jamais lu, est supprimé. Les
  pistes rejetées sont purgées et l'historique d'événements est borné.
- **P1-18 — Jour/nuit.** `sunrise_h` et `sunset_h` figuraient dans la
  configuration mais n'étaient jamais transmis : un site alpin en décembre et un
  site corse en juin partageaient les mêmes bornes.

### Honnêteté sur les capacités

- **P0-03 / P1-11 — Nouvelle commande `openvigie capabilities`.** Plusieurs drapeaux
  de configuration (`use_segmentation`, `use_temporal_model`,
  `use_ptz_confirmation`) n'étaient consommés par aucun code. La commande dit ce
  qui fonctionne réellement.
- **P0-05 / P0-22 — `openvigie doctor` élargi** : plateforme, pilote capteur,
  backend effectif, mode d'exploitation, masques.
- **P0-04 — Matrice matérielle stricte** : `max_sensor_mp` n'était comparé à
  rien ; `gk7605v100 + IMX415` (8,5 MP sur un SoC plafonné à 5 MP) était annoncé
  « prêt ». Nouveau statut `resolution_exceeded`.
- **P0-02 — Paquet edge complet.** La liste de modules était tenue à la main et
  omettait `events`, `transport`, `dem`, `correlation` : `import openvigie`
  réussissait, `import openvigie.pipeline` échouait, et le script affichait quand
  même « opérationnel ». Le paquet copie désormais tout et vérifie l'import du
  pipeline.

### Ce qui reste ouvert

L'audit relève à juste titre l'absence d'agent continu (`openvigie run`), de modèle
de référence, de capture automatique des preuves, de validation terrain et de
transport mTLS. Ces points sont en tête de [ROADMAP.md](ROADMAP.md) ; aucun
n'est traité dans cette version, et `openvigie capabilities` les déclare absents.

## 0.3.0

Étalonnage géométrique par trafic aérien (ADS-B) : modèle de caméra, ajustement
robuste de pose, analyse d'identifiabilité, détection de dérive.

## 0.2.0

Schéma d'événement canonique, store-and-forward durable, supervision,
géoréférencement par MNT, corrélation multi-tours.

## 0.1.0

Noyau : géométrie, recalage, modèle de fond, candidats, suivi, fusion, PTZ,
abstraction matérielle OpenIPC, contrôles d'équipement.
