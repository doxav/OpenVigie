# Politique de sécurité

## Signaler une vulnérabilité

Ne pas ouvrir de ticket public. Écrire à l'adresse de sécurité du projet en
décrivant l'impact, les conditions de reproduction et la version concernée.
Une réponse est visée sous 7 jours, un correctif sous 30 jours pour les
vulnérabilités exploitables à distance.

## Périmètre

Ce dépôt est un outil open source, **pas un produit de sécurité certifié**. Il
n'existe pas de version bénéficiant d'un support de sécurité à long terme :
seule la dernière version publiée reçoit des correctifs.

## Ce qui est en place

| Mesure | État |
|---|---|
| Le cœur ne dépend que de NumPy et PyYAML | ✅ |
| Aucun secret dans les fichiers de configuration (jetons lus dans l'environnement) | ✅ |
| Aucune caméra exposée : la passerelle sort, la plateforme n'entre jamais | ✅ par conception |
| Masques de confidentialité appliqués à l'acquisition | ✅ depuis 0.4.0 |
| Validation stricte des clés de configuration | ✅ |
| Journalisation intégrale des alertes et des dégradations | ✅ |
| Transport HTTPS avec jeton porteur | ✅ |
| mTLS, épinglage de certificat, rotation de jeton | ❌ roadmap v0.6 |
| Mises à jour signées, identité d'appareil | ❌ roadmap v0.6 |
| Exécution en compte non privilégié sur la cible | ❌ roadmap v0.4 |

`openvigie capabilities` affiche l'état réel sur une configuration donnée.

## Risques connus, non corrigés

- **Le paquet edge est déposé dans `/tmp`** et disparaît au redémarrage. Une
  installation persistante et supervisée reste à faire (roadmap v0.4).
- **`openipc_deploy.sh` exécute des commandes SSH construites par chaîne.** À
  n'utiliser que sur un réseau de confiance, avec des paramètres maîtrisés.
- **Le jeton porteur est statique.** Pas de rotation automatique.
- **Un HTTP 2xx est traité comme un acquittement.** Un accusé applicatif avec
  identifiant d'événement reste à implémenter.

## Vie privée

Un zoom 30× identifie personnes et véhicules à plusieurs kilomètres. Les
masques de confidentialité (`masks` dans la configuration du site) sont
appliqués **avant** toute analyse, tout stockage et toute transmission. Ils
doivent être définis avant la mise en service, pas après.

Le déployeur reste responsable de la conformité de son installation : voir
[docs/RESPONSABILITE.md](docs/RESPONSABILITE.md).
