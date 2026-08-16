# Contributions prêtes à proposer à Pyronear

*Deux briques développées et validées dans OpenVigie, sans matériel, destinées
à être proposées en amont plutôt que gardées ici. Elles visent des défauts
observés en production, pas des manques théoriques.*

Le principe suivi est `upstream-first` : on démontre le correctif sur un banc
d'essai reproductible, puis on propose un changement **chirurgical** qui épouse
la forme du code cible — pas une refonte.

---

## 1. Association robuste — [`association.py`](../src/openvigie/association.py)

### Le défaut

Dans `pyro-api`, l'association d'une détection à une séquence
([`detections.py`](https://github.com/pyronear/pyro-api/blob/main/src/app/api/api_v1/endpoints/detections.py))
combine trois décisions qui s'aggravent mutuellement :

```python
candidate_sequences = await sequences.fetch_all(
    ...,
    order_by="last_seen_at", order_desc=True,      # (1) ordre par RÉCENCE
)
for seq in candidate_sequences:
    last_bbox = await _get_last_bbox_for_sequence(detections, seq.id)   # (3) DERNIÈRE boîte
    if last_bbox is not None and _bboxes_overlap(last_bbox, det_bbox, tol):  # (2) test BOOLÉEN
        matched_sequence = seq
        break                                       # premier match gagne
```

1. **l'ordre d'examen est la récence**, sans rapport avec la qualité spatiale ;
2. **le test est booléen** — `_bboxes_overlap` accepte même un écart négatif
   jusqu'à `SEQUENCE_BBOX_TOLERANCE` : un chevauchement d'un pixel vaut autant
   qu'un recouvrement parfait ;
3. **l'identité spatiale de la séquence est sa dernière boîte**, donc une seule
   boîte aberrante la déplace durablement.

Avec `SEQUENCE_RELAXATION_SECONDS = 7200` (deux heures), une intersection
fortuite peut relier des épisodes sans rapport.

**Conséquence observée** : deux feux sur la même pose, une boîte anormalement
grande sur l'un, et cette boîte absorbe les détections de l'autre pendant 2 h 30
— l'opérateur voit les images d'un feu à la position d'un autre. Le vol est
irréversible : la séquence volante s'élargit, donc chevauche encore plus.

### Le correctif

| Décision | Avant | Après |
|---|---|---|
| Ordre | récence | tous les candidats évalués |
| Comparaison | booléenne | qualité continue (IoU, distance des centres, rapport de tailles, écart temporel) |
| Choix | premier qui passe | meilleur score |
| Égalité | premier arrivé | **refus explicite** → nouvelle piste |
| Identité de piste | dernière boîte | **médiane** des N dernières |
| Boîte géante | acceptée | rejetée au-delà d'un rapport de surface |
| Écart temporel | jusqu'à 2 h | découpage en épisodes |

### Démonstration

Rejeu du scénario, mêmes données pour les deux logiques
(`tests/test_upstream_contributions.py::TestRejeuIncidentProduction`) :

```
détection appartenant visiblement à A : (0.11, 0.11, 0.18, 0.18)
  logique historique  -> B        ← vol
  logique proposée    -> A | meilleur score 0.778
    A: qualité=0.778 iou=0.581 rejet=None
    B: qualité=0.000 iou=0.000 rejet=déplacement du centre de 0.60 : trop rapide
```

La médiane protège aussi l'identité : dans ce scénario, `last_box` de B couvre
plus de 90 % de l'image, mais `reference_box` reste sous 5 % — le vol cesse
d'être irréversible.

### Forme de la PR proposée

Le point d'entrée de `pyro-api` traite les détections **une par une** : la
contribution utile n'est donc pas l'affectation globale (`assign_batch`, utile
côté OpenVigie où les détections arrivent par lot), mais la fonction de score
et le remplacement du `break`.

PR minimale, sans changement de schéma ni de base :

1. ajouter un module pur `app/services/association.py` (score + garde) ;
2. remplacer la boucle `for … break` par « évaluer tous les candidats, garder
   le meilleur, refuser en cas d'égalité » ;
3. exposer les seuils en configuration, avec les valeurs actuelles par défaut
   pour que le comportement reste inchangé tant qu'on ne les active pas ;
4. ajouter le test de rejeu.

Les seuils recommandés (`max_gap_s`, `max_area_ratio`, `ambiguity_margin`) sont
volontairement conservateurs : **en cas de doute, ouvrir une nouvelle piste**.
Un doublon coûte une vérification à l'opérateur ; une association erronée lui
montre un feu à la mauvaise position.

---

## 2. Santé sémantique caméra/PTZ — [`posehealth.py`](../src/openvigie/posehealth.py)

### Le défaut

Deux pannes rendent une **zone aveugle invisible** — pire qu'une panne franche,
puisque personne ne va la corriger.

**Tête PTZ bloquée.** Les presets sont commandés, la tête ne bouge pas
(mécanique grippée, moteur non alimenté, commande refusée en silence), et la
caméra continue de renvoyer des images parfaitement valides. Le flux répond,
l'inférence tourne, rien ne signale la panne — mais toutes les poses montrent
la même scène. Les azimuts attribués aux détections deviennent faux, les
alertes se dupliquent pose après pose, et la surveillance se limite à une
direction.

**Caméra hors ligne déclarée vivante.** Dans `pyro-engine`,
`_safe_get_latest_image` renvoie une image sans aucun contrôle de fraîcheur. Si
la source resert le dernier cliché réussi, une caméra déconnectée paraît active
indéfiniment.

### Le correctif

**Empreinte perceptuelle par pose** (dHash, NumPy pur). Si deux poses censées
regarder des directions différentes produisent des images quasi identiques, la
tête n'a pas bougé.

Le dHash est choisi pour un compromis précis : il compare chaque pixel à son
voisin, donc **une transformation affine de la luminosité laisse l'empreinte
strictement inchangée** (vérifié en test) — un passage nuageux ne déclenche pas
de fausse alerte de panne — tout en restant sensible à la structure du paysage.
Coût : quelques centaines d'opérations par image.

**Horodatage de capture + durée de validité.** On date la *capture*, pas la
lecture, puis on applique un TTL explicite. Une image plus vieille que sa
fenêtre est déclarée périmée, quelle que soit la réussite de la requête.

```python
reg = PoseFingerprintRegistry("cam-1", ttl_s=900.0)
reg.record("P0", frame, captured_at=stamp)   # à chaque acquisition
rapport = reg.report()                        # à chaque heartbeat
rapport.status     # ok | stuck | stale | degraded
rapport.message    # explication actionnable
```

`drift_since()` mesure en plus une dérive progressive du cadrage — vent,
maintenance, fixation qui bouge — **avant** qu'elle ne devienne une collision.

### Forme de la PR proposée

Dans `pyro-engine`, la greffe est peu invasive :

1. module pur `pyroengine/posehealth.py` (aucune dépendance nouvelle : NumPy
   est déjà présent) ;
2. dans `SystemController.inference_loop`, un appel `record()` après chaque
   capture réussie ;
3. dans la boucle de santé, un `report()` joint au heartbeat ;
4. le seuil de collision et le TTL en configuration.

Aucun changement de comportement de détection : c'est un observateur.

### Réglage à valider sur le terrain

`collision_threshold` (0,92 par défaut) dépend du paysage : un horizon très
uniforme — mer, plaine, brouillard — rapproche naturellement les empreintes de
deux directions voisines et demande un seuil plus haut. C'est le paramètre à
mesurer en premier sur un site réel, et la raison pour laquelle il est exposé
plutôt que figé.

---

## Ce que ces deux briques ne font pas

- Elles ne remplacent ni le détecteur, ni le modèle temporel, ni l'API, ni la
  plateforme opérateur.
- Elles n'ont **pas encore été confrontées à des données de production** : les
  scénarios de validation sont reconstitués à partir de descriptions
  d'incidents, pas rejoués sur des séquences réelles. C'est la première chose à
  faire avant toute proposition ferme, et cela demande un accès aux données
  d'une des deux stacks.
- Le seuil de collision et les seuils d'association demandent une calibration
  sur données réelles avant d'être proposés comme valeurs par défaut.
