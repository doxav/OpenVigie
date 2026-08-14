# Roadmap

Ordre directeur : **détecter → géolocaliser → recouper → valider → apprendre →
intégrer aux secours.**

Et surtout pas l'inverse. Brancher un système de gestion opérationnelle avant
d'avoir démontré la détection, c'est engager une dépendance institutionnelle
autour d'une capacité non prouvée.

Légende : ✅ fait · 🟡 partiel · ⬜ à faire

## Niveau de maturité réel

Un audit externe de la 0.3.0 a relevé, à juste titre, que le marquage
« phase faite » décrivait la **présence des primitives**, pas leur intégration
opérationnelle. Trois colonnes valent mieux qu'une case :

| Fonction | Code de bibliothèque | Intégrée de bout en bout | Validée terrain |
|---|---|---|---|
| Géométrie, budget optique, viewshed | ✅ | ✅ | ❌ |
| Recalage, fond, candidats, suivi, fusion | ✅ | ✅ | ❌ |
| Géoréférencement MNT | ✅ | ✅ | ❌ |
| Schéma d'événement, cycle de vie | ✅ | ✅ | ❌ |
| File hors ligne durable | ✅ | ✅ | ❌ |
| Masques de confidentialité | ✅ | ✅ | ❌ |
| Corrélation multi-tours | ✅ | ❌ | ❌ |
| Étalonnage ADS-B | ✅ | ❌ | ❌ |
| Confirmation PTZ | 🟡 | ❌ | ❌ |
| Modèle temporel, segmentation | ❌ | ❌ | ❌ |
| Backend NNIE | 🟡 | ❌ | ❌ |
| Agent continu | ✅ | ✅ | ❌ |

`openvigie capabilities` produit ce constat pour une configuration donnée.

---

## Phase 0 — Fondations *(fait)*

| | Élément | État |
|---|---|---|
| P0 | Budget optique, portée, revisite, usure mécanique | ✅ `geometry` |
| P0 | Recalage, modèle de fond, candidats, suivi | ✅ `registration`, `background`, `candidates`, `tracking` |
| P0 | Features physiques en unités réelles | ✅ `tracking` |
| P0 | Fusion calibrée + veto géométrique + hystérésis | ✅ `scoring` |
| P0 | Abstraction matérielle OpenIPC multi-SoC | ✅ `platform` |
| P0 | Contrôles d'équipement et de configuration | ✅ `hwcheck` |
| P0 | Trois niveaux de déploiement | ✅ `config`, `docs/HARDWARE.md` |

## Phase 1 — Autonomie du site *(fait)*

Un site doit rester utile seul, sans plateforme et sans réseau.

| | Élément | État |
|---|---|---|
| P0 | **Schéma d'événement figé** + cycle de vie | ✅ `events` |
| P0 | **Store-and-forward durable** avec réémission | ✅ `transport.Outbox` |
| P0 | **Battement de cœur et santé du site** | ✅ `transport.HealthMonitor` |
| P0 | **Horodatage UTC** systématique | ✅ `events.utc_now_iso` |
| P0 | **Géoréférencement MNT** (ray-casting, courbure, viewshed) | ✅ `dem` |
| P0 | **Incertitude de localisation** portée par l'événement | ✅ `events.Uncertainty` |
| P0 | Typologie des décisions opérateur | ✅ `events.OPERATOR_DECISIONS` |
| P1 | **Corrélation multi-tours** + triangulation + sollicitation PTZ | ✅ `correlation` |
| P1 | **Étalonnage géométrique par trafic aérien** (ADS-B) | ✅ `calibration` |
| P0 | Campagne de mesure et banque de fonds | ✅ `scripts/` |

---

## Phase 2 — Réseau et exploitation *(prochaine)*

### P0 — À faire avant tout déploiement à plusieurs sites

*Priorités confirmées par l'audit externe de la 0.3.0 (les corrections de
justesse sont livrées en 0.4.0, voir CHANGELOG.md).*

| Élément | Pourquoi | Difficulté |
|---|---|---|
| ✅ **Agent de site** (`openvigie run`) | Boucle multi-caméras fixe/PTZ, reprise avec backoff, flush, heartbeat, signaux et fermeture propre ; spécification et limites dans `docs/AGENT_CONTINU.md` | livrée, validation terrain à faire |
| ⬜ **Cache MNT par vue** | Le ray-casting est fait à l'installation ; il manque la persistance sur disque et l'invalidation quand un preset bouge | faible |
| 🟡 **Preuves d'alerte** | Les vignettes et séquences sont référencées dans l'événement mais pas encore capturées et stockées automatiquement | faible |
| ⬜ **Chargeur MNT depuis GeoTIFF** | Script de conversion des dalles IGN vers le format `.npy` + `.json`, hors passerelle | faible |
| ⬜ **Validation terrain de l'étalonnage ADS-B** | Le module n'a été éprouvé que sur simulation. Le maillon à confronter au réel en premier est la détection des points : un avion à 1 px sur un capteur bruité n'est pas un point gaussien synthétique | moyenne |
| ⬜ **Boucle d'étalonnage continue** | Le calcul existe ; il manque le démon qui collecte l'ADS-B, accumule les points, réajuste chaque nuit et alerte sur dérive | moyenne |
| ⬜ **Sécurité du transport** | mTLS optionnel, rotation de jeton, empreinte de certificat épinglée | moyenne |

### P1 — Ce qui fait passer d'un capteur à un système

| Élément | Pourquoi | Difficulté |
|---|---|---|
| ⬜ **Plateforme centrale minimale** | API de réception, base spatiale, carte, timeline, accusé de réception. Forker `pyro-api` / `pyro-platform` plutôt que réécrire | élevée |
| ⬜ **Portail de levée de doute** | Un opérateur doit valider ou invalider en quelques secondes, avec la séquence avant/après | moyenne |
| ⬜ **Boucle d'apprentissage automatisée** | Chaque invalidation part dans le jeu de négatifs du site, réentraînement mensuel, recalibration du seuil | moyenne |
| ⬜ **Station météo locale** | Le vent réellement observé vaut mieux qu'un vent modélisé pour tester la cohérence de dérive. Peu coûteux, très rentable | faible |
| ⬜ **Estimation de visibilité en continu** | Le calcul existe (`estimate_visibility_m`) ; il manque la sélection d'amers fixes par vue et la journalisation | faible |
| ⬜ **Étalonnage sur amers** (sommets, pylônes, clochers) | Complémentaire de l'ADS-B : n'exige aucune horloge, mais échoue sur horizon plat et ne détecte pas la dérive. Les deux ensemble couvrent tous les sites | moyenne |
| ⬜ **Étalonnage sur soleil et lune** | Positions calculables à la seconde d'arc, aucune dépendance externe. Rarement dans le champ d'une caméra de guet, mais gratuit quand ça l'est | faible |
| ⬜ **Calendriers de brûlages dirigés** | Le faux positif n°1 en France. Géofencing des parcelles déclarées quand le flux existe | faible |
| ⬜ **Liaison de secours 4G/5G** | Bascule automatique, et surtout mesure de ce que la file d'attente absorbe pendant une coupure | moyenne |
| ⬜ **Contexte EFFIS / indice de danger** | Pondérer le risque, prioriser les secteurs les jours rouges | faible |
| ⬜ **Confirmation PTZ automatisée bout en bout** | Les tâches sont calculées (`confirmation_tasks`) ; il manque l'exécution : interruption de ronde, pointage, reclassification | moyenne |

### P2 — Quand les indicateurs de détection sont démontrés

| Élément | Condition préalable |
|---|---|
| ⬜ Intégration à un système de gestion opérationnelle | Un partenariat, et des taux de fausses alertes tenus |
| ⬜ Adaptateur vers un format d'échange métier | Le schéma d'événement est déjà conçu pour qu'un adaptateur se branche dessus sans toucher au cœur |
| ⬜ Multi-tenant et routage géographique | Réseau à l'échelle départementale |
| ⬜ Recoupement satellite *a posteriori* | Utile pour l'étiquetage automatique, jamais comme déclencheur : la latence se compte en heures |
| ⬜ Supervision de flotte, maintenance prédictive | Dizaines de sites |
| ⬜ Voie thermique LWIR | Uniquement si la performance nocturne devient contractuelle |

### P3 — Délibérément reporté

| Élément | Pourquoi pas maintenant |
|---|---|
| ⬜ Modèle de propagation | Aide au commandement, pas à la détection. Autre métier |
| ⬜ Application grand public | Autre produit, autres responsabilités |
| ⬜ Alerte directe à la population | Prérogative des autorités |
| ⬜ Déclenchement automatique de secours | **Jamais.** L'humain reste dans la boucle |

---

## Améliorations du cœur

Priorisées par rapport gain / effort, indépendamment des phases.

| Élément | Gain attendu | Effort |
|---|---|---|
| ⬜ **Portage pilote IMX675 dans OpenIPC** | Débloque toute la gamme STARVIS 2. Le blocage matériel n°1 du projet | élevé |
| ⬜ **Binaire d'inférence NNIE** | Rend le tier MEDIUM autonome pour de bon ; aujourd'hui il se replie sur l'étage classique | élevé |
| ⬜ **Modèle ONNX de référence** entraîné sur Pyro-SDIS, publié | Rend le projet utilisable sans entraînement préalable | moyen |
| ⬜ **Vérification temporelle CNN+LSTM ou TSM** | Le gain le plus documenté de la littérature sur la détection précoce | moyen |
| ⬜ **Segmentation du candidat** (tier FULL) | Croissance et ascendance nettement plus précises que sur une boîte | moyen |
| ⬜ **Modèle de nuit dédié** | La nuit est traitée aujourd'hui par les mêmes seuils que le jour, avec un état séparé mais pas de modèle propre | moyen |
| ⬜ **Détection de dégradation optique automatique** | `check_window_cleanliness` existe ; l'intégrer à la boucle et déclencher une alerte de maintenance | faible |
| ⬜ **Porte anémométrique** | Suspendre l'analyse quand le mât vibre au-delà d'un seuil mesuré, plutôt que de filtrer après coup | faible |
| ⬜ **Récepteur ADS-B dans la nomenclature** | Clé SDR ~25 € + antenne. Débloque l'étalonnage continu sans dépendance Internet | faible |
| ⬜ **Calibration par site automatisée** | Ajuster les seuils sur les 30 jours de négatifs sans intervention manuelle | moyenne |

---

## Ce qui n'est volontairement pas au programme

- **Streaming vidéo permanent vers un serveur.** Huit caméras à un JPEG de 500 ko
  toutes les 30 s représentent déjà ~11 Go/jour. L'architecture est analyse
  locale, remontée événementielle.
- **Segmentation en continu sur l'image entière.** Uniquement sur candidat.
- **Voie thermique sur chaque tour.** Coût disproportionné hors besoin nocturne avéré.
- **Un modèle unique « global » sans calibration par site.** Le seuil optimal
  varie d'un facteur 2 à 3 entre un site landais et un site alpin.

---

## Contribuer

Par valeur décroissante :

1. **des négatifs annotés de vos sites** — écobuage, pollen, poussière de
   moisson, brouillard de vallée, aéroréfrigérant, feux d'artifice. Ces images
   n'existent nulle part et ce sont elles qui fixent les seuils ;
2. **un portage de pilote capteur STARVIS 2 dans OpenIPC** ;
3. des mesures de répétabilité, de vibration et de portée réelle sur vos sites ;
4. l'agent de site et la plateforme centrale (P0/P1 ci-dessus) ;
5. du code.

Toute contribution doit conserver le double passage des tests, avec et sans
OpenCV/SciPy.
