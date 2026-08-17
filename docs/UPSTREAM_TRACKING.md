# Suivi des contributions upstream

*Registre unique des briques développées dans OpenVigie et destinées à
Pyronear. À tenir à jour à chaque changement d'état — c'est la source de vérité
sur ce qui a été proposé, ce qui attend, et ce qui a été retenu.*

## États

| État | Signification |
|---|---|
| `prêt` | développé et validé ici ; texte de PR rédigé ; **pas encore envoyé** |
| `discussion` | issue ouverte en amont, en attente de retour avant de proposer du code |
| `soumis` | PR ouverte, en attente de revue |
| `en revue` | retours reçus, corrections en cours |
| `fusionné` | intégré en amont |
| `refusé` | non retenu — motif consigné, code conservé ici si utile localement |
| `abandonné` | rendu inutile (déjà couvert en amont, ou périmé) |

---

## Tableau de suivi

| Nº | Contribution | Module OpenVigie | Dépôt cible | Texte de PR | État | Dernière action |
|---|---|---|---|---|---|---|
| 1 | Association robuste (remplace `first-match-wins`) | [`association.py`](../src/openvigie/association.py) | `pyro-api` | [`PR1`](upstream/PR1_pyro-api_association.md) | **prêt** | rédigé, non envoyé |
| 2 | Santé sémantique caméra/PTZ (pose bloquée, image périmée) | [`posehealth.py`](../src/openvigie/posehealth.py) | `pyro-engine` | [`PR2`](upstream/PR2_pyro-engine_posehealth.md) | **prêt** | rédigé, non envoyé |
| 3 | Métriques opérationnelles + garde de non-régression | [`opmetrics.py`](../src/openvigie/opmetrics.py) | `pyro-eval` | [`PR3`](upstream/PR3_pyro-eval_opmetrics.md) | **prêt** | rédigé, non envoyé |
| 4 | Collision inter-classes et reproductibilité de jeu | [`dataintegrity.py`](../src/openvigie/dataintegrity.py) | `pyro-dataset` | [`PR4`](upstream/PR4_pyro-dataset_integrity.md) | **prêt** | rédigé, non envoyé |

Les quatre correspondent aux **quatre contributions au meilleur rapport
impact/charge** identifiées par l'audit des faiblesses Pyronear : tracker
robuste, santé réelle caméra/PTZ, gates de reproductibilité, harness
opérationnel multi-objectifs.

---

## Procédure recommandée avant d'envoyer

Ces contributions touchent des parties centrales d'un projet en production
avec des déploiements SDIS actifs. L'ordre suivant limite le risque de
proposer un correctif inadapté :

1. **Ouvrir une issue de discussion**, pas directement une PR — surtout pour
   les nº1 (association) et nº3 (métriques), qui changent des décisions
   structurantes. Y coller la démonstration chiffrée : elle porte l'argument
   mieux qu'une description.
2. **Attendre l'accord sur le diagnostic** avant de proposer du code. Un
   mainteneur peut avoir un contexte qui invalide l'analyse — par exemple un
   correctif déjà en cours, comme cela s'est produit pour le gel du jeu de
   test dans `pyro-dataset` (voir « leçons » ci-dessous).
3. **Proposer ensuite une PR chirurgicale**, à la forme du code cible, sans
   refonte.
4. **Mettre à jour ce tableau** à chaque changement d'état.

---

## Leçons déjà tirées

**Relire le code cible avant d'affirmer qu'il manque quelque chose.** La
première version de PR3 annonçait l'absence de métriques opérationnelles dans
`pyro-eval`. Or les métriques de séquence **et** `avg_detection_delay` y sont
déjà. Le texte a été refait pour partir de l'existant et ne revendiquer que
quatre écarts précis. Envoyée telle quelle, la PR aurait été accueillie par
« on l'a déjà » — et aurait discrédité les suivantes.

**Ne pas ignorer les mécanismes en aval.** La même PR convertissait un FPR par
image en alertes par jour, en oubliant que le moteur applique un lissage
temporel à vote majoritaire. La conversion surestimait donc massivement. La
correction s'est révélée plus intéressante que l'erreur : le filtre écrase le
scintillement (5 % de présence → 0,04 % de survie) mais laisse passer la
persistance (80 % → 99 %), et ce sont les faux positifs persistants qui coûtent
cher. La métrique utile n'est donc pas le FPR mais sa **fraction persistante**.

**Vérifier l'état réel du code cible avant de proposer.** L'audit signalait un
jeu de test instable entre deux constructions ; en clonant `pyro-dataset`, on
constate qu'un mécanisme de lockfile a depuis été ajouté (« the lockfile IS the
test FP selection »). Proposer ce correctif aurait fait perdre du temps à tout
le monde. La partie restée ouverte — la collision **inter-classes**, distincte
de la fuite entre splits — a été retenue à la place.

**Distinguer ce qui se ressemble.** `test_data_leakage.py` est paramétré *par*
catégorie : chaque classe est vérifiée séparément contre les splits, donc
`wildfire` et `fp` ne sont jamais croisés entre eux. Un contrôle de fuite et un
contrôle de collision se ressemblent et ne se recouvrent pas.

**Démontrer plutôt qu'affirmer.** Chaque contribution embarque un test qui
rejoue le défaut côte à côte : logique actuelle d'abord, logique proposée
ensuite, sur les mêmes données. Sans ce contraste, « c'est plus robuste »
n'est qu'une opinion.

---

## Limite commune, à dire dans chaque PR

Aucune de ces briques n'a été confrontée à des **données de production** : les
scénarios de validation sont reconstitués à partir de descriptions
d'incidents, pas rejoués sur des séquences réelles. Les seuils proposés
relèvent du jugement d'ingénieur, pas de la mesure.

C'est la première chose à faire avant toute proposition ferme, et cela demande
un accès aux données. Les textes de PR le disent explicitement et proposent
de démarrer en mode observationnel.
