# Portage IMX675 sous OpenIPC — état et procédure

> **Ce portage n'est pas fait, et ce document n'y substitue pas un pilote.**
> Il contient ce qui peut être préparé sans matériel : la procédure, les
> paramètres attendus, et un harnais de validation à exécuter par qui dispose
> d'une carte. Le pilote lui-même reste à écrire.

## Pourquoi ce document ne contient pas de pilote

Écrire un pilote capteur pour OpenIPC demande trois choses qu'aucune analyse ne
remplace :

1. **la table de registres du capteur**, qui figure dans une documentation Sony
   sous accord de confidentialité et n'est pas publique ;
2. **la carte elle-même**, pour vérifier l'adresse I²C, le nombre de voies MIPI,
   la fréquence d'horloge et le câblage réel du module ;
3. **des essais**, parce qu'un capteur mal initialisé ne renvoie pas une erreur
   mais une image — noire, bruitée, décalée d'une ligne, ou correcte à 20 ips et
   corrompue à 25.

Publier un `imx675.c` d'apparence plausible avec des registres reconstitués
serait exactement l'artefact que ce projet s'attache à refuser : quelque chose
de convaincant et de faux. Quelqu'un le flasherait.

**Ce qui est vrai aujourd'hui :** `openvigie hw --soc hi3516av300 --sensor IMX675`
renvoie `porting_required`, et c'est la bonne réponse. La variante
IMX335 + HI3516AV300 fonctionne, elle, sans aucun portage.

---

## Ce que le portage doit produire, et comment le vérifier

Le logiciel n'a besoin que de quatre garanties de la part du capteur. Le harnais
ci-dessous les vérifie une par une, sur cible :

```bash
openvigie sensor-validate --host 192.168.1.64 --sensor IMX675 --expect-fps 25
```

| # | Garantie | Pourquoi elle compte pour OpenVigie | Contrôle |
|---|---|---|---|
| 1 | **2592 × 1944 effectifs** | Tout le budget optique en dépend : la portée se calcule à partir du nombre de pixels et du pas | résolution du flux, et image non rognée |
| 2 | **Pas de pixel 2,0 µm confirmé** | Une erreur ici propage une erreur de portée proportionnelle | champ mesuré sur amers vs champ calculé |
| 3 | **Cadence stable** | Les features temporelles (croissance en m²/s) supposent un intervalle connu | écart-type des intervalles entre trames |
| 4 | **Réponse NIR effective** | C'est la seule raison de préférer STARVIS 2 à STARVIS 1 | comparaison de bruit à faible éclairement |

Les deux premiers points sont vérifiables en une heure. Le quatrième demande une
nuit et une scène de référence, et c'est celui qui décidera si le surcoût
STARVIS 2 se justifie sur le terrain — question aujourd'hui ouverte.

---

## Procédure

### 1. Inventaire de la carte

```bash
ssh root@<ip> ipctool          # SoC, capteur détecté, flash, MIPI
ssh root@<ip> cat /proc/umap/sensor
```

Relever l'adresse I²C, le nombre de voies MIPI et la fréquence d'horloge. Ces
trois valeurs conditionnent tout le reste et diffèrent d'un module à l'autre,
même à capteur identique.

### 2. Partir du pilote le plus proche

Dans l'arbre OpenIPC, l'IMX335 partage la génération et le SoC cible. C'est le
point de départ le plus court : même famille de bus, même intégration ISP.

Les écarts à traiter : taille de matrice, séquence d'initialisation, plages de
gain analogique et numérique, et gestion du HCG/LCG propre à STARVIS 2.

### 3. Profil ISP

Un pilote qui donne une image n'est pas un pilote utilisable pour la détection
de fumée. Les réglages qui comptent — et qui, mal choisis, détruisent le signal
recherché — sont documentés dans [OPENIPC.md](OPENIPC.md) : `3dnr`, `drc`,
qualité JPEG. Prévoir trois profils : jour, crépuscule, nuit NIR.

### 4. Validation

```bash
# sur la cible, une fois le pilote chargé
openvigie sensor-validate --host <ip> --sensor IMX675 --expect-fps 25 --json
```

Puis la campagne optique, qui vérifie que la géométrie annoncée correspond au
capteur réel :

```bash
openvigie survey --lat <lat> --lon <lon> --declination <d> --altitude <alt> ...
python scripts/site_survey.py --config site.yaml --snapshot-url http://<ip>/image.jpg
```

Un champ mesuré s'écartant de plus de 2 % du champ calculé signale un pas de
pixel ou une focale différents de la fiche — à corriger dans `geometry.py`
avant tout dimensionnement.

---

## Ce que le portage débloquerait

Un seul portage couvre **toutes** les cartes du même capteur : modules fixes
5 MP, blocs caméra zoom 20×/30×, et la sensibilité NIR sur les deux. Le portage concerne le capteur/ISP; il ne fournit ni ne pilote la mécanique Pan/Tilt. C'est la raison pour
laquelle IMX675 + HI3516AV300 reste la cible prioritaire plutôt qu'IMX678 ou
IMX664, qui demanderaient chacun leur propre portage pour un gain moindre.

En attendant, la variante IMX335 permet de développer et valider l'intégralité
du logiciel — c'est ce que recommande [HARDWARE.md](HARDWARE.md), et rien dans
la chaîne de détection ne dépend du capteur.

## Contribuer

C'est le blocage matériel n°1 du projet, et il ne se lève pas par du logiciel.
Si vous avez une carte, une chaîne de compilation OpenIPC et l'accès à la
documentation du capteur, ouvrir une issue « Retour matériel » est le point de
départ le plus utile.
