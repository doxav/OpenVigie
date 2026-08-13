# Connectivité et réseau

Principe directeur : **le site détecte, la plateforme agrège.** Une tour qui perd
son lien continue de détecter et conserve ses alertes ; elle les rejoue dans
l'ordre au retour du réseau. Une panne de liaison ne doit jamais rendre un site
aveugle, seulement muet.

## Ce qui remonte, et ce qui ne remonte pas

| Remonte | Ne remonte pas |
|---|---|
| Événements de détection (quelques ko) | Flux vidéo permanent |
| Vignette + séquence courte du candidat | Images de fond, images négatives |
| Battements de cœur (santé du site) | Journaux applicatifs complets |
| Décisions opérateur, en retour | Rien d'autre |

L'ordre de grandeur qui tranche : 8 caméras envoyant un JPEG de 500 ko toutes les
30 s représentent **~11 Go/jour et par tour**. Sur une liaison de secours 4G en
été, c'est intenable et inutile — l'analyse a déjà eu lieu sur site.

## Store-and-forward

```python
from openvigie.pipeline import build_outbox, build_transport, DetectionPipeline

outbox    = build_outbox(cfg)       # file durable sur disque
transport = build_transport(cfg)    # file | http | memory | none
pipe = DetectionPipeline(cfg, outbox=outbox, transport=transport)

pipe.process_frame(...)   # détecte, met en file — jamais bloquant
pipe.flush()              # tente d'émettre ce qui est échu
pipe.heartbeat()          # battement si l'intervalle est écoulé
```

Garanties de la file :

- **persistance** — une entrée par fichier, écriture atomique par `rename`. Une
  coupure de courant ne tronque rien ;
- **idempotence** — une même alerte n'est jamais mise en file deux fois ;
- **réémission à intervalles croissants** — 15 s, 30 s, 60 s… plafonnée à 15 min,
  pour ne pas marteler un lien déjà saturé ;
- **abandon borné** — après N échecs, l'entrée part en `dead_letters` et reste
  consultable, plutôt que de bloquer la file indéfiniment ;
- **saturation gérée** — au-delà de `max_queue_entries`, ce sont les **plus
  anciennes** qui sont sacrifiées. Une alerte d'il y a trois jours vaut moins
  qu'une alerte d'il y a trois minutes ;
- **tolérance à la corruption** — une entrée illisible est mise de côté, la file
  continue.

```bash
openvigie outbox --dir data/outbox                    # état de la file
openvigie outbox --dir data/outbox --flush --to out.jsonl
```

## Transports

| Transport | Usage |
|---|---|
| `file` | **défaut.** JSONL local. Un site sans plateforme reste exploitable |
| `http` | POST HTTPS avec jeton porteur, vers une plateforme centrale |
| `memory` | tests |
| `none` | détection seule, aucune remontée |

Le jeton est lu dans l'environnement (`network.token_env`), **jamais** écrit dans
le fichier de configuration : un fichier de site finit toujours par être copié,
versionné ou transmis par message.

## Architecture réseau du site

```
   [ caméras OpenIPC ]          VLAN dédié, aucune route sortante
            │  snapshot HTTP
            ▼
   [ passerelle du site ]       seul équipement à sortir
            │  HTTPS + jeton, sortant uniquement
            ▼
   [ plateforme centrale ]
```

Règles :

- **aucune caméra n'est jamais exposée sur Internet.** Ni redirection de port, ni
  UPnP, ni DDNS. Une caméra OpenIPC avec ses identifiants d'usine sur une IP
  publique est un incident d'infrastructure qui arrivera ;
- la passerelle **sort**, la plateforme n'entre jamais. Pas de port ouvert côté
  site ;
- VLAN caméra isolé, sans accès Internet ;
- pour l'administration à distance : VPN vers la passerelle, jamais vers les
  caméras.

## Horodatage

Toutes les dates sont en **UTC ISO 8601**. Corréler deux tours dont les horloges
sont exprimées en heure locale — avec ou sans heure d'été — produit des erreurs
silencieuses, et la corrélation multi-tours repose entièrement sur le temps.

NTP sur la passerelle, et une horloge temps réel sauvegardée si le site peut
redémarrer sans réseau. Le champ `clock_source` du battement de cœur permet de
savoir ce sur quoi on s'appuie.

## Santé du site

Un site silencieux est indistinguable d'un site sans feu. Le battement de cœur
remonte donc, par caméra : dernière image reçue, images sur la dernière heure,
qualité d'image, propreté de l'optique, amplitude de recalage, maturité du modèle
de fond. Et par site : version logicielle, backend effectivement utilisé,
dégradations, état de la file, espace disque.

```python
snapshot = pipe.health.snapshot()
snapshot.status   # ok | degraded | down
```

Un modèle de fond immature compte comme une dégradation : tant qu'il n'est pas
constitué, la caméra ne protège rien, et il vaut mieux l'afficher que le supposer.

## Multi-tours

```python
from openvigie.correlation import MultiTowerCorrelator, Tower

towers = {
    "A": Tower("A", 44.00, 3.00, max_range_m=12_000, has_ptz=True),
    "B": Tower("B", 44.00, 3.10, max_range_m=12_000, has_ptz=True),
}
corr = MultiTowerCorrelator(towers)

clusters = corr.cluster(events)      # déduplication + triangulation
event    = corr.promote(clusters[0]) # un seul événement pour l'opérateur
tasks    = corr.confirmation_tasks(event)   # tours à faire pointer
```

Trois effets, tous mesurables :

1. **déduplication** — deux tours qui voient le même feu produisent un seul
   événement, pas deux alertes concurrentes ;
2. **triangulation** — l'ellipse d'incertitude passe de plusieurs kilomètres
   carrés à quelques hectares. L'erreur en distance d'un relèvement unique
   domine tout le reste, et c'est exactement ce qu'une deuxième tour corrige ;
3. **sollicitation** — la tour B ne patiente pas jusqu'à son passage naturel :
   elle reçoit un azimut et une distance et y pointe. La fenêtre utile d'un
   départ de feu se compte en minutes.

Le corrélateur refuse une intersection dont les relèvements se croisent à moins
de 5° : mathématiquement définie, opérationnellement instable.

## Ce qu'un site fait sans aucune connexion aux secours

Beaucoup. L'absence d'intégration institutionnelle ne rend pas le système
inutile :

- caractérisation d'un site et construction de ses négatifs réels ;
- surveillance pour un propriétaire forestier, un industriel, un parc
  photovoltaïque ou éolien, une installation télécom ;
- alerte d'une équipe de surveillance locale, par notification ou message ;
- géolocalisation automatique par MNT et confirmation PTZ ;
- historisation et statistiques des secteurs à risque ;
- mesure de visibilité atmosphérique ;
- détection des écobuages et brûlages.

Ce qu'un site ne doit **pas** faire seul : envoyer une alerte automatique à un
service de secours sans accord préalable de ce service sur le format, le débit et
la procédure de levée de doute. Un flux d'alertes non sollicité est une nuisance,
pas un service.

## Modes d'exploitation (0.4.0)

Un site ne peut plus alerter par accident. `operating.mode` :

| Mode | Détection | Événement produit | Mis en file |
|---|---|---|---|
| `measure` | ✅ | ✗ | ✗ |
| `shadow` | ✅ | ✅ journalisé localement | ✗ |
| `alert` | ✅ | ✅ | ✅ si `fusion.fitted` |

Les poids de fusion livrés sont provisoires ; le mode `alert` les refuse, sauf
`operating.allow_uncalibrated_alerts`. La dérogation apparaît dans `summary()`
et dans chaque événement (`features.fusion_calibrated`).

Préréglages : MINIMAL → `measure` (c'est une campagne de mesure), MEDIUM et
FULL → `shadow`. Passer en `alert` est une décision, jamais un défaut.

## Échecs définitifs et saturation (0.4.0)

Les entrées ayant épuisé leurs tentatives, et celles sacrifiées à la saturation,
sont **écrites sur disque** dans `<outbox>/dead/` avec leur motif. Elles
survivent au redémarrage et sont rejouables :

```python
box.replay_dead_letters()      # remet en file après incident
box.stats()["dropped_on_overflow"]
```

Auparavant, elles n'existaient qu'en mémoire — la promesse « aucune coupure ne
perd une alerte » n'était donc pas tenue au-delà d'environ une heure et demie de
panne continue.

## Horodatage : UTC obligatoire

`process_frame` **refuse** un `datetime` naïf. Deux tours dans des fuseaux
différents, ou un simple passage à l'heure d'été, faussaient silencieusement la
corrélation multi-tours — c'est-à-dire ce qui fonde la triangulation.
