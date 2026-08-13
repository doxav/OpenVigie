# Étalonnage géométrique par trafic aérien

*Option. Désactivée par défaut. Activée sur le tier FULL.*

```bash
openvigie calibrate -t full --simulate                 # validation de la chaîne, sans matériel
openvigie calibrate -c site.yaml --azimuth 87 \
     --observations sky.json --adsb adsb.jsonl \
     --output data/calibration/V03.json
openvigie calibrate -c site.yaml ... --reference data/calibration/V03.json   # contrôle de dérive
```

---

## L'idée

Une caméra de guet regarde l'horizon. Un avion en croisière à 10 700 m se trouve,
vu depuis une tour, **entre 19° au-dessus de l'horizon à 30 km et 2° à 200 km** :
exactement dans la partie haute de l'image, la zone que le détecteur de fumée
ignore de toute façon.

Et cet avion diffuse en clair sa position tridimensionnelle, horodatée.

Chaque passage est donc une **mire de calibration gratuite à position connue**.

## Ce que ça vaut vraiment

Mesures obtenues sur la chaîne complète (25 aéronefs, bruit 1,5 px, 5 fausses
détections, départ depuis une boussole fausse de 2,7°) — reproductibles par
`openvigie calibrate --simulate` :

| Grandeur | Boussole + niveau | Après étalonnage |
|---|---|---|
| Lacet (azimut) | ±2° | **±0,005°** |
| Assiette (tilt) | ±0,5° | **±0,003°** |
| Focale réelle | fiche constructeur | **±0,002 mm** |
| Décalage d'horloge | inconnu | **mesuré à ±0,05 s** |

Deux conséquences, l'une bien plus importante que l'autre.

**1. La portée estimée — le gain majeur, et le moins évident.** Pour une caméra
dominant son terrain de 100 m :

| Erreur d'assiette | Erreur de distance à 5 km |
|---|---|
| 0,5° (niveau à bulle) | **2 180 m — 44 %** |
| 0,05° | 218 m — 4 % |
| 0,007° (mesuré ici) | **31 m — 0,6 %** |

Comme l'ellipse d'incertitude d'une tour seule est **dominée par le terme de
distance**, c'est là que se joue l'essentiel. Le terme par défaut du dépôt
(25 % de la distance) suppose précisément une assiette mal connue.

**2. La triangulation.** L'ellipse triangulée est *entièrement* proportionnelle à
l'incertitude d'azimut. À 8 km avec deux tours croisant à 80° :

| σ azimut | Ellipse triangulée |
|---|---|
| 2° (boussole) | 377 m |
| 0,027° (étalonné) | **5 m** |

**3. Et l'usage le plus rentable au quotidien : la détection de dérive.** Un
preset qui a glissé, un bras qui a plié, une platine heurtée par un technicien.
L'étalonnage tourne en continu et le voit — au lieu qu'on s'en aperçoive sur une
alerte mal localisée, un jour de feu.

```
glissement réel 0,05° -> mesuré +0,059° -> stable
glissement réel 0,30° -> mesuré +0,309° -> DÉRIVE
glissement réel 1,20° -> mesuré +1,213° -> DÉRIVE
```

## Les trois pièges, et leur traitement

### Le temps

Un avion à 50 km défile à **0,29°/s**. Une seconde d'erreur d'horloge vaut donc
une erreur d'azimut de 0,29° — autant que ce qu'on cherche à corriger.

Le décalage d'horloge est donc **estimé comme paramètre**, ce qui transforme le
piège en mesure :

```
décalage réel 1,0 s -> estimé 1,004 s | erreur de lacet résiduelle 0,002°
```

Mais seulement si les avions volent dans des **directions variées**. Sur un
couloir unique, un retard se confond partiellement avec une erreur d'azimut, et
le paramètre est automatiquement gelé (voir plus bas).

*Résultat mesuré, plus rassurant que prévu :* même sur un couloir unique avec un
décalage d'une seconde non corrigé, le biais de lacet reste inférieur à 0,01° —
le décalage se manifeste surtout par une inflation du résidu (2,0 → 3,2 px), donc
par un élargissement honnête de l'incertitude annoncée. La méthode se dégrade
proprement.

### L'altitude barométrique

L'ADS-B diffuse le plus souvent une altitude *pression*, référencée à
1013,25 hPa. Elle peut s'écarter de plusieurs centaines de mètres de l'altitude
vraie. À 50 km, **300 m d'erreur valent 0,33° d'élévation** — c'est-à-dire
directement une erreur d'assiette, donc de portée.

| Biais d'altitude | Erreur d'assiette sans correction | Avec estimation du biais |
|---|---|---|
| 150 m | −0,140° | +0,014° |
| 300 m | −0,279° | +0,015° |

On privilégie donc l'altitude **GNSS** (`alt_geom` / `geo_altitude`), et à défaut
on estime un biais commun — ce qui exige de la diversité de distance.

### La sphéricité

À 80 km vers l'est, la convergence des méridiens fait que l'azimut orthodromique
vaut 89,65° et non 90°. Un calcul en plan tangent introduirait donc **0,35°
d'erreur**, soit dix fois la précision visée. Les azimuts sont calculés en
orthodromie.

## Le garde-fou central : l'identifiabilité

Ajuster un paramètre que les données ne contraignent pas ne produit pas une
erreur visible : cela produit une **valeur plausible et fausse**, qui se propage
ensuite dans toutes les alertes du site. Le module analyse donc ce que les
observations permettent réellement, et **gèle le reste en le disant** :

| Paramètre | Condition | Pourquoi |
|---|---|---|
| lacet, assiette | ≥ 4 points | — |
| roulis | ≥ 8 points, étalement en azimut ≥ 3° | sinon confondu avec l'assiette |
| focale | ≥ 12 points, azimut ≥ 5°, élévation ≥ 3° | sinon confondue avec lacet/assiette |
| décalage d'horloge | ≥ 12 points, **dispersion des caps ≥ 25°** | sinon confondu avec le lacet |
| biais d'altitude | ≥ 12 points, **rapport de distances ≥ 2** | son effet varie en 1/d, celui de l'assiette non |

```
   caps ±  5° -> dispersion  2,3° | horloge estimable = non -> gelée
   caps ± 90° -> dispersion 46,5° | horloge estimable = oui -> ajustée
```

## Robustesse

| Situation | Résultat mesuré |
|---|---|
| Boussole fausse de 12° | converge, erreur finale 0,004° |
| 30 fausses détections (satellites, oiseaux, pixels chauds) | erreur 0,004° |
| 60 fausses détections (plus que de vraies) | erreur 0,041° — encore 50× mieux qu'une boussole |
| Bruit de détection 10 px | erreur 0,035°, et **σ annoncé porté à 0,17°** |
| 3 aéronefs seulement | erreur 0,15° — utile, et la qualité annoncée le dit |

Poids de Huber, rejet des résidus aberrants puis réajustement, association
refusée en cas d'ambiguïté. L'incertitude reportée dans les alertes est
délibérément **conservatrice** : une incertitude sous-estimée donne à un
opérateur une confiance qu'il n'a pas.

## Comment collecter les données

### Source ADS-B

**Recommandé : un récepteur local.** Une clé SDR (~25 €) et une antenne sur le
mât. C'est cohérent avec le principe d'autonomie du site : aucune dépendance à
Internet, horodatage local donc bien meilleur, et l'étalonnage continue de
fonctionner quand la liaison est tombée — c'est-à-dire les jours d'orage.

```python
from openvigie.calibration import Dump1090Source
source = Dump1090Source("http://127.0.0.1:8080/data/aircraft.json")
source.poll()          # à appeler ~1 Hz
tracks = source.tracks(t0, t1)
```

**Alternative : OpenSky.** Pratique pour démarrer sans matériel, mais cadence
plus faible, horodatage moins précis, quotas, et dépendance réseau. Les modalités
d'accès évoluent : vérifier la documentation du service.

### Détection des points dans le ciel

```python
from openvigie.calibration import detect_sky_points
points = detect_sky_points(frame, reference, t=timestamp, horizon_rows=horizon)
```

Petites taches lumineuses nouvelles, **uniquement au-dessus de l'horizon**. Un
avion à 50 km ne fait qu'un ou deux pixels : c'est un point, pas un objet.

⚠️ **Les traînées de condensation ne sont pas des mires.** Elles sont bien plus
visibles que l'avion, mais elles s'étirent derrière lui et dérivent avec le vent :
leur position ne dit pas où est l'appareil. Le filtre de surface maximale les
écarte volontairement.

### Cadence

Collecter sur plusieurs jours. Une trentaine d'observations bien réparties suffit
pour atteindre 0,01°, et la collecte est passive : la caméra fait son travail
normal, l'étalonnage se nourrit de ce qu'elle voit déjà.

## Limites, honnêtement

- **Il faut du trafic aérien.** En France métropolitaine ce n'est pas un
  problème, mais une vue orientée vers un secteur sans couloir aérien peut
  n'accumuler que quelques points par semaine. Le module le dit (`quality`,
  `n_used`) au lieu de prétendre le contraire.
- **Il faut du ciel dégagé** dans la direction observée.
- **Il faut une horloge synchronisée.** NTP sur la passerelle. Le module mesure
  le décalage résiduel, il ne rattrape pas une horloge en dérive libre.
- **Ce module n'a pas été confronté à des données réelles.** Toutes les valeurs
  ci-dessus proviennent d'une simulation de bout en bout avec bruit, biais et
  aberrants ; elles valident la chaîne de calcul, pas le comportement d'un
  capteur devant un vrai ciel. La détection des points est le maillon à éprouver
  en premier : un avion à 1 px sur un capteur bruité par temps de brume n'est pas
  la même chose qu'un point gaussien synthétique.
- **Alternative classique à ne pas oublier :** l'étalonnage sur amers (sommets,
  pylônes, clochers relevés sur carte) reste excellent et n'exige aucune horloge.
  Il échoue en revanche sur un horizon plat et sans relief — les Landes, la
  Beauce — et il ne détecte pas la dérive. Les deux méthodes sont
  complémentaires ; utiliser les amers quand il y en a, les avions partout et en
  continu.

## Vie privée et données

L'ADS-B est une diffusion publique en clair, émise par des aéronefs, non par des
personnes. Le module n'en conserve que ce qui sert à l'étalonnage : identifiant
technique, position, horodatage, sur une fenêtre courte. Il ne construit pas
d'historique de vols et n'a pas vocation à en construire — ce serait un autre
produit, avec d'autres responsabilités. Certains États restreignent la diffusion
de positions d'aéronefs d'État ou militaires : filtrer si nécessaire.

## Altitude du site (corrigé en 0.4.0)

`site_altitude_m` doit être renseignée : c'est l'altitude du **terrain**, la
hauteur de mât venant en plus (`optics.camera_height_m`). Jusqu'à la 0.4.0 elle
était figée à 0 m dans ce chemin de configuration ; sur une tour à 900 m,
l'élévation calculée des aéronefs était fausse de plusieurs dixièmes de degré,
soit exactement la grandeur qu'on prétend mesurer au millième près.

`openvigie doctor` le signale désormais quand l'altitude vaut 0.

## Distorsion optique — limite connue

Le modèle de caméra est un sténopé pur : aucun coefficient de distorsion radiale
ou tangentielle. Sur un objectif grand-angle non corrigé, la distorsion en bord
de champ dépasse largement les précisions annoncées ici. Les chiffres de ce
document restent donc valables au centre du champ et pour des focales moyennes à
longues ; l'ajustement de coefficients intrinsèques est en roadmap.

En pratique, cela signifie qu'un étalonnage obtenu sur des aéronefs répartis
dans tout le champ absorbera une partie de la distorsion dans la focale et le
lacet, avec un résidu qui se verra dans le RMS. Un RMS élevé sur des données
réelles alors que la simulation donne 2 px est un signe de distorsion non
modélisée, pas nécessairement d'un mauvais appariement.
