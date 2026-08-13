# Contribuer à OpenVigie

## Ce qui a le plus de valeur

Par ordre décroissant, et l'ordre compte :

1. **Des négatifs annotés de vos sites.** Écobuage, pollen de pin, poussière de
   moisson, brouillard de vallée, aéroréfrigérant, feux d'artifice, toile
   d'araignée sur le hublot. Ces images n'existent nulle part et ce sont elles
   qui fixent les seuils. Il existe des centaines de milliers d'images de fumée
   en accès libre ; il n'existe aucune image de *votre* horizon.
2. **Un portage de pilote capteur STARVIS 2 dans OpenIPC.** Un seul portage
   débloque toutes les cartes du même capteur. C'est le blocage matériel n°1.
3. **Des mesures terrain** : répétabilité de preset, vibration de mât, portée
   réelle par météo, taux de faux candidats. Le format de `site_survey.py` est
   fait pour être partagé.
4. **L'agent de site et la plateforme centrale** (P0/P1 de la [roadmap](ROADMAP.md)).
5. **Du code** sur le reste.

## Mettre en place

```bash
./scripts/bootstrap.sh
source .venv/bin/activate
make test-all
```

## Règles

### Les tests passent dans les deux modes

```bash
make test-all      # pytest, puis OPENVIGIE_FORCE_NUMPY=1 pytest
```

Ce n'est pas une coquetterie : le cœur doit tourner sur une carte caméra où
seuls NumPy et PyYAML sont installés. Toute dépendance à OpenCV ou SciPy doit
passer par `openvigie.compat` et avoir un repli NumPy pur.

### Le code de détection reste testable sans matériel

Toute fonction qui parle à un équipement prend ses dépendances en argument :
`run`, `read_file`, `sleep`, `clock`. Un code qui n'est testable que sur pylône
n'est pas testé.

### Les tests négatifs valent plus que les positifs

Un nouveau détecteur, un nouveau critère, un nouveau seuil doivent venir avec le
cas où ils *ne doivent pas* déclencher. Le taux de fausses alertes est la
métrique qui décide de l'adoption ; le rappel ne se discute qu'ensuite.

### Les seuils sont en unités physiques

m, m², m²/s, m/s, degrés. Jamais en pixels. Un seuil en pixels n'est pas
transférable d'un site à un autre, et c'est la raison principale pour laquelle ce
type de système doit habituellement être re-réglé à la main sur chaque tour.

### Pas de dégradation silencieuse

Un backend indisponible, un modèle de fond immature, un recalage rejeté : tout
cela doit apparaître dans `summary()` et dans le battement de cœur. Un système
qui se dégrade sans le dire est pire qu'un système en panne.

### Licences

Le cœur reste **Apache-2.0**. Aucune dépendance copyleft ne doit devenir
obligatoire. Un backend s'appuyant sur du code AGPL est acceptable **comme extra
optionnel**, jamais comme dépendance par défaut — voir
[docs/LICENCES.md](docs/LICENCES.md).

### Style

```bash
make lint      # ruff, ligne à 110
```

Commentaires et documentation en français, comme le reste du dépôt. Un
commentaire explique *pourquoi*, pas *quoi* : le code dit déjà quoi.

## Signaler un problème

Pour un problème de détection, joindre si possible : la configuration du site
(sans jeton), la sortie de `openvigie doctor`, et la sortie de `openvigie plan`. Pour un
problème matériel, la sortie de `openvigie hw` sur la cible.

## Ce que le projet n'acceptera pas

- Le déclenchement automatique de secours sans validation humaine.
- Le streaming vidéo permanent vers un serveur central.
- Un chemin de code qui contourne le veto « origine au sol » sur la seule foi
  d'un score de réseau.
- Des chiffres de performance non reproductibles dans la documentation.
