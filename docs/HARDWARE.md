# Conception matérielle — trois niveaux

Les portées annoncées ici ne sont pas des estimations commerciales : elles sortent
du calcul optique du dépôt (`openvigie plan`, `openvigie doctor`) et sont vérifiées par
les tests. Elles sont plus modestes que ce qu'annonce la plupart des fiches
produit, pour une raison simple : la fiche produit parle de « voir un panache »,
et nous parlons de « détecter automatiquement un panache naissant de 30 m ».

> [!WARNING]
> **Une caméra « IA feu/fumée » du commerce n'est presque jamais une caméra de
> guet.** Les gammes IA anti-incendie des grands fabricants de vidéosurveillance
> (Dahua, Hikvision, ANNKE) et la plupart des annonces génériques AliExpress
> ciblent un **bâtiment**, pas une forêt : la fiche technique Dahua
> `DHI-HY-SAV849HAP-E` annonce une couverture fumée de **30 à 60 m²**, la
> `DHI-HY-FT121LDP-TD1F4` une portée flamme de **10 m**. Un massif forestier
> se surveille en kilomètres. Le vocabulaire marketing (« IA », « détection
> précoce », « fumée et flamme ») est identique dans les deux cas — seule la
> fiche technique le distingue. Voir [la veille marché](VEILLE_MARCHE.md)
> pour le détail des solutions existantes, aucune achetable rapidement par une
> commune à ce périmètre.

---

## 0. La règle de dimensionnement

Un détecteur accroche un panache translucide à partir d'environ **12 pixels de
largeur**. Pour un capteur au pas de 2,0 µm :

```
taille au sol d'un pixel  =  (2,0 µm / focale) × distance
panache minimum détecté   =  12 × taille au sol d'un pixel
```

D'où le tableau qui commande tout le reste (capteur 5 MP, 2592 × 1944, pas 2,0 µm) :

| Focale | Champ H | Vues pour 360° | Panache min. @3 km | @6,5 km | @11,5 km |
|---|---|---|---|---|---|
| 2,8 mm | 84,7° | 5 | **26 m** | 56 m | 99 m |
| 5,2 mm | 52,9° | 8 | 14 m | **30 m** | 53 m |
| 9,3 mm | 30,3° | 14 | 8 m | 17 m | **30 m** |
| 13,5 mm | 21,7° | 20 | 5 m | 12 m | 20 m |

**Conséquence directe et contre-intuitive :** couvrir 360° à 11,5 km demande
**14 caméras**, pas 8. Le prix se joue là, pas sur le calculateur.

À cela s'ajoute l'atmosphère (loi de Koschmieder) : par visibilité estivale de
20 km, le contraste résiduel n'est que de **37 % à 5 km et 14 % à 10 km**. Les
portées ci-dessous supposent une bonne visibilité ; le code estime la visibilité
en continu à partir d'amers fixes, et une alerte lointaine par temps brumeux doit
être pondérée en conséquence.

---

## 0 bis. Dimensionner par secteurs, pas par tour d'horizon

*Ajouté en 0.6.0 en réponse à l'[issue #1](https://github.com/doxav/OpenVigie/issues/1).*

`openvigie sectors` évalue les secteurs réellement utiles; `plan`, `doctor` et
`ptz-test` doivent utiliser ces secteurs dès qu'ils sont déclarés.

```bash
openvigie sectors -c site.yaml --from-viewshed data/mnt/site.npy
```

### Exemple cohérent : panache de 30 m à 8 km

Avec IMX675 (2,0 µm), la formule du dépôt exige environ **6,4 mm** de focale,
soit ~44,1° de champ horizontal. Avec 15 % de recouvrement, 15 s de dwell et
4 s de stabilisation :

| Ouverture utile | Positions PTZ | Cycle | Attente moyenne avant 1er regard |
|---|---:|---:|---:|
| 90° | **3** | 0,95 min | 29 s |
| 140° | **4** | 1,27 min | 38 s |
| 250° | **7** | 2,22 min | 1,11 min |
| 360° | **10** | 3,17 min | 1,58 min |

Le précédent tableau mélangeait plusieurs focales et donnait notamment
`90° → 2 positions` tout en annonçant 8 km : ces deux hypothèses n'étaient pas
compatibles avec la géométrie du dépôt.

### Ce que coûte réellement chaque architecture

Sur 140° utiles à 8 km, panache de 30 m :

| Architecture | Appareils | Portée | Attente 1er regard | Mouvements/an | Matériel |
|---|---|---:|---:|---:|---:|
| Anneau fixe seul | 4 fixes | 8,0 km | **0** | **0** | ~744 $ |
| Anneau fixe + PTZ de confirmation | 4 fixes + 1 PTZ | 8,0 km | 0 | ~9 k | ~2 605 $ |
| Caméra zoom + tête PTZ en ronde | **1 PTZ** | 8,0 km | 38 s | **1,66 M** | ~1 861 $ |
| Grand-angle + PTZ à la demande | 2 fixes + 1 PTZ | **3,4 km** | 0 | ~9 k | ~2 233 $ |

La PTZ en ronde peut être intéressante pour **mesurer** avec un seul appareil,
mais une architecture qui dépasse le budget d'usure ne doit pas être
recommandée comme exploitation continue. `recommend()` applique donc aussi le
seuil de mouvements/an, au lieu de classer uniquement par coût.

Le grand-angle + PTZ ne voit pas plus loin : la portée de détection reste celle
du grand-angle. La PTZ améliore la levée de doute sur ce qui a déjà été repéré.

---

## 1. MINIMAL — module de mesure sur secteur

Le préréglage `minimal` cible désormais explicitement un **secteur de 140° à
8 km**, avec 4 positions. C'est un exemple de départ : le viewshed réel du site
doit remplacer ce secteur avant une campagne terrain. Le mode reste `measure`;
ce niveau ne doit pas être présenté comme un système d'alerte.

**Attention au vocabulaire :** `SIP-K675A-30X` est un **bloc caméra à zoom
optique**, pas une tête Pan/Tilt. Une vraie PTZ est l'assemblage du bloc caméra
avec une mécanique pan/tilt distincte.

| Poste | Référence | Rôle | Statut OpenIPC | Prix indicatif |
|---|---|---|---|---|
| Caméra principale | SIP-K675A-27135 (IMX675 + HI3516AV300, 2,7–13,5 mm motorisé) | acquisition et mesure; montée sur la tête pan/tilt | SoC ✅ / pilote IMX675 à porter | ~86 $ |
| Tête pan/tilt | double axe vis sans fin, DIY | prototype uniquement | — | ~93 $ |
| **Carte témoin** | **SIP-K335A-27135 (IMX335 + HI3516AV300)** | témoin OpenIPC sur le même SoC/ISP; comparaison STARVIS 1 ↔ 2 | **✅ prêt** | ~77–86 $ |
| Caisson, PoE, câblage | — | — | — | ~100 $ |
| | | | **Total** | **≈ 356–365 $** |

### Zoom, focale, iris et portée : quatre notions à ne pas confondre

Le `SIP-K675A-27135` **a déjà un zoom motorisé** : 2,7–13,5 mm, soit environ
5×. Le fabricant documente aussi des objectifs 6–22 mm et 5–50 mm sur la même
carte. Pour la géométrie OpenVigie, c'est la **focale absolue en millimètres**
qui compte, pas l'étiquette commerciale « 20× » ou « 30× ».

- augmenter la focale réduit le champ et met davantage de pixels sur un panache :
  la portée géométrique augmente, au prix d'une couverture angulaire plus faible ;
- le Pan/Tilt change la **direction** observée; sans tête Pan/Tilt, un zoom 20×/30×
  ne regarde toujours que le même axe ;
- l'iris/ouverture règle surtout la quantité de lumière et la profondeur de
  champ. Ce n'est pas le zoom. La carte K675A expose une interface auto-iris,
  mais OpenVigie ne modélise pas aujourd'hui un pilotage d'iris ;
- pour les focales de détection prévues ici (~5,2 mm en MEDIUM et ~9,3 mm en
  FULL), 2,7–13,5 mm suffit déjà. Un 20×/30× est surtout utile pour la
  **confirmation détaillée** ou une campagne de mesure très téléobjectif.

Le code contient une abstraction `set_zoom()`, mais l'ordonnanceur PTZ ne
commande pas encore automatiquement la focale à chaque vue. En pratique, pour
une caméra fixe de détection, on règle la focale une fois pour son secteur et on
la conserve : changer le zoom modifie le champ, le modèle de fond et la
calibration colonne→azimut.

### Ce que ce niveau permet réellement

| Configuration | Ouverture | Positions | Cycle | Portée (panache 30 m) |
|---|---:|---:|---:|---:|
| préréglage MINIMAL | 140° | 4 | 1,27 min | 8,0 km |
| autre exemple | 90° | 3 | 0,95 min | 8,0 km |

Réduire l'ouverture utile réduit le **cycle et la latence**, mais pas
automatiquement l'usure si la tête continue de faire un mouvement toutes les
19 s : on reste autour de **1,66 million de mouvements/an** avec
15 s d'observation + 4 s de stabilisation. C'est acceptable pour une campagne de
mesure limitée, pas pour une exploitation permanente.

Le bloc caméra 20×/30× n'est donc pas requis dans MINIMAL. Il devient pertinent
dans MEDIUM/FULL quand il est monté sur la tête de confirmation et n'est déplacé
que sur candidat.

### Relevé d'installation

Un relevé au smartphone à la pose ([issue #2](https://github.com/doxav/OpenVigie/issues/2))
donne une pose de départ pour quelques minutes de travail :

```bash
openvigie survey --lat 44.0 --lon 3.0 --altitude 500 --height 40                  --azimuth 85 --declination 2.1 --tilt 1.4 --mounting steel_tower
```

Il mesure **très bien l'assiette** (±0,5°, accéléromètre) et **mal l'azimut**
(±15° sur pylône treillis, le magnétomètre subissant l'acier). C'est
complémentaire de l'étalonnage par trafic aérien, qui excelle sur l'azimut.
L'assiette commande directement la portée estimée : elle doit donc être mesurée
et conservée avec la configuration du site.

---

## 2. MEDIUM — 360° robuste, sans calculateur externe

**Le changement d'architecture majeur.** On abandonne le balayage PTZ pour la
détection : 8 modules fixes couvrent 360° en permanence. Revisite nulle, aucune
usure mécanique, modèle de fond parfait puisqu'il n'y a plus de dérive de preset.
Le bloc **caméra zoom 30×** reste, monté sur une tête pan/tilt et uniquement pour la confirmation.

### Variante A — disponible aujourd'hui (STARVIS 1)

| Poste | Référence | Qté | Prix unitaire | Total |
|---|---|---|---|---|
| Modules fixes | SIP-K335A-27135 (IMX335 + HI3516AV300, 2,7–13,5 mm) | 8 | ~77–86 $ | ~616–688 $ |
| Caméra zoom de confirmation | SIP-K327A-30X (IMX327 + HI3516AV300, 30×; sans tête pan/tilt) | 1 | ~298 $ | ~298 $ |
| Positionneur | tête 10 kg motorisée | 1 | ~1 453 $ | ~1 453 $ |
| | | | **Total** | **≈ 2 370–2 440 $** |

Tout est **✅ prêt sous OpenIPC** : SoC et pilotes capteur en amont. Aucun portage.
Le HI3516AV300 apporte IVE + NNIE, donc recalage, mouvement et petit CNN local.

### Variante B — cible STARVIS 2 après portage

Mêmes quantités avec SIP-K675A-27135 (~86 $) et le bloc caméra zoom SIP-K675A-30X (~308 $, sans tête pan/tilt) :
**≈ 2 450 $**, et surtout **un capteur unique partout** — un seul ISP à calibrer,
un seul jeu de seuils, un seul portage à maintenir.

### Le point à trancher avant d'acheter

Le tier MEDIUM suppose que le CNN tourne dans la caméra, via NNIE. C'est
possible sur HI3516AV300, mais la chaîne d'outils HiSilicon est figée depuis
~2020 : jeu d'opérateurs restreint, INT8/INT16, pas de LSTM, RuyiStudio à faire
tourner sur une machine ancienne. Le dépôt gère ce risque de deux façons :

- le backend `nnie` **se replie automatiquement sur `classical`** si le binaire
  d'inférence est absent, et la dégradation est tracée dans les logs et le
  résumé — jamais silencieuse ;
- l'étage classique seul (différence au fond recalée + features physiques +
  logistique) reste fonctionnel, avec un taux de fausses alertes attendu 2 à 4×
  supérieur. C'est exploitable, mais c'est précisément la métrique qui décide de
  l'adoption.

**Alternative à considérer sérieusement :** un Raspberry Pi 5 + Hailo-8L (~13
TOPS, ~15 W l'ensemble, ~250 $) supprime le portage NNIE pour un dixième du prix
de la tête pan/tilt. Le tier MEDIUM « sans module externe » est un choix
d'architecture défendable — sobriété, autonomie, pas de point de panne central —
mais il ne se justifie pas par le coût.

---

## 3. FULL — opérationnel

| Poste | Référence | Qté | Prix unitaire | Total |
|---|---|---|---|---|
| Modules fixes | SIP-K675A-27135, réglés à ~9,3 mm | **14** | ~86 $ | ~1 204 $ |
| Caméra zoom de confirmation | SIP-K675A-30X (sans tête pan/tilt) | 1 | ~308 $ | ~308 $ |
| Positionneur | tête 15–20 kg, vis sans fin autobloquante, Pelco-D | 1 | ~1 933 $ | ~1 933 $ |
| Calculateur | Jetson Orin Nano Super 8 Go (67 TOPS INT8, 7–25 W) | 1 | **249 $ tarif NVIDIA** | ~249 $ |
| | | | **Total** | **≈ 3 694 $** |

**Corrections par rapport à la nomenclature initiale :**

- **8 modules → 14.** C'est le calcul de portée qui l'impose : 8 caméras à 52,9°
  ne détectent qu'un panache de 53 m à 11,5 km. `openvigie doctor` renvoyait `FAIL
  budget_portee` sur la configuration à 8 caméras / 12 km. Surcoût : ~516 $, à
  comparer au coût d'un feu détecté 40 minutes trop tard.
- Le Jetson à **249 $ chez un distributeur agréé**, pas 476 $ sur une place de
  marché. Bien vu dans la nomenclature initiale ; c'est confirmé ici.
- La tête lourde reste le poste le plus cher du site — plus que le calculateur et
  les 14 caméras additionnelles réunies. Elle ne sert qu'à la confirmation : la
  question « peut-on s'en passer et confirmer par triangulation depuis une
  deuxième tour ? » mérite d'être posée avant de l'acheter.

### Variante secteurs critiques

Remplacer 2 modules par des SIP-K678A-3611 (IMX678 8 MP) coûte ~220 $ de plus et
demande un **second portage de pilote**. À focale maximale, l'IMX678 offre un
champ plus large mais une résolution angulaire **moins fine** que l'IMX675 à
13,5 mm : il couvre plus de ciel, pas plus loin. À ne faire que si les essais de
phase 1 démontrent un gain réel sur vos horizons.

---

## 4. Ce que ces prix n'incluent pas

C'est le principal écart entre une nomenclature de modules et un site installé.
Compter, par tour, en plus des chiffres ci-dessus :

| Poste | Ordre de grandeur |
|---|---|
| Caissons IP66, parasoleils, hublots | 60–150 $ par caméra |
| Chauffage / dégivrage / essuie-glace (ensemble caméra zoom + tête PTZ) | 100–300 $ |
| Switch PoE industriel, injecteurs, câblage | 200–600 $ |
| Parafoudre réseau et alimentation | 150–400 $ |
| Mât, platines, bras déportés, main-d'œuvre en hauteur | très variable, souvent dominant |
| Stockage local (SSD pour la campagne de mesure) | 60–150 $ |
| Onduleur / batterie tampon | 150–500 $ |

Une intervention de maintenance sur pylône coûte généralement plus cher que le
calculateur externe. C'est l'argument décisif en faveur du tier FULL : mieux vaut
un site qu'on ne remonte pas voir.

---

## 5. Récapitulatif décisionnel

| Question | Réponse |
|---|---|
| Je veux commencer sans attendre le portage STARVIS 2 | Variante A du tier MEDIUM (IMX335 + HI3516AV300), 100 % OpenIPC aujourd'hui. Le portage IMX675 **n'est pas fait** : voir [PORTAGE_IMX675.md](PORTAGE_IMX675.md) |
| Je veux un seul capteur partout | Porter **IMX675 + HI3516AV300** : il couvre les fixes et le bloc caméra zoom 30×, le NNIE et le NIR |
| PTZ qui balaye ou caméras fixes ? | **Ça dépend de l'ouverture utile.** À 360°, les fixes gagnent nettement (revisite nulle, usure nulle, et moins cher). Sur un secteur restreint, un ensemble caméra zoom + tête PTZ redevient défendable — surtout parce qu'il y a un seul appareil à installer et à maintenir sur le mât |
| Combien de secteurs faut-il couvrir ? | Ce que le viewshed déclare exploitable, pas 360° par principe : `openvigie sectors --from-viewshed` |
| Faut-il un relevé à l'installation ? | Oui : quelques minutes au smartphone donnent une assiette à ±0,5°, et l'assiette commande la portée estimée. `openvigie survey` |
| Le grand-angle + PTZ voit-il plus loin ? | **Non.** La portée est celle du grand-angle ; la PTZ ne fait que lever le doute sur ce qu'il a déjà vu |
| Calcul embarqué ou externe ? | Externe si un faux négatif coûte cher. Le surcoût réel est de ~250 $, pas d'un ordre de grandeur |
| Quelle portée annoncer ? | Celle que renvoie `openvigie doctor`, mesurée ensuite en phase 1. Pas celle de la fiche capteur |
