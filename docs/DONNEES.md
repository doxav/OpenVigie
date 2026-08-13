# Jeux de données

Le dépôt ne contient ni images ni poids. Cette page recense ce qui est utile, et
sous quelles conditions. **Vérifiez chaque licence avant usage** : elles diffèrent
et changent.

## Le plus proche du cas d'usage

**Pyro-SDIS** — `huggingface.co/datasets/pyronear/pyro-sdis` — ~33 600 images
issues de caméras installées sur points hauts avec des services d'incendie et de
secours français, annotées au format YOLO, **licence Apache-2.0**. Également
publié sur data.gouv.fr. C'est le point de départ recommandé : mêmes conditions
d'observation, mêmes paysages, mêmes faux positifs.

**PyroNear-2025** — `arxiv.org/abs/2402.05349` — ~50 000 images, ~150 000
annotations, 640 incendies, France / Espagne / Chili / États-Unis, avec des
**séquences vidéo** permettant d'entraîner et d'évaluer des modèles temporels.
À noter : le papier lui-même montre un F1 inter-datasets d'environ 60 %, ce qui
en fait un jeu difficile et un test d'honnêteté utile.

**pyro-dataset** — `github.com/earthtoolsmaker/pyro-dataset` — pipeline de
construction de jeux qui contient notamment `FP_2024`, les faux positifs réels
collectés par un système en exploitation. Des négatifs durs issus de vraies
caméras : c'est rare et précieux.

## Autres jeux utiles

| Jeu | Volume | Où | Rôle |
|---|---|---|---|
| FIgLib (HPWREN) | ~25 000 images de caméras de montagne, horodatées autour de l'ignition | `hpwren.ucsd.edu/HPWREN-FIgLib/` | mesurer le temps de détection depuis l'ignition |
| FASDD | ~100 000 images (caméras, tours de guet, drones, satellite) | `github.com/openrsgis/FASDD` | volume, petits objets |
| D-Fire | 21 527 images, 26 557 boîtes, dont 9 838 négatifs | `github.com/gaia-solutions-on-demand/DFireDataset` | négatifs et diversité |
| AI For Mankind | 2 192 images + négatifs | `github.com/aiformankind/wildfire-smoke-dataset` | négatifs durs |
| DFS Fire/Smoke | 9 462 images | `github.com/siyuanwu/DFS-FIRE-SMOKE-Dataset` | diversification |

Réseaux de caméras publics (ALERTWildfire, HPWREN) : des milliers d'heures de flux
réels de tours, exploitables pour constituer une bibliothèque de négatifs **avant
même d'installer un mât**.

## Le déséquilibre à comprendre

Vous n'avez pas besoin de plus d'images de fumée. Il en existe des centaines de
milliers en accès libre, et un détecteur correct s'entraîne dessus en une journée.

Vous avez besoin de **négatifs de vos sites** : écobuage et brûlage de sarments en
hiver, pollen de pin au printemps, poussière de moissonneuse en été, brouillard de
vallée au petit matin, panaches d'aéroréfrigérants, feux d'artifice de juillet,
toiles d'araignée sur le hublot, vibration du mât. Ces images n'existent nulle
part, et ce sont elles qui fixent le seuil de décision.

D'où l'ordre imposé par le dépôt : **30 jours de `record_baseline.py` avant toute
mise en service.**

## Protocole d'évaluation

Le seul découpage acceptable est **par incendie ET par site ET par saison**. Un
découpage aléatoire image par image fait fuiter des images du même feu entre
apprentissage et test, et produit des scores flatteurs sans aucune valeur.

Métriques à défendre, dans cet ordre :

| Métrique | Objectif de départ |
|---|---|
| Fausses alertes / caméra / jour | < 1 de jour, < 0,2 de nuit |
| P(détection ≤ 5 min) à 5 km, de jour | ≥ 90 % |
| P(détection ≤ 5 min) à 10 km, de jour | ≥ 70 % |
| Erreur de localisation | < 300 m à 5 km (1 tour + MNT), < 100 m (2 tours) |
| Disponibilité | > 98 % hors maintenance |

Vérité terrain d'ignition : les **brûlages dirigés** conduits en novembre–mars
avec les gestionnaires forestiers et les services de secours ont une heure
d'allumage connue à la minute. C'est la seule vérité terrain parfaite, et elle
est gratuite.
