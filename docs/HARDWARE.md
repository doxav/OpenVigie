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

## 1. MINIMAL — campagne de mesure

**Objectif : mesurer, pas détecter.** Ce niveau ne doit pas être mis en service
comme système d'alerte. Il sert à répondre aux questions qu'aucun calcul ne
remplace : quelle est la portée réelle par temps réel, le mât vibre-t-il, la tête
revient-elle sur son preset, le hublot s'encrasse-t-il, à quoi ressemblent les
faux candidats de *ce* site.

| Poste | Référence | Rôle | Statut OpenIPC | Prix indicatif |
|---|---|---|---|---|
| Module de détection | SIP-K675G6 (IMX675 + GK7605V100) | mesurer la fumée faible et le NIR STARVIS 2 | SoC ✅ / pilote IMX675 à porter | ~91 $ |
| Bloc de confirmation | SIP-K675A-30X (IMX675 + HI3516AV300) | zoom 30×, levée de doute | SoC ✅ / pilote à porter | ~306–309 $ |
| **Carte témoin** | **SIP-K335G6 (IMX335 + GK7605V100)** | **développer toute la chaîne OpenIPC pendant le portage** | **✅ prêt** | ~74 $ |
| Tête pan/tilt | double axe vis sans fin, DIY | prototype uniquement | — | ~93 $ |
| | | | **Total** | **≈ 567 $** |

**Corrections par rapport à la nomenclature initiale :**

- Le nombre de presets passe de 4 à **5**. Quatre positions au grand-angle
  laissent 4 secteurs aveugles : `openvigie doctor` le refuse (`FAIL couverture`).
- La portée annoncée passe de 6 km à **3,5 km**. À 6 km, le grand-angle ne
  détecte qu'un panache de 53 m — soit un feu déjà installé.
- L'usure est de **~1,7 million de mouvements/an**. La tête à 93 $ ne tiendra pas
  une saison en balayage continu. C'est acceptable ici parce que ce niveau est
  une campagne de mesure, pas un service continu — mais il faut le dire.

La carte témoin IMX335 n'est pas un accessoire : elle permet de développer tout
le logiciel pendant que le pilote STARVIS 2 est porté, et de comparer directement
STARVIS 1 et STARVIS 2 sur la même scène, ce qui est la seule façon honnête de
savoir si le surcoût STARVIS 2 se justifie sur votre terrain.

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
| Je veux commencer sans attendre le portage STARVIS 2 | Variante A du tier MEDIUM (IMX335 + HI3516AV300), 100 % OpenIPC aujourd'hui |
| Je veux un seul capteur partout | Porter **IMX675 + HI3516AV300** : il couvre les fixes, le bloc 30×, le NNIE et le NIR |
| PTZ qui balaye ou caméras fixes ? | **Fixes pour détecter, PTZ pour confirmer.** Ce n'est pas un compromis, c'est mieux sur tous les critères sauf le prix initial |
| Calcul embarqué ou externe ? | Externe si un faux négatif coûte cher. Le surcoût réel est de ~250 $, pas d'un ordre de grandeur |
| Quelle portée annoncer ? | Celle que renvoie `openvigie doctor`, mesurée ensuite en phase 1. Pas celle de la fiche capteur |
