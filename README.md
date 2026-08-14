# OpenVigie

**Détection précoce de feux de forêt depuis des points hauts** — tours télécom,
pylônes, châteaux d'eau. Projet open source (Apache-2.0), conçu autour
d'**OpenIPC** pour rester indépendant d'une référence de matériel.

```bash
openvigie hw --matrix                          # quelles cartes sont utilisables
openvigie plan   -c config/tiers/medium.yaml   # combien de caméras, quelle portée, quelle revisite
openvigie doctor -c config/tiers/medium.yaml   # le dimensionnement tient-il debout
openvigie viewshed --synthetic                 # ce que le relief laisse voir
openvigie selftest -t medium --mode cloud      # un nuage ne doit jamais alerter
```

> **660 tests**, exécutés dans deux modes : avec OpenCV/SciPy, et **en NumPy pur**
> — ce qui garantit que le cœur tourne sur une carte caméra.

> [!IMPORTANT]
> **Statut : alpha de recherche et d'ingénierie.** OpenVigie est une boîte à outils
> pour *concevoir, dimensionner, simuler et instrumenter* un réseau de caméras de
> détection. Ce n'est pas encore un système de surveillance autonome : il n'y a
> pas d'agent continu (`openvigie run`), pas de modèle de détection livré, pas de
> validation sur fumées réelles, et aucune performance de détection n'a été
> mesurée sur le terrain.
>
> **Utilisable aujourd'hui** : étude optique et de couverture, vérification de
> configuration, simulation du pipeline, préparation de MNT et d'étalonnage,
> campagne de collecte de négatifs, développement d'algorithmes et de pilotes.
>
> **Pas encore** : fonctionnement 24 h/24 sans surveillance, alerte
> opérationnelle, liaison à un service de secours.
>
> `openvigie capabilities` affiche, pour une configuration donnée, ce qui fonctionne
> réellement. La [roadmap](ROADMAP.md) distingue trois niveaux : code de
> bibliothèque, intégration de bout en bout, validation terrain.

---

## Pourquoi maintenant, et comment aider

Depuis janvier 2026, plus de 87 000 hectares ont brûlé en France — un niveau
sans précédent depuis vingt ans de mesures satellite ([Copernicus
EFFIS](https://effis.jrc.ec.europa.eu/)). Un feu a atteint la forêt de
Fontainebleau, à moins de 60 km de Paris. Le feu de Saumos, en Gironde, a brûlé
plus de 32 000 hectares et entraîné l'évacuation de 167 000 personnes. Au
moins trois sapeurs-pompiers sont morts en intervention depuis le début de
l'été — un chiffre resté à zéro l'année précédente. Ce n'est pas propre à la
France : juin et juillet 2026 forment la période la plus chaude jamais
enregistrée en Europe de l'Ouest
([Copernicus](https://climate.copernicus.eu/copernicus-highest-july-global-ocean-surface-temperatures-exceptionally-hot-dry-conditions-fuel)),
et une étude d'attribution publiée fin juillet conclut que le changement
climatique a rendu la sévérité du risque observé cet été en Gironde et dans
les Landes au moins deux fois plus probable ([World Weather
Attribution](https://www.worldweatherattribution.org/climate-change-increases-likelihood-of-compounding-drivers-of-severe-wildfire-conditions-in-france-and-spain/)).

Ce projet ne s'attaque qu'à un seul maillon de ce problème : réduire le délai
entre l'ignition d'un feu et sa détection. Pas la prévention, pas la lutte,
pas la coordination des secours — ces sujets existent déjà, portés par des
acteurs mieux placés pour ça (SDIS, ONF, DFCI, Pyronear). Détecter plus tôt
change ce qu'un départ de feu devient : une intervention au sol plutôt qu'un
embrasement. Et c'est l'un des rares leviers qui ne dépendent ni d'un budget
public ni d'un calendrier politique — seulement de code, de données de
terrain, et de temps.

Ce dernier point est concret : ce projet avance avec le temps disponible en
dehors du reste, pas à plein temps — la situation ci-dessus n'attend pas que
ça change. D'où cette organisation, pensée pour que chacun contribue à la
mesure de ce qu'il peut vraiment donner, sans que ça devienne une seconde
charge :

- **Quelques heures, ponctuellement** — signaler un bug, tester une
  configuration, relire une page de documentation. Une simple issue suffit ;
  débutants bienvenus.
- **Un week-end de temps en temps** — porter un module OpenIPC, instrumenter
  un site de mesure, documenter des faux positifs. Voir
  [CONTRIBUTING.md](CONTRIBUTING.md).
- **Une expérience du terrain, sans écrire de code** — sapeur-pompier,
  forestier, gestionnaire de tour ou de pylône : ce que vous savez des faux
  positifs locaux ou de ce qui rend une alerte crédible pour un opérateur vaut
  plus que n'importe quelle ligne de code. Ouvrir une issue suffit à démarrer
  la discussion.
- **Une expertise rare** (portage de pilote capteur OpenIPC, calibration
  géométrique, vision par ordinateur, droit du numérique) — c'est la
  ressource la plus recherchée du projet ; la [roadmap](ROADMAP.md) liste ce
  qui bloque concrètement.

Aucune contribution n'est trop petite, et aucune ne suppose une réactivité de
type entreprise : ce projet avance par blocs volontairement indépendants,
justement pour que chacun puisse s'arrêter et reprendre sans rien casser.

---

## Sommaire

- [Pourquoi maintenant, et comment aider](#pourquoi-maintenant-et-comment-aider)

1. [Le parti pris](#1-le-parti-pris)
2. [Pourquoi OpenIPC](#2-pourquoi-openipc)
3. [Le calcul qui commande tout](#3-le-calcul-qui-commande-tout)
4. [Symptômes à détecter](#4-symptômes-à-détecter)
5. [Conception matérielle en trois niveaux](#5-conception-matérielle-en-trois-niveaux)
6. [Le pipeline](#6-le-pipeline)
7. [Du capteur au système](#7-du-capteur-au-système)
8. [Modèles, données, faux positifs](#8-modèles-données-faux-positifs)
9. [Ce qu'il faut exclure](#9-ce-quil-faut-exclure)
10. [Mise en service](#10-mise-en-service)
11. [Installation et usage](#11-installation-et-usage)
12. [Licence et responsabilité](#12-licence-et-responsabilité)

---

## 1. Le parti pris

Ce projet ne prétend pas battre l'état de l'art en détection de fumée. Sur ce
problème, ce n'est pas le détecteur qui décide du succès : c'est la géométrie,
les négatifs du site et le temps de revisite. L'effort est donc mis là.

- **La géométrie commande tout.** Chaque pixel est relié au terrain, donc la
  croissance d'un panache se mesure en **m²/s réels**, pas en pixels. Un seuil
  devient transférable d'un site landais à un site alpin.
- **L'origine au sol est un veto, pas un bonus.** Ce qui est au-dessus de la
  ligne de crête est un nuage, quelle que soit la probabilité du réseau. Ce test
  reste hors du modèle appris, pour être auditable.
- **Le seuil se règle sur les fausses alertes par jour**, jamais sur le F1.
- **Le matériel se mesure avant d'être cru** : répétabilité de preset, vibration
  du mât, encrassement du hublot, destruction du signal par la compression.
- **Le site reste utile seul** : il détecte sans réseau, conserve ses alertes et
  les rejoue au retour du lien.

---

## 2. Pourquoi OpenIPC

Écrire pour un SoC particulier lierait le projet à une référence, un fournisseur
et une génération de puce déjà figée. OpenVigie cible le **firmware** et ne parle
qu'à quatre interfaces stables : `majestic` (flux et snapshot), `cli -g/-s`
(configuration), `ipctool` (inventaire), `/etc/os-release`.

```bash
openvigie hw --soc ssc338q     --sensor IMX335    # → utilisable immédiatement
openvigie hw --soc hi3516av300 --sensor IMX675    # → SoC supporté, pilote à porter
openvigie majestic --host 192.168.1.64            # profil de réglages pour la détection
./scripts/openipc_deploy.sh 192.168.1.64 --apply
```

| SoC | Famille | Accélérateur | IVE | Backend conseillé |
|---|---|---|---|---|
| hi3516av300, hi3516cv500 | HiSilicon CV500 | **NNIE** | oui | `nnie` |
| hi3516ev300, hi3516cv300 | HiSilicon | aucun | oui | `classical` |
| gk7605v100, gk7205v300 | Goke | aucun | oui | `classical` |
| ssc338q, ssc30kq | SigmaStar | aucun | non | `classical` |
| t31, t41 | Ingenic | NPU (SDK propriétaire) | non | `classical` |

**Deux conditions indépendantes** décident si une carte est achetable — les
confondre est la première source d'erreur : le SoC est-il supporté, et le pilote
capteur est-il en amont ?

| Capteur | Pilote OpenIPC |
|---|---|
| IMX307, IMX327, IMX335, IMX415 | **en amont** |
| IMX662, IMX664, IMX675, IMX678, IMX585 (STARVIS 2) | **à porter** |

Le portage IMX675 **n'est pas fait** et ne peut pas l'être sans matériel ni
documentation constructeur. Ce qui existe : la procédure, les paramètres
attendus et un harnais de validation exécutable par qui dispose d'une carte
(`openvigie sensor-validate`). Voir [docs/PORTAGE_IMX675.md](docs/PORTAGE_IMX675.md).

Recommandation : **porter IMX675 + HI3516AV300 en priorité** — cette seule
combinaison couvre les modules fixes 5 MP, les blocs caméra zoom 20×/30×, le NNIE et le NIR
STARVIS 2, et un portage débloque toutes les cartes du même capteur. En
attendant, **IMX335 + HI3516AV300 est opérationnel aujourd'hui** et permet de
développer et valider l'intégralité du logiciel.

### Le profil majestic

Les réglages par défaut d'une caméra de vidéosurveillance détruisent le signal
recherché. Trois en particulier :

- **`.isp.3dnr`** — la réduction de bruit temporelle agressive efface une fumée
  fine en mouvement lent. C'est exactement le signal recherché.
- **`.isp.drc`** — le WDR modifie le mapping tonal image par image, donc le
  modèle de fond voit un changement global à chaque bascule.
- **`.image.mirror`** — invalide la relation colonne → azimut, donc envoie les
  alertes avec un relèvement faux.

Et la règle non négociable : **snapshot JPEG q≥90 (`http://<ip>/image.jpg`),
jamais le flux H.265.** Détails dans [docs/OPENIPC.md](docs/OPENIPC.md).

---

## 3. Le calcul qui commande tout

Un détecteur accroche un panache translucide à partir d'environ **12 px de
largeur**. Pour un capteur au pas de 2,0 µm :

```
taille au sol d'un pixel = (2,0 µm / focale) × distance
panache minimum détecté  = 12 × taille au sol d'un pixel
```

| Focale | Champ H | Vues pour 360° | @3 km | @6,5 km | @11,5 km |
|---|---|---|---|---|---|
| 2,8 mm | 84,7° | 5 | **26 m** | 56 m | 99 m |
| 5,2 mm | 52,9° | 8 | 14 m | **30 m** | 53 m |
| 9,3 mm | 30,3° | 14 | 8 m | 17 m | **30 m** |
| 13,5 mm | 21,7° | 20 | 5 m | 12 m | 20 m |

**Conséquence contre-intuitive : couvrir 360° à 11,5 km demande 14 caméras, pas
8.** Le prix se joue là, pas sur le calculateur. C'est `check_range_budget` qui a
fait tomber la configuration à 8 caméras / 12 km en `FAIL`, pas une intuition.

S'y ajoutent deux effets que le code calcule :

- **l'atmosphère** (Koschmieder) : par visibilité estivale de 20 km, le contraste
  résiduel n'est que de **37 % à 5 km et 14 % à 10 km**. La visibilité est
  estimée en continu à partir d'amers fixes ;
- **l'usure** : 8 presets à 2 min de cycle = **~2,1 millions de mouvements/an**.
  Aucune tête de vidéosurveillance courante ne tient.

D'où l'architecture : **caméras fixes pour détecter, PTZ pour confirmer.**

Et le relief, enfin, décide de ce qu'on peut voir :

```
openvigie viewshed --synthetic --sectors 8
     0.0°    9.9 km  ############################
    45.0°    1.5 km  ####
   225.0°   14.0 km  ########################################
```

Une caméra face à une crête à 1,5 km n'a aucune raison d'être réglée pour 12 km.

---

## 4. Symptômes à détecter

| # | Symptôme | Mesure | Jour | Nuit | Confusables | Poids |
|---|---|---|---|---|---|---|
| 1 | Panache naissant | région translucide nouvelle vs référence | ✅ | ❌ | brume, poussière, pollen | ★★★★★ |
| 2 | **Origine au sol** | base en contact avec le MNT projeté | ✅ | ➖ | — *(discriminant quasi parfait)* | ★★★★★ |
| 3 | Croissance | dA/dt **en m²/s réels** | ✅ | ❌ | cumulus en formation | ★★★★★ |
| 4 | Ascendance | vitesse verticale du centroïde, m/s | ✅ | ❌ | — | ★★★★☆ |
| 5 | Persistance | survie sur ≥3 revisites | ✅ | ✅ | brouillard persistant | ★★★★★ |
| 6 | Perte de contraste | Δ énergie de gradient vs référence | ✅ | ❌ | buée, pluie | ★★★★☆ |
| 7 | Translucidité | corrélation résiduelle avec le fond | ✅ | ❌ | nuages bas | ★★★☆☆ |
| 8 | Cohérence au vent | angle de dérive vs vent observé | ✅ | ✅ | — *(excellent filtre)* | ★★★★☆ |
| 9 | Lueur nocturne | source nouvelle et scintillante | ❌ | ✅ | phares, feux d'artifice, balisage | ★★★☆☆ |
| 10 | Flamme visible | régions fluctuantes 5–15 Hz | ✅ | ✅ | soleil rasant, gyrophares | ★★☆☆☆ |
| — | Braises, scintillement thermique | — | — | — | — | ✗ à ignorer |

**Règle de décision.** Une alerte n'est émise que si :

```
nouveau ∧ origine-sol ∧ persistant(≥3) ∧ croissance>0
        ∧ (ascendance ∨ dérive-cohérente-vent) ∧ P_CNN>seuil
```

`P_CNN` désigne ici le score ROI du détecteur spatial, quel que soit le backend
effectif (`onnx`, `ultralytics`, `nnie` ou `classical`) ; ce score ne devient
jamais une alarme sans persistance, géométrie et fusion calibrée.

La probabilité du réseau ne déclenche **jamais** seule.

---

## 5. Conception matérielle en trois niveaux

| | MINIMAL | MEDIUM | FULL |
|---|---|---|---|
| Rôle | campagne de mesure | surveillance autonome | surveillance opérationnelle |
| Caméras | 1 IMX675 2,7–13,5 mm sur tête pan/tilt + témoin IMX335 | **8 modules fixes** + caméra zoom PTZ de confirmation | **14 modules fixes** + caméra zoom PTZ de confirmation |
| Calcul | dans la caméra | dans les caméras | calculateur externe |
| **Portée honnête** | **8,0 km sur le secteur 140° par défaut** | **6,5 km** | **11,5 km** |
| Revisite | 1,27 min | 0,17 min | 0,1 min |
| Usure de ronde | ~1,66 M mvts/an si balayage continu | **0** pour la détection | **0** pour la détection |
| Coût indicatif du banc/site | ~360 $ | ~2 370 $ | ~3 694 $ |

Le préréglage `minimal` est désormais réellement sectoriel : 140°/4 positions/8 km. Ce secteur d'exemple doit être remplacé par le viewshed réel.

**Un bloc `SIP-K675A-30X` n'est pas une tête PTZ.** C'est une caméra à zoom
optique destinée à être montée sur une mécanique Pan/Tilt. Le
`SIP-K675A-27135` possède déjà un zoom motorisé 2,7–13,5 mm, suffisant pour les
focales de détection prévues par les tiers MEDIUM/FULL. Le 20×/30× apporte
surtout une plage téléobjectif pour la confirmation; sans Pan/Tilt, il ne change
pas la direction observée.

Détail, variantes et nomenclature annotée : [docs/HARDWARE.md](docs/HARDWARE.md).

---

## 6. Le pipeline

```
snapshot JPEG q≥90        (jamais le flux H.265 : il détruit la fumée fine)
   ↓ recalage             corrélation de phase, porte anti-vibration
   ↓ modèle de fond       appris par site/vue sur ~30 j de négatifs, sans GPU
   ↓ candidats            différence robuste (MAD) + morphologie + composantes connexes
   ↓ classification       uniquement sur les ROI — coût ∝ candidats, pas ∝ pixels
   ↓ suivi                association IoU + cycle de vie
   ↓ features physiques   m²/s, m/s, origine sol, perte de contraste, cohérence vent
   ↓ fusion               logistique à calibrer par site + veto géométrique
   ↓ hystérésis           entrer en alerte est plus dur qu'y rester
   ↓ événement            azimut, position MNT ou triangulée, ellipse, vignette, séquence
   ↓ file d'attente       durable : une coupure réseau ne perd rien
```

Le coût de calcul est proportionnel au nombre de **candidats**, pas à la surface
de l'image. C'est ce qui permet à la même logique de tourner sur une carte caméra
et sur un Jetson.

### Les tests négatifs comptent plus que les positifs

C'est le taux de fausses alertes qui décide de l'adoption par un service de
secours. Vérifié sur les trois tiers :

- nuage au-dessus de l'horizon → **aucune alerte** ;
- changement global d'exposition (brouillard, bascule WDR) → cycle ignoré ;
- vibration du mât → analyse suspendue ;
- objet opaque stationnaire → rejeté faute de croissance ;
- 60 cycles de scène stable → zéro alerte.

Trois bugs réels ont été attrapés par ces tests pendant l'écriture, dont un
**azimut faux de 20°** sur image sous-échantillonnée — de quoi envoyer les
secours dans le mauvais vallon.

---

## 7. Du capteur au système

Une caméra qui détecte n'est pas encore utile aux secours. La valeur
opérationnelle vient de la localisation, du recoupement, de la supervision et de
la validation humaine.

### Géoréférencement par MNT

```python
from openvigie.dem import DEM
from openvigie.pipeline import view_maps_from_dem

dem = DEM.from_npy("mnt.npy")
dmap, horizon = view_maps_from_dem(dem, cfg, azimuth, focal_mm)
pipe.register_view("V00", azimuth, focal_mm, distance_map=dmap, horizon_rows=horizon)
```

Ray-casting réel, courbure terrestre et réfraction comprises (~6,7 m à 10 km).
Préparation des dalles IGN : [docs/GEOREFERENCEMENT.md](docs/GEOREFERENCEMENT.md).
Trois apports que rien ne remplace : distance vraie par pixel donc surface en m²,
**ligne d'horizon qui suit les crêtes** — donc veto « origine au sol » bien plus
discriminant en relief —, et viewshed pour ne pas gaspiller de caméras sur ce que
le relief masque.

### Événement canonique

Figé tôt, volontairement **neutre** : aucun format métier externe n'entre dans le
cœur. Un adaptateur se branchera dessus le jour où un partenariat le justifiera.

```bash
openvigie schema     # cycle de vie et typologie des décisions opérateur
```

```
candidate → confirmed → transmitted → acknowledged → operator_validated → closed
                                                   ↘ operator_rejected  → closed
```

Chaque motif de rejet (`prescribed_burn`, `dust`, `pollen`, `industrial`,
`optical_artifact`…) devient une classe de négatifs pour le réentraînement.

L'événement porte son **ellipse d'incertitude** : prétendre à une précision
inexistante est le plus sûr moyen de perdre la confiance d'un opérateur.

### Connectivité

```bash
openvigie outbox --dir data/outbox           # état de la file
openvigie outbox --dir data/outbox --flush
```

File durable (écriture atomique, idempotente), réémission à intervalles
croissants plafonnés, abandon borné, saturation qui sacrifie les **plus
anciennes** entrées, tolérance aux fichiers corrompus. Battements de cœur avec
santé par caméra : un site silencieux est indistinguable d'un site sans feu.

**Aucune caméra n'est jamais exposée** : la passerelle sort, la plateforme
n'entre jamais. Voir [docs/CONNECTIVITE.md](docs/CONNECTIVITE.md).

### Étalonnage géométrique par trafic aérien *(option)*

Une caméra de guet regarde l'horizon ; un avion de croisière y est **entre 19° au
-dessus de l'horizon à 30 km et 2° à 200 km**, et diffuse sa position en clair.
Chaque passage est une mire de calibration gratuite.

```bash
openvigie calibrate -t full --simulate          # validation de bout en bout, sans matériel
```

| Grandeur | Boussole + niveau | Après étalonnage |
|---|---|---|
| Azimut | ±2° | **±0,005°** |
| Assiette | ±0,5° | **±0,003°** |
| Décalage d'horloge | inconnu | **mesuré à ±0,05 s** |

Le gain principal n'est pas celui qu'on attend. Ce n'est pas l'azimut, c'est
**l'assiette, donc la portée estimée** : pour une caméra dominant son terrain de
100 m, une erreur d'assiette de 0,5° produit **44 % d'erreur de distance à 5 km**,
contre 0,6 % après étalonnage. Comme l'ellipse d'une tour seule est dominée par
le terme de distance, c'est là que se joue l'essentiel. Sur la triangulation, le
gain d'azimut réduit l'ellipse de 377 m à 5 m à 8 km.

Et l'usage le plus rentable au quotidien : **la détection de dérive**. Un preset
qui a glissé se voit ici, pas sur une alerte mal localisée.

Un récepteur ADS-B local (clé SDR ~25 €) est préférable à une API : horodatage
meilleur, et l'étalonnage continue quand la liaison est tombée. Pièges traités et
limites : [docs/CALIBRATION.md](docs/CALIBRATION.md).

### Modes d'exploitation

Un site ne peut pas alerter par accident. Le mode est explicite et vérifié :

| Mode | Détection | Événement | Transmission |
|---|---|---|---|
| `measure` | ✅ | ✗ | ✗ |
| `shadow` | ✅ | ✅ journalisé localement | ✗ |
| `alert` | ✅ | ✅ | ✅ **si** le modèle de fusion est calibré |

Les poids de fusion livrés sont explicitement provisoires ; le mode `alert` les
refuse, sauf dérogation `operating.allow_uncalibrated_alerts` — consciente et
tracée dans le résumé comme dans chaque événement. C'est la phase de mesure
recommandée plus bas, transformée en verrou logiciel plutôt qu'en phrase de
documentation.

Ici, **modèle de fusion calibré** signifie : coefficients `fitted=true` appris
sur les événements validés/rejetés du site, et seuil choisi sur le budget de
fausses alertes/jour mesuré sur les négatifs réels. Ce fit tient sur CPU ; la
charge lourde éventuelle est celle du détecteur spatial, pas de la fusion.

### Dimensionnement par secteurs utiles

```bash
openvigie sectors -c site.yaml --from-viewshed data/mnt/site.npy
```

La planification ne suppose plus une couverture 360° : elle part des secteurs
que le relief rend réellement exploitables, et compare les architectures qui
peuvent les couvrir. Le résultat change les conclusions — **le balayage PTZ,
inexploitable à 360° (21 positions, plus de 5 min de cycle), redevient
raisonnable sur 140° utiles (4 positions, 1,3 min)**.

Trois arbitrages que le calcul impose :

- la **tête PTZ (~1 450 $) domine tout budget PTZ** : un anneau de quatre
  modules fixes coûte moins de la moitié d'un module PTZ, sans latence ni usure ;
- mais le matériel n'est pas le coût dominant d'un site réel — mât, câblage et
  main-d'œuvre en hauteur dépendent du **nombre d'appareils**, et c'est là que
  le module unique se défend ;
- **grand-angle + PTZ ne voit pas plus loin** : la portée est celle du
  grand-angle, la PTZ ne fait que lever le doute. L'intuition inverse est
  fréquente et coûteuse.

### Relevé d'installation

```bash
openvigie survey --lat 44.0 --lon 3.0 --altitude 500 --height 40 \
                 --azimuth 85 --declination 2.1 --tilt 1.4 --mounting steel_tower
```

Quelques minutes au smartphone à la pose donnent une pose de départ. La
répartition des incertitudes est très inégale, et c'est ce qui la rend utile :

| Grandeur | Incertitude | Pourquoi |
|---|---|---|
| Assiette, roulis | **±0,5°** | l'accéléromètre mesure la gravité, que rien ne perturbe |
| Position | ±5 m | GNSS ordinaire |
| **Azimut** | **±15°** sur pylône treillis | le magnétomètre subit l'acier |

Soit un facteur trente entre les deux axes. **Exactement complémentaire de
l'étalonnage par trafic aérien**, qui excelle sur l'azimut et peine sur le
reste. Et l'assiette est la grandeur qui commande la portée estimée.

La déclinaison magnétique est **obligatoire** : un smartphone donne le nord
magnétique, et l'oublier introduit un biais de 1 à 3° en France — du même ordre
que ce que le relevé prétend mesurer.

### Multi-tours

```python
clusters = corr.cluster(events)            # déduplication + triangulation
event    = corr.promote(clusters[0])       # un seul événement pour l'opérateur
tasks    = corr.confirmation_tasks(event)  # tours à faire pointer immédiatement
```

Le saut d'une tour à deux apporte la triangulation, une confirmation indépendante
et de la redondance. Le saut de deux à vingt n'apporte que de la surface. C'est
le meilleur rendement du projet après le détecteur lui-même.

---

## 8. Modèles, données, faux positifs

### Modèles

| Composant | Premier choix | État | Travail à prévoir |
|---|---|---|---|
| Modèle de fond | médiane glissante par vue × heure × saison | **À apprendre par site** | 30 jours de `record_baseline.py`, CPU seulement ; rien à télécharger |
| Détection spatiale | poids Pyronear [`yolo11s_sensitive-detector`](https://huggingface.co/pyronear/yolo11s_sensitive-detector), export ONNX disponible | **Prêt à tester** | télécharger/épingler les poids ; calibrer seuil et fusion sur les négatifs du site avant tout mode `alert` |
| Alternative sans AGPL | [RTMDet-tiny](https://mmyolo.readthedocs.io/en/dev/recommended_topics/algorithm_descriptions/rtmdet_description.html), [D-FINE](https://github.com/Peterande/D-FINE), [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX), [NanoDet](https://github.com/RangiLyu/nanodet) | **Préentraînés COCO, pas prêts fumée** | fine-tuner sur Pyro-SDIS + négatifs du site, puis exporter ONNX/INT8 ; charge typique : quelques heures de GPU 8–16 Go pour un nano/tiny |
| Déploiement NNIE | modèle spatial converti pour HI3516AV300 | **À convertir** | export + quantification INT8/INT16 avec échantillons réels ; binaire NNIE non livré |
| Temporel | CNN+LSTM ou Temporal Shift Modules | **À entraîner, non raccordé au pipeline** | séquences PyroNear-2025 + séquences du site ; GPU recommandé, à activer après validation du spatial |
| Segmentation (tier FULL) | modèle distillé temps réel, **sur candidat uniquement** | **À entraîner/distiller, non raccordé au pipeline** | pseudo-labels hors ligne puis distillation ; coût principal = annotation/QA, pas inférence |
| Fusion d'alerte | régression logistique OpenVigie | **À calibrer par site** | `fit_logistic` sur décisions opérateur + seuil FP/jour ; CPU, minutes |
| Embarqué sans NPU | étage classique, aucune dépendance ML | **Prêt comme baseline** | pas d'entraînement ; seuils à calibrer, rappel limité |

Le dépôt ne versionne aucun poids : même un modèle prêt à tester est un artefact
externe à télécharger et à tracer via `model_version`. En pratique,
**fine-tuner** signifie partir d'un poids COCO/Pyronear, entraîner sur
[Pyro-SDIS](https://huggingface.co/datasets/pyronear/pyro-sdis) plus les
négatifs du site, puis exporter vers le backend choisi (`onnx`, `ultralytics` ou
`nnie`). Cette charge appartient au poste de développement ou au cloud GPU, pas à
la caméra.

Le cœur du sujet : ce n'est pas le backbone qui décide. PyroNear-2025 montre un
F1 inter-datasets de l'ordre de 60–70 % — des modèles qui « font 90 % » sur les
jeux classiques s'effondrent sur des données réelles diversifiées. **Un YOLO nano
bien calibré avec 100 000 vrais négatifs de vos sites battra un transformeur SOTA
entraîné sur des jeux web.**

### Données

Le dépôt ne contient aucun poids. Jeux publics et licences :
[docs/DONNEES.md](docs/DONNEES.md). En tête, **Pyro-SDIS** (Apache-2.0, images de
caméras installées avec des services d'incendie français), puis PyroNear-2025
pour les séquences temporelles.

Vous n'avez pas besoin de plus d'images de fumée : il en existe des centaines de
milliers en accès libre. Vous avez besoin de **négatifs de vos sites**, et
ceux-là n'existent nulle part.

### Faux positifs français

| Saison | Source | Parade |
|---|---|---|
| Nov–avril | **Brûlages dirigés, écobuage** | calendrier + géofencing des parcelles. FP n°1 |
| Nov–mars | sarments, déchets verts | classe dédiée |
| Avril–juin | **pollen de pin** (Landes, Sologne) | teinte, absence d'origine ponctuelle |
| Juillet–août | **poussière de moissonneuse** | origine mobile linéaire + parcellaire |
| Été | cumulus de convection, brume de chaleur | **test d'origine au sol** |
| Toute l'année | brouillard de vallée, brume marine | modèle de fond horaire par vue |
| Permanent | aéroréfrigérants, scieries, cimenteries | masques fixes par vue |
| Nuit | **feux d'artifice**, phares, balisage éolien | calendrier + classe nuit dédiée |
| Permanent | toiles d'araignée, gouttes, givre, buée | contrôle optique + alerte maintenance |
| Permanent | vibration du pylône | porte anémométrique + recalage |

**Boucle d'apprentissage** — le mécanisme à plus fort retour : chaque
invalidation motivée part dans le jeu de négatifs du site, réentraînement
mensuel, seuils recalibrés par site après 3–4 semaines.

---

## 9. Ce qu'il faut exclure

| Technique | Motif |
|---|---|
| Classification image entière feu/pas-feu | panache = 0,1–2 % de la scène |
| YOLO mono-frame seul comme alarme | détecteur spatial seulement : aucune persistance ni fusion calibrée |
| Seuils RGB/gris, seuil NIR seul | NIR ≠ thermique |
| Mouvement / flux optique / MOG2 **seuls** | générateurs de candidats, pas classifieurs |
| Détecteur de flamme seul | le feu est masqué avant que la fumée ne le soit |
| Braises, scintillement thermique à longue portée | sous la limite de résolution |
| VLM/MLLM comme détecteur primaire | localisation médiocre des fumées précoces |
| Segmentation fondation en continu | surdimensionné ; utile hors ligne pour pseudo-labels avant distillation |
| **Détection pendant le mouvement PTZ** | détruit fond et flux |
| **Différence de fond sans recalage** | bords fantômes partout |
| **Analyse sur flux H.265** | détruit le signal de faible contraste |
| **Split aléatoire par frame** | fuite entre images d'un même incendie |
| Seuil global unique jour/nuit | distributions incomparables |
| Modèle global sans calibration par site | le seuil varie d'un facteur 2–3 entre sites |
| **3DNR agressif** | efface la fumée fine en mouvement lent |
| Streaming vidéo permanent vers un serveur | ~11 Go/jour et par tour, pour rien |
| **Déclenchement automatique des secours** | l'humain reste dans la boucle |

---

## 10. Mise en service

| Phase | Durée | Contenu | Critère de sortie |
|---|---|---|---|
| **1. Mesure** | 2–3 mois | `site_survey.py` puis `record_baseline.py` 24/24. **Aucune détection active.** | portée réelle mesurée + 30 jours de négatifs du site |
| **2. Détection** | 3–4 mois | détecteur finetuné, seuil calibré sur FP/jour et non sur F1 | < 1 FP/caméra/jour, détection sur brûlages dirigés |
| **3. Réseau** | 6+ mois | 2ᵉ tour, triangulation, portail de validation, boucle d'apprentissage | adoption par les opérateurs |

**Sauter la phase 1 revient à deviner tous les seuils.**

Métriques à défendre : fausses alertes/caméra/jour (< 1 de jour, < 0,2 de nuit),
P(détection ≤ 5 min) à 5 et 10 km, erreur de localisation, disponibilité.
Découpage **par incendie ET par site ET par saison** — jamais aléatoire par
image. Vérité terrain d'ignition : les **brûlages dirigés** de novembre à mars,
dont l'heure d'allumage est connue à la minute, et qui sont gratuits.

La suite : [ROADMAP.md](ROADMAP.md).

---

## 11. Installation et usage

```bash
git clone https://github.com/doxav/OpenVigie.git && cd OpenVigie
./scripts/bootstrap.sh          # poste de développement
./scripts/bootstrap.sh --edge   # cible embarquée : NumPy + PyYAML seulement
```

Le cœur ne dépend que de **NumPy et PyYAML**. OpenCV et SciPy sont optionnels et
ne font qu'accélérer.

```bash
# dimensionnement
openvigie plan -t full                    openvigie doctor -c site.yaml
openvigie viewshed --dem mnt.npy          openvigie hw --matrix

# exploitation
openvigie sectors -c site.yaml            # secteurs utiles, architectures comparées
openvigie survey  --declination 2.1 ...   # relevé d'installation
openvigie capabilities -c site.yaml       # ce qui fonctionne VRAIMENT
openvigie schema                          openvigie outbox --dir data/outbox
openvigie calibrate -t full --simulate    # étalonnage par trafic aérien
openvigie majestic --host 192.168.1.64    openvigie selftest -t medium --mode cloud

# terrain
./scripts/openipc_deploy.sh <ip> --apply
python scripts/site_survey.py --config site.yaml --snapshot-url http://<ip>/image.jpg
python scripts/record_baseline.py --camera V00=http://<ip>/image.jpg --days 30
```

```bash
make test-all      # avec ET sans OpenCV/SciPy — ce que doit passer toute contribution
```

### Modules

| Module | Rôle |
|---|---|
| `geometry` | optique, couronne, budget de balayage, Koschmieder, triangulation |
| `dem` | ray-casting MNT, courbure, viewshed, intersection sol |
| `platform` | matrice OpenIPC, détection SoC/capteur, adaptateur majestic |
| `registration` | corrélation de phase, répétabilité de preset, vibration |
| `background` | banque de fonds par vue × heure × saison × jour/nuit |
| `candidates` | seuillage MAD, morphologie, perte de contraste, translucidité |
| `tracking` | suivi IoU, features physiques en unités réelles |
| `scoring` | logistique, apprentissage, seuil par budget de FP/jour, hystérésis |
| `detectors` | `classical` prêt / `onnx` poids fourni par l'utilisateur / `ultralytics` optionnel AGPL / `nnie` binaire converti par l'utilisateur |
| `events` | schéma canonique, cycle de vie, incertitude, décisions opérateur |
| `transport` | file durable, transports, battement de cœur, santé |
| `correlation` | déduplication, triangulation, sollicitation PTZ |
| `calibration` | pose caméra, ADS-B, ajustement robuste, dérive |
| `masking` | masques de confidentialité, appliqués à l'acquisition |
| `modules` | secteurs utiles, comparaison d'architectures |
| `survey` | relevé d'installation, amorce de calibration |
| `ptz` | trames Pelco-D, ordonnanceur, avertissements d'usure |
| `alerting`, `pipeline`, `config`, `sources`, `hwcheck`, `cli`, `compat` | — |

---

## 12. Licence et responsabilité

Cœur sous **Apache-2.0**. Un greffon optionnel s'appuie sur Ultralytics
(AGPL-3.0) et n'est jamais installé par défaut :
[docs/LICENCES.md](docs/LICENCES.md).

Ce dépôt est un outil, pas un système certifié. Le déployeur reste responsable de
la conformité de son installation — protection des données, cadre applicable aux
systèmes d'IA, autorisations locales, interface avec les services de secours. Ce
que le code fournit pour la rendre possible : journalisation intégrale,
supervision humaine par défaut, traçabilité des dégradations, versionnage des
modèles. Voir [docs/RESPONSABILITE.md](docs/RESPONSABILITE.md).

### Contribuer

Voir [« Pourquoi maintenant, et comment aider »](#pourquoi-maintenant-et-comment-aider)
en tête de ce document, et [CONTRIBUTING.md](CONTRIBUTING.md) pour les détails
pratiques.

---

*Prix indicatifs en USD, hors TVA, port, caissons, PoE et parafoudre ; relevés fin juillet / début août 2026, à revérifier.*
*Chiffres optiques et de balayage calculés par le dépôt, à confirmer par mesure terrain (phase 1).*
*Résultats de publications à revérifier dans les articles avant tout usage contractuel.*
