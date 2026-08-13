# Responsabilité du déployeur

Ce dépôt est un **outil open source**, pas un système certifié, pas un dispositif
homologué, pas un service. Il est fourni sans garantie, conformément à la licence
Apache-2.0.

Toute personne ou organisation qui installe une caméra, exécute ce code et
transmet des alertes assume la responsabilité de son installation. Ce document
n'est pas un avis juridique : il liste les questions à traiter, et ce que le
dépôt fournit pour y aider.

## Ce qui relève du déployeur

**Protection des données et vidéoprotection.** Un zoom 30× identifie personnes,
véhicules et propriétés à plusieurs kilomètres. Selon l'emplacement et le champ,
il peut y avoir analyse d'impact, masques de confidentialité, limitation des
durées de conservation, information du public, ou autorisation administrative.
Ces obligations dépendent du pays, du site et du champ observé.

**Cadre applicable aux systèmes d'IA.** Un système qui répartit ou hiérarchise
l'envoi de secours relève de catégories réglementées dans plusieurs juridictions.
La posture la plus simple et la plus sûre est celle que retiennent d'ailleurs les
services de secours eux-mêmes : **aide à la décision, avec validation humaine de
chaque alerte**. C'est le comportement par défaut du dépôt.

**Interface avec les secours.** Ne transmettez pas d'alertes automatiques à un
service d'urgence sans accord préalable de ce service, sur le format, le débit et
la procédure de levée de doute. Un flux d'alertes non sollicité est une nuisance.

**Sécurité de l'installation.** Caméras exposées sur Internet, mots de passe par
défaut d'OpenIPC, accès SSH : une caméra compromise sur un pylône est un problème
d'infrastructure, pas seulement de vie privée.

**Sécurité physique.** Travail en hauteur, protection foudre, conformité
électrique, autorisation du gestionnaire du pylône.

## Ce que le dépôt fournit pour vous aider

Ces éléments ne rendent aucune installation conforme par eux-mêmes, mais ils
rendent la conformité possible plutôt que d'y faire obstacle :

- **journalisation intégrale** — chaque alerte enregistre son vecteur de features,
  son score, la version du modèle et le tier du pipeline (`AlertStore` en JSONL).
  Sans cela, auditer une alerte manquée six mois plus tard est impossible ;
- **supervision humaine par défaut** — le pipeline produit des alertes, jamais un
  ordre d'engagement ;
- **traçabilité des dégradations** — un backend indisponible est signalé dans les
  logs et dans `summary()`, jamais remplacé en silence ;
- **versionnage explicite du modèle de fusion** — un jeu de poids d'un autre
  schéma est refusé au chargement plutôt que réinterprété ;
- **masques de confidentialité par vue** — champ `masks` de la configuration ;
- **rétention maîtrisée** — le dépôt n'enregistre que ce que vous lui demandez
  d'enregistrer, et les scripts de campagne exposent explicitement ce qu'ils
  conservent.

## Ce que le dépôt ne fournit pas

- aucun poids de modèle entraîné ;
- aucune garantie de performance de détection ;
- aucune certification, homologation ou marquage ;
- aucun engagement de disponibilité.

Les portées, latences et taux de fausses alertes calculés par `openvigie plan` et
`openvigie doctor` sont des **budgets de conception**, à confirmer par la campagne de
mesure de phase 1 sur votre site. Ne les reprenez pas tels quels dans un document
contractuel.
