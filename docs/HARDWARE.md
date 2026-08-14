# Conception matérielle — trois niveaux

Les portées annoncées ici ne sont pas des estimations commerciales : elles sortent
du calcul optique du dépôt (`openvigie plan`, `openvigie doctor`) et sont vérifiées par
les tests. Elles sont plus modestes que ce qu'annonce la plupart des fiches
produit, pour une raison simple : la fiche produit parle de « voir un panache »,
et nous parlons de « détecter automatiquement un panache naissant de 30 m ».

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

Le tableau ci-dessus déduit un nombre de caméras d'une couverture 360°. C'est
rarement ce que le terrain demande : une direction peut être masquée par une
crête, dépourvue de végétation, ou impossible à couvrir depuis le point de
montage disponible.

```bash
openvigie sectors -c site.yaml --from-viewshed data/mnt/site.npy
```

L'unité de dimensionnement devient le **secteur angulaire utile**. Et cela
change la conclusion, parce que le coût d'une ronde PTZ n'est pas linéaire :

| Ouverture utile | Positions PTZ | Cycle | Latence moyenne |
|---|---|---|---|
| 90° | 2 | 0,6 min | 19 s |
| 140° | 4 | 1,3 min | 38 s |
| 250° | 9 | 2,9 min | 1,4 min |
| 360° | 21 (à 3× de zoom) | > 5 min | > 2,5 min |

**Le balayage PTZ, inexploitable à 360°, redevient raisonnable sur un secteur
restreint.** C'est le fond de l'issue #1, et c'est ce qui justifie de revoir le
tier MINIMAL ci-dessous.

### Ce que coûte réellement chaque architecture

Sur 140° utiles à 8 km, panache de 30 m :

| Architecture | Appareils | Portée | Latence | Mouvements/an | Matériel |
|---|---|---|---|---|---|
| Anneau fixe seul | 4 fixes | 8,0 km | **0** | **0** | **744 $** |
| Anneau fixe + PTZ de confirmation | 4 fixes + 1 PTZ | 8,0 km | 0 | ~10 k | 2 605 $ |
| Module PTZ seul | **1 PTZ** | 8,0 km | 1,3 min | **1,66 M** | 1 861 $ |
| Grand-angle + PTZ à la demande | 2 fixes + 1 PTZ | **3,4 km** | 0 | ~9 k | 2 233 $ |

Trois enseignements que le calcul impose et que l'intuition contredit :

1. **La tête PTZ (~1 450 $) domine tout budget PTZ.** Un anneau de quatre
   modules fixes coûte 744 $ — moins de la moitié d'un module PTZ, avec zéro
   latence et zéro usure. Sur le seul critère du matériel, l'anneau fixe gagne.
2. **Mais le matériel n'est pas le coût dominant d'un site réel.** Mât, génie
   civil, câblage, main-d'œuvre en hauteur et maintenance ne figurent pas dans
   ce tableau et dépendent du **nombre d'appareils à installer**, pas de leur
   prix. Un module unique contre quatre change cet arbitrage — c'est l'argument
   sérieux en faveur de l'option PTZ, et il n'est pas dans la colonne « prix ».
3. **Le grand-angle + PTZ ne voit pas plus loin.** La portée est fixée par le
   grand-angle (3,4 km), pas par la PTZ : on n'envoie la PTZ que sur ce que le
   grand-angle a déjà repéré. C'est une architecture de **levée de doute**, pas
   d'extension de portée. L'erreur d'intuition inverse est fréquente.

---

## 1. MINIMAL — module de mesure sur secteur

*Redéfini en 0.6.0 : ce niveau est désormais un **module couvrant un secteur
utile**, non une tentative de couverture 360° au rabais
([issue #1](https://github.com/doxav/OpenVigie/issues/1)).*

**Objectif : mesurer, pas détecter.** Ce niveau ne doit pas être mis en service
comme système d'alerte — le mode d'exploitation par défaut est d'ailleurs
`measure`, qui n'émet aucun événement. Il sert à répondre aux questions
qu'aucun calcul ne remplace : quelle est la portée réelle par temps réel, le mât
vibre-t-il, la tête revient-elle sur son preset, le hublot s'encrasse-t-il, à
quoi ressemblent les faux candidats de *ce* site.

| Poste | Référence | Rôle | Statut OpenIPC | Prix indicatif |
|---|---|---|---|---|
| Bloc PTZ | SIP-K675A-30X (IMX675 + HI3516AV300) | module de mesure, 2 à 4 positions sur le secteur utile | SoC ✅ / pilote IMX675 à porter | ~307 $ |
| Tête pan/tilt | double axe vis sans fin, DIY | prototype uniquement | — | ~93 $ |
| **Carte témoin** | **SIP-K335G6 (IMX335 + GK7605V100)** | **développer toute la chaîne OpenIPC pendant le portage** | **✅ prêt** | ~74 $ |
| Caisson, PoE, câblage | — | — | — | ~100 $ |
| | | | **Total** | **≈ 574 $** |

### Ce que ce niveau permet réellement

| Ouverture utile | Positions | Cycle | Portée (panache 30 m) |
|---|---|---|---|
| 90° | 2 | 0,6 min | 8,0 km |
| 140° | 4 | 1,3 min | 8,0 km |

Contre 3,5 km auparavant, quand ce même budget tentait de couvrir 360° au
grand-angle. **Se restreindre au secteur utile double la portée à matériel
constant** — c'est tout l'argument de l'issue #1.

### Ce qui reste vrai, et qu'il faut dire

- **L'usure demeure ~1,7 million de mouvements/an.** La tête à 93 $ ne tiendra
  pas une saison en balayage continu. Acceptable pour une campagne de mesure de
  quelques mois, pas pour un service permanent — et c'est précisément pourquoi
  ce niveau n'est pas un système d'alerte.
- **Un anneau de modules fixes coûterait moins cher** (744 $ pour 140° à 8 km,
  contre 1 861 $ pour un module PTZ complet avec tête industrielle). Le module
  PTZ se justifie ici par le nombre d'appareils à installer et à maintenir sur
  un mât — un seul contre quatre — et parce qu'une campagne de mesure a besoin
  de zoom pour caractériser ce qu'elle observe.
- **La carte témoin IMX335 n'est pas un accessoire.** Elle permet de développer
  tout le logiciel pendant que le pilote STARVIS 2 est porté, et de comparer
  STARVIS 1 et STARVIS 2 sur la même scène — la seule façon honnête de savoir
  si le surcoût STARVIS 2 se justifie sur votre terrain. Voir
  [PORTAGE_IMX675.md](PORTAGE_IMX675.md).

### Relevé d'installation

Un relevé au smartphone à la pose ([issue #2](https://github.com/doxav/OpenVigie/issues/2))
donne une pose de départ pour quelques minutes de travail :

```bash
openvigie survey --lat 44.0 --lon 3.0 --altitude 500 --height 40 \
                 --azimuth 85 --declination 2.1 --tilt 1.4 --mounting steel_tower
```

Il mesure **très bien l'assiette** (±0,5°, accéléromètre — rien sur un pylône ne
perturbe la gravité) et **mal l'azimut** (±15° sur pylône treillis, le
magnétomètre subissant l'acier). C'est exactement complémentaire de
l'étalonnage par trafic aérien, qui excelle sur l'azimut. Et l'assiette est la
grandeur qui commande la portée estimée : 0,5° d'erreur valent 44 % d'erreur de
distance à 5 km.

---

## 2. MEDIUM — 360° robuste, sans calculateur externe

**Le changement d'architecture majeur.** On abandonne le balayage PTZ pour la
détection : 8 modules fixes couvrent 360° en permanence. Revisite nulle, aucune
usure mécanique, modèle de fond parfait puisqu'il n'y a plus de dérive de preset.
Le bloc 30× reste, mais uniquement pour la confirmation.

### Variante A — disponible aujourd'hui (STARVIS 1)

| Poste | Référence | Qté | Prix unitaire | Total |
|---|---|---|---|---|
| Modules fixes | SIP-K335A-27135 (IMX335 + HI3516AV300, 2,7–13,5 mm) | 8 | ~77–86 $ | ~616–688 $ |
| Confirmation | SIP-K327A-30X (IMX327 + HI3516AV300, 30×) | 1 | ~298 $ | ~298 $ |
| Positionneur | tête 10 kg motorisée | 1 | ~1 453 $ | ~1 453 $ |
| | | | **Total** | **≈ 2 370–2 440 $** |

Tout est **✅ prêt sous OpenIPC** : SoC et pilotes capteur en amont. Aucun portage.
Le HI3516AV300 apporte IVE + NNIE, donc recalage, mouvement et petit CNN local.

### Variante B — cible STARVIS 2 après portage

Mêmes quantités avec SIP-K675A-27135 (~86 $) et SIP-K675A-30X (~308 $) :
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
| Confirmation | SIP-K675A-30X | 1 | ~308 $ | ~308 $ |
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
| Chauffage / dégivrage / essuie-glace (bloc PTZ) | 100–300 $ |
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
| Je veux un seul capteur partout | Porter **IMX675 + HI3516AV300** : il couvre les fixes, le bloc 30×, le NNIE et le NIR |
| PTZ qui balaye ou caméras fixes ? | **Ça dépend de l'ouverture utile.** À 360°, les fixes gagnent nettement (revisite nulle, usure nulle, et moins cher). Sur un secteur restreint, un module PTZ redevient défendable — surtout parce qu'il y a un seul appareil à installer et à maintenir sur le mât |
| Combien de secteurs faut-il couvrir ? | Ce que le viewshed déclare exploitable, pas 360° par principe : `openvigie sectors --from-viewshed` |
| Faut-il un relevé à l'installation ? | Oui : quelques minutes au smartphone donnent une assiette à ±0,5°, et l'assiette commande la portée estimée. `openvigie survey` |
| Le grand-angle + PTZ voit-il plus loin ? | **Non.** La portée est celle du grand-angle ; la PTZ ne fait que lever le doute sur ce qu'il a déjà vu |
| Calcul embarqué ou externe ? | Externe si un faux négatif coûte cher. Le surcoût réel est de ~250 $, pas d'un ordre de grandeur |
| Quelle portée annoncer ? | Celle que renvoie `openvigie doctor`, mesurée ensuite en phase 1. Pas celle de la fiche capteur |
