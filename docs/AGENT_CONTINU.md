# Agent continu `openvigie run` — analyse et spécification du MVP

## Décision

**Oui, l'agent continu peut être développé maintenant.** Le dépôt possède déjà
les primitives coûteuses et risquées : acquisition, pipeline avec état par vue,
géométrie, modes d'exploitation, stockage local, outbox durable, transport et
heartbeat. Ce qui manque est une couche d'orchestration qui relie ces contrats
pendant plusieurs jours et rend les défaillances visibles.

Cette conclusion ne signifie pas qu'OpenVigie est prêt à émettre des alertes aux
services de secours. L'agent rend le logiciel autonome ; il ne remplace ni un
modèle entraîné, ni la calibration des seuils sur les négatifs du site, ni la
validation terrain. Les verrous `measure` / `shadow` / `alert` restent donc
inchangés.

## 1. Analyse systématique de faisabilité

### 1.1 Primitives disponibles et écarts réels

| Besoin d'un démon | Primitive existante | Écart avant le MVP | Verdict |
|---|---|---|---|
| Lire des images | `FrameSource`, snapshot HTTP, RTSP, fichiers | choisir et configurer les sources par caméra | assemblage simple |
| Conserver un état par vue | `DetectionPipeline.register_view()` | enregistrer les vues au démarrage | prêt |
| Détecter sans bloquer sur le réseau | `process_frame()` puis `Outbox.enqueue()` | appeler le pipeline dans une boucle durable | prêt |
| Rejouer après une coupure | `Outbox.flush()` avec backoff et dead letters | déclencher périodiquement le flush | prêt |
| Signaler la santé | `HealthMonitor`, `pipeline.heartbeat()` | cadencer l'appel et signaler les échecs d'acquisition | petit ajout |
| Balayer un PTZ | backends Pelco-D/CGI et `ScanScheduler` | relier preset, stabilisation, vue et acquisition | assemblage borné |
| Arrêt propre | `close()` sur source, PTZ et transport | gérer `SIGINT`/`SIGTERM` et un `finally` unique | petit ajout |
| Reprise après erreur caméra | aucune boucle d'exploitation | backoff borné et recréation de la source | à implémenter |
| Configuration stricte | dataclasses + YAML à clés inconnues refusées | schéma `agent` imbriqué et validé | à implémenter |
| Cache de fond après redémarrage | banque de fond en mémoire | remise en chauffe après redémarrage | non bloquant, hors MVP |
| Cache MNT par vue | calcul disponible, pas de cache persistant | géolocalisation terre plate si aucun cache n'est injecté | non bloquant, hors MVP |
| Modèle validé sur fumées réelles | absent | campagne terrain | bloque l'usage opérationnel, pas l'agent |

### 1.2 Pourquoi ce n'était pas déjà une commande

| Cause | Nature | Conséquence avant le MVP | Traitement retenu |
|---|---|---|---|
| Pas de topologie caméra → vues | configuration | impossible de savoir quelle URL alimente quel azimut | `agent.cameras[].views[]` explicite |
| `None` signifie fin de fichier ou panne réseau selon la source | contrat d'acquisition | une boucle naïve s'arrêterait à la première erreur HTTP | propriété source finie + reprise des sources réseau |
| Aucune politique de temporisation | exploitation | boucle chaude ou martèlement d'une caméra en panne | intervalle normal + backoff exponentiel borné |
| Pas de propriétaire du cycle de vie | exploitation | ressources non fermées, outbox non vidée à l'arrêt | agent propriétaire de toutes les ressources |
| PTZ non relié au pipeline | intégration | risque d'analyser pendant le mouvement ou sous le mauvais preset | mouvement, stabilisation interruptible, puis une acquisition |
| Échecs caméra absents de la santé | observabilité | un site muet peut sembler sain | erreur enregistrée par vue et exposée au heartbeat |
| Chemins relatifs dépendants du répertoire courant | robustesse | états écrits à des endroits différents selon systemd/SSH | résolution relative au fichier de configuration |
| Dépendances JPEG/HTTP optionnelles | packaging | le cœur NumPy seul ne peut pas décoder un snapshot | extra `agent` documenté, erreur de démarrage explicite |

### 1.3 Choix d'architecture Pareto

Notation : 1 = mauvais, 5 = excellent. La simplicité et la robustesse ont un
poids supérieur au débit, car le pipeline est volontairement limité à une image
par vue et par période de revisite.

| Option | Simplicité ×3 | Robustesse ×3 | Testabilité ×2 | Débit ×1 | Total /45 | Décision |
|---|---:|---:|---:|---:|---:|---|
| Script shell appelant une commande par image | 5 | 1 | 2 | 2 | 24 | rejeter : aucun état temporel durable |
| Boucle Python synchrone, état et backoff par caméra | 5 | 5 | 5 | 3 | **43** | **MVP retenu** |
| Threads d'acquisition + pipeline sérialisé | 3 | 3 | 3 | 5 | 29 | différer jusqu'à mesure d'un goulot réel |
| `asyncio` généralisé | 2 | 3 | 3 | 5 | 26 | gain faible avec `requests`, OpenCV et pilotes synchrones |
| Un processus par caméra + bus de messages | 1 | 4 | 2 | 5 | 24 | surdimensionné pour un agent de site MVP |

Le point Pareto est une seule boucle : peu de surfaces de panne, aucun état
partagé entre threads, mémoire bornée, et assez de débit pour des snapshots
espacés de plusieurs secondes. La concurrence ne sera ajoutée que si une mesure
montre que la somme des latences d'acquisition compromet la revisite.

## 2. Périmètre fonctionnel du MVP

| Priorité | Fonction | Comportement d'acceptation |
|---|---|---|
| MUST | sources snapshot HTTP, RTSP et répertoire de fichiers | une configuration décrit chaque caméra et ses vues |
| MUST | caméras fixes multiples | une vue exactement par caméra en mode `fixed` |
| MUST | ronde PTZ | presets explicites, aucune analyse avant `settle_s` |
| MUST | pipeline, outbox et heartbeat raccordés | aucune panne réseau ne bloque l'analyse |
| MUST | reprise caméra | fermeture, recréation et backoff borné après échec |
| MUST | arrêt `SIGINT` / `SIGTERM` | attente interruptible, flush final, fermeture de toutes les ressources |
| MUST | validation stricte et secrets par environnement | aucun mot de passe dans le YAML ou dans les journaux |
| MUST | modes `measure` / `shadow` / `alert` inchangés | l'agent ne contourne jamais le verrou de transmission |
| SHOULD | `--dry-run` | valide la topologie sans ouvrir les caméras ni écrire l'état |
| SHOULD | `--once` et `--max-frames` | tests de recette courts et déterministes |
| SHOULD | résumé final JSON | compteurs d'acquisition, erreurs, statuts pipeline et envois |
| COULD | cache persistant des fonds et cartes MNT | réduit la remise en chauffe, sans changer la justesse du MVP |
| WON'T | entraînement, calibration nocturne, confirmation inter-tours | métiers séparés, non nécessaires à la boucle de site |
| WON'T | démonisation interne, PID file, rotation de logs | responsabilité de systemd/OpenRC et de `journald`/`logrotate` |

## 3. Contrat de configuration

Les chemins `directory`, `network.events_path` et `network.outbox_dir` relatifs
sont résolus par rapport au dossier du fichier `site.yaml`, jamais par rapport au
répertoire courant du service.

### 3.1 Bloc `agent`

| Clé | Type / défaut | Validation | Effet |
|---|---|---|---|
| `alert_log_path` | chemin / `data/alerts.jsonl` | non vide | journal local durable, y compris en mode `shadow` |
| `flush_interval_s` | float / `15` | `> 0` | fréquence maximale des tentatives d'outbox |
| `retry_initial_s` | float / `2` | `> 0` | premier délai après panne source/PTZ |
| `retry_max_s` | float / `120` | `>= retry_initial_s` | plafond du backoff exponentiel |
| `status_interval_s` | float / `60` | `> 0` | cadence du résumé dans les journaux |
| `cameras` | liste / `[]` | identifiants uniques, non vide pour `run` | topologie physique |

### 3.2 Caméra et vues

| Clé caméra | Type / défaut | Validation / secret | Usage |
|---|---|---|---|
| `camera_id` | chaîne | non vide, unique | santé et journaux |
| `source` | `snapshot`, `rtsp`, `files` | valeur fermée | fabrique de `FrameSource` |
| `url` | chaîne | HTTP(S) pour snapshot, RTSP pour RTSP, aucun identifiant embarqué | acquisition réseau |
| `directory` | chemin | requis pour `files` | rejeu fini, principalement recette |
| `pattern` | chaîne / `*.jpg` | non vide | sélection du rejeu |
| `user` | chaîne / `admin` | non secret | authentification caméra |
| `password_env` | chaîne / vide | nom de variable d'environnement valide | mot de passe lu au démarrage |
| `timeout_s` | float / `10` | `> 0` | borne d'une requête snapshot/PTZ |
| `frame_interval_s` | float / `30` | `> 0` | revisite d'une caméra fixe |
| `ptz_backend` | `none`, `cgi`, `pelco_d`, `simulated` | cohérent avec `scan.mode` | pilote de mouvement |
| `ptz_url` | URL | requis pour CGI, sans identifiant embarqué | commande PTZ |
| `serial_port` | chaîne | requis pour Pelco-D | port RS485 |
| `baudrate` | entier / `2400` | `> 0` | débit Pelco-D |
| `address` | entier / `1` | 1…255 | adresse Pelco-D |
| `views` | liste | au moins une, IDs globaux uniques | vues logiques de la caméra |

| Clé vue | Type / défaut | Validation | Usage |
|---|---|---|---|
| `view_id` | chaîne | non vide, unique sur le site | clé de fond, suivi et alerte |
| `azimuth_deg` | float | `[0, 360[` | relèvement central |
| `focal_mm` | float ou `null` | `> 0`, sinon focale optique du site | géométrie |
| `preset` | entier ou `null` | 1…255 ; obligatoire en PTZ | position physique enregistrée |

Règles croisées :

| `scan.mode` | Topologie valide | Topologie refusée |
|---|---|---|
| `fixed` | chaque caméra a une vue et `ptz_backend: none` | plusieurs vues par source, preset ou backend PTZ |
| `ptz` | chaque caméra a un backend PTZ et un preset par vue | backend absent, preset dupliqué ou manquant |

Exemple minimal fixe :

```yaml
agent:
  alert_log_path: data/alerts.jsonl
  flush_interval_s: 15
  retry_initial_s: 2
  retry_max_s: 120
  status_interval_s: 60
  cameras:
    - camera_id: cam-est
      source: snapshot
      url: http://192.168.10.21/image.jpg
      user: root
      password_env: OPENVIGIE_CAM_EST_PASSWORD
      timeout_s: 8
      frame_interval_s: 30
      ptz_backend: none
      views:
        - view_id: V00
          azimuth_deg: 90
          focal_mm: 6.25
```

## 4. Machine d'état d'exécution

| État caméra | Événement | Action | État suivant |
|---|---|---|---|
| initiale | démarrage | construire la source, enregistrer les vues | prête |
| prête | échéance fixe | lire puis traiter une image | prête |
| prête PTZ | échéance | commander le preset | stabilisation ou panne |
| stabilisation | délai `settle_s` écoulé | lire puis traiter une image | attente `dwell_s` |
| toute | `read()` lève ou renvoie `None` réseau | fermer la source, santé en échec, programmer le backoff | panne |
| panne | backoff écoulé | recréer la source | prête ou panne |
| fichier | fin de séquence | marquer la source épuisée sans la rejouer | terminée |
| toute | signal d'arrêt | interrompre l'attente | arrêt |
| arrêt | toujours | flush final puis fermer source/PTZ/transport | terminée |

Backoff après `n` échecs consécutifs :

`min(retry_initial_s × 2^(n-1), retry_max_s)`.

Un succès remet le compteur à zéro. Les caméras ont des compteurs indépendants :
une caméra hors ligne ne doit pas ralentir volontairement les autres au-delà de
la durée bornée de sa tentative d'acquisition.

## 5. Matrice de défaillances

| Défaillance | Détection | Continuité | Trace / santé | Perte admise |
|---|---|---|---|---|
| HTTP non-2xx / timeout | `read()` échoue | autre caméra et maintenance continuent | compteur + note par vue | image du cycle |
| RTSP interrompu | `read()` renvoie `None` | source fermée puis recréée | compteur + backoff | image du cycle |
| PTZ refuse un preset | retour `False` | aucune image analysée sous une pose incertaine | compteur PTZ | visite |
| image invalide / pipeline lève | exception bornée au cycle | boucle continue à l'intervalle normal | compteur traitement | image du cycle |
| transport indisponible | `Outbox.flush()` retourne `retried` | détection inchangée | statistiques outbox | aucune alerte avant saturation |
| outbox saturée | politique existante | détection continue | dead letter + compteur | plus anciennes entrées |
| configuration invalide | validation avant ouverture | démarrage refusé | erreur descriptive | aucune |
| dépendance optionnelle absente | construction source | démarrage refusé | commande d'installation | aucune |
| `SIGTERM` pendant attente | événement d'arrêt | attente interrompue | résumé final | aucune alerte déjà en file |
| coupure électrique | processus tué | reprise par superviseur externe | outbox atomique conservée | fonds mémoire à reconstruire |

## 6. Budget de performance et de ressources

| Ressource | Objectif MVP | Mécanisme | Critère de recette |
|---|---|---|---|
| CPU au repos | pas de boucle active | attente jusqu'à la prochaine échéance | aucune attente nulle répétée |
| Mémoire agent | O(caméras + vues), hors pipeline | compteurs bornés, événements déjà bornés à 500 | pas de liste de frames dans l'agent |
| Latence caméra fixe | intervalle + durée d'acquisition | planification par échéance monotone | dérive non cumulative |
| Latence PTZ | `settle_s + dwell_s` par vue | même budget que `ScanScheduler` | aucune analyse pendant mouvement |
| Réseau sortant | événementiel | snapshots locaux + outbox | aucun flux vidéo remonté |
| Panne caméra | tentative bornée par `timeout_s` | timeout + backoff | pas de martèlement |
| Arrêt | inférieur au plus petit de l'attente et du timeout en cours | attentes interruptibles | ressources fermées et flush final |

La boucle synchrone peut subir un blocage en tête de ligne égal au timeout d'une
caméra. C'est accepté dans le MVP parce que le timeout est borné et que les
revisites sont lentes. Le seuil de passage à des acquisitions concurrentes est
mesurable : p95 de la somme des acquisitions supérieur à 25 % du plus petit
`frame_interval_s` configuré.

## 7. Plan de tests d'acceptation

| Niveau | Cas | Attendu |
|---|---|---|
| unitaire | validation des durées, URLs, secrets, IDs et presets | erreur précise avant I/O |
| unitaire | backoff 2, 4, 8… plafonné | échéances exactes, remise à zéro au succès |
| unitaire | source réseau en panne puis rétablie | recréation, reprise du traitement |
| unitaire | source fichier épuisée | arrêt sans rejeu |
| unitaire | erreur pipeline | cycle suivant exécuté |
| unitaire | refus PTZ | aucune analyse avant nouvelle tentative |
| unitaire | arrêt pendant stabilisation | attente interrompue, ressources fermées |
| intégration | `--dry-run` sur YAML valide/invalide | code 0 / code 2 sans accès caméra |
| intégration | `--once` avec doubles injectés | une tentative par vue, résumé déterministe |
| intégration | événement en mode `alert` et transport défaillant | entrée conservée dans l'outbox |
| non-régression | suite complète avec et sans OpenCV/SciPy | mêmes garanties du cœur |

## 8. Limites assumées après le MVP

| Limite restante | Impact | Priorité suivante |
|---|---|---|
| fonds non persistés | quelques cycles sans protection après redémarrage | cache versionné et atomique |
| cartes MNT non chargées par l'agent | distance terre plate si non injectées | cache MNT par vue avec empreinte de calibration |
| acquisition synchrone | timeout d'une caméra retarde les suivantes | mesurer avant d'ajouter un pool borné |
| pas de capture automatique de preuves | alerte sans séquence avant/après | tampon d'images borné et chiffré |
| pas de boucle ADS-B nocturne | calibration appliquée manuellement | tâche séparée, atomique et révocable |
| aucun modèle livré/validé | pas d'usage d'alerte opérationnelle | données réelles, calibration FP/jour, recette terrain |
