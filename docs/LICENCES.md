# Licences

## Ce dépôt

**Apache-2.0.** Ce choix est délibéré et aligné sur l'écosystème existant
(Pyronear publie ses briques sous Apache-2.0) : il permet à une association, un
service de secours, une collectivité ou une entreprise d'adopter et d'adapter le
code sans friction.

Le cœur ne dépend que de **NumPy** (BSD-3) et **PyYAML** (MIT). Aucune
dépendance copyleft n'est requise pour faire tourner le projet.

## Dépendances optionnelles

| Extra | Paquets | Licence | Effet |
|---|---|---|---|
| `full` | opencv-python-headless, scipy, onnxruntime, requests | Apache-2.0 / BSD | aucune contrainte |
| `ptz` | pyserial, requests | BSD / Apache-2.0 | aucune contrainte |
| `dev` | pytest, ruff | MIT | aucune contrainte |
| `ultralytics` | ultralytics | **AGPL-3.0** | voir ci-dessous |

## Le greffon Ultralytics

`UltralyticsDetector` permet d'utiliser directement les poids Pyronear entraînés
sur Pyro-SDIS, ce qui est l'option la plus rapide pour obtenir un détecteur de
qualité. Le paquet `ultralytics` est sous **AGPL-3.0**.

Le greffon n'est donc **jamais installé ni importé par défaut**. Il faut le
demander explicitement :

```bash
pip install "OpenVigie[ultralytics]"
```

Ce que cela implique :

- **projet ouvert, déploiement associatif ou public** : aucune difficulté. C'est
  le cas d'usage prévu, et probablement le vôtre.
- **intégration dans un produit fermé ou un service en ligne propriétaire** :
  l'AGPL s'applique, y compris à l'usage en réseau. Il faut alors soit publier
  la source de l'ensemble, soit prendre une licence entreprise Ultralytics, soit
  utiliser un détecteur Apache-2.0.

Alternatives Apache-2.0 pour le backend `onnx`, sans aucune contrainte :
**RTMDet-tiny** (MMDetection), **YOLOX**, **D-FINE**, **NanoDet**, **EfficientDet**.

## Jeux de données

Les licences des jeux publics diffèrent de celles du code et doivent être
vérifiées une par une : voir [DONNEES.md](DONNEES.md). Pyro-SDIS est en
Apache-2.0 ; plusieurs autres sont en usage recherche uniquement.

## Modèles entraînés

Un modèle entraîné hérite des contraintes du code d'entraînement *et* des jeux de
données utilisés. Entraîner avec `ultralytics` sur un jeu en licence recherche
produit des poids que vous ne pouvez ni redistribuer librement ni exploiter
commercialement. Documentez la provenance de chaque modèle — le champ
`model_version` de chaque alerte est prévu pour ça.
