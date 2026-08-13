# Cibler OpenIPC plutôt qu'un SoC

## Pourquoi

Écrire le projet pour HI3516AV300 reviendrait à le lier à une référence de
module, un fournisseur et une génération de puce déjà figée. OpenIPC fournit une
interface stable au-dessus de quatre familles de SoC :

| Interface | Rôle |
|---|---|
| `majestic` | flux vidéo, snapshot JPEG, configuration image |
| `cli -g` / `cli -s` | lecture/écriture de la configuration majestic |
| `ipctool` | inventaire matériel (SoC, capteur, flash) |
| `/etc/os-release` | identification du firmware et de sa version |

OpenVigie ne parle qu'à ces quatre interfaces. Changer de carte ne change pas le code.

```bash
openvigie hw --matrix                          # tout ce qu'OpenVigie connaît
openvigie hw                                   # inventaire de la carte locale
openvigie hw --soc ssc338q --sensor IMX335     # évaluer une référence avant de l'acheter
openvigie majestic --host 192.168.1.64         # profil de réglages pour la détection
```

## Deux conditions indépendantes

Une carte est utilisable si **le SoC est supporté** *et* **le pilote capteur est
en amont**. Ce sont deux questions distinctes, et les confondre est la source
d'erreur d'achat la plus fréquente.

### SoC

| SoC | Famille | Accélérateur | IVE | Backend conseillé |
|---|---|---|---|---|
| hi3516av300 | HiSilicon CV500 | **NNIE** | oui | `nnie` |
| hi3516cv500 | HiSilicon CV500 | **NNIE** | oui | `nnie` |
| hi3516ev300 | HiSilicon EV300 | aucun | oui | `classical` |
| hi3516cv300 | HiSilicon CV300 | aucun | oui | `classical` |
| gk7605v100 | Goke | aucun | oui | `classical` |
| gk7205v300 | Goke | aucun | oui | `classical` |
| ssc338q | SigmaStar | aucun | non | `classical` |
| ssc30kq | SigmaStar | aucun | non | `classical` |
| t31 | Ingenic | NPU | non | `classical` |
| t41 | Ingenic | NPU | non | `classical` |

Les NPU Ingenic ne sont pas retenus comme backend conseillé : le SDK est
propriétaire et la conversion de modèle doit être validée avant de compter dessus.
Un SoC absent de cette table n'est pas une impasse — OpenVigie se replie sur l'étage
classique et le calcul externe, et le signale.

### Pilote capteur

| Capteur | Génération | Pilote OpenIPC |
|---|---|---|
| IMX307, IMX327 | 2 MP | **en amont** |
| IMX335 | 5 MP, STARVIS 1 | **en amont** |
| IMX415 | 8 MP | **en amont** |
| IMX662, IMX664 | STARVIS 2 | à porter |
| IMX675, IMX678 | STARVIS 2 | à porter |
| IMX585 | STARVIS 2 | à porter |

**Un seul portage débloque toutes les cartes du même capteur.** C'est pourquoi
la recommandation est de porter **IMX675 + HI3516AV300** en priorité : cette
combinaison couvre à la fois les modules fixes 5 MP, le bloc 30×, le NNIE et la
sensibilité NIR STARVIS 2.

En attendant, la variante IMX335 + HI3516AV300 est utilisable telle quelle et
permet de développer et valider l'intégralité du logiciel.

## Profil majestic pour la détection

Les réglages par défaut d'une caméra de vidéosurveillance sont optimisés pour
l'œil humain sur une scène proche. Ils détruisent le signal recherché ici.

```bash
openvigie majestic --host 192.168.1.64          # affiche le profil commenté
./scripts/openipc_deploy.sh 192.168.1.64 --apply   # l'applique (avec sauvegarde)
```

| Réglage | Valeur | Raison |
|---|---|---|
| `.jpeg.qfactor` | 90 | la fumée naissante est un signal de faible amplitude |
| `.jpeg.fps` | 1 | le facteur limitant est la revisite, pas le débit |
| `.osd.enabled` | false | l'horodatage incrusté crée un candidat permanent |
| `.isp.slowShutter` | disabled | le flou de mouvement fausse les features temporelles |
| `.image.contrast` | 50 | ne pas écraser les faibles écarts |
| `.video0.fps` | 5 | le flux ne sert qu'à la levée de doute humaine |

### Trois réglages à ne pas toucher à l'aveugle

- **`.isp.3dnr`** — la réduction de bruit temporelle agressive efface une fumée
  fine en mouvement lent. C'est exactement le signal recherché. À baisser au
  minimum acceptable, et à vérifier sur des séquences réelles.
- **`.isp.drc`** — le DRC/WDR modifie le mapping tonal image par image : le
  modèle de fond voit un changement global à chaque bascule.
- **`.image.mirror`** / rotation — invalide la relation colonne → azimut. Toute
  modification impose de recalibrer le relèvement, faute de quoi les alertes
  partent avec un azimut faux.

## Acquisition : snapshot, pas RTSP

```
http://<ip>/image.jpg    ← chemin recommandé
rtsp://<ip>/stream0      ← repli seulement
```

La compression H.265 supprime les micro-textures de faible amplitude, c'est-à-dire
la fumée fine. `openvigie doctor` et `site_survey.py` mesurent le niveau de blocking
et refusent une source trop compressée.

## Agent embarqué

```bash
./scripts/openipc_deploy.sh 192.168.1.64 --push-agent
```

Copie le cœur NumPy (aucune dépendance native) dans `/tmp`. Si l'image OpenIPC ne
contient pas Python 3, le script le dit et renvoie vers le mode calcul externe —
sans échouer silencieusement.

Le partage des rôles :

| SoC avec NNIE/NPU | SoC sans accélérateur |
|---|---|
| capture + étage classique + petit CNN local | capture + étage classique |
| alerte autonome possible | candidats remontés au calculateur |

Dans les deux cas, l'étage classique tourne dans la caméra : c'est lui qui ramène
une image 5 MP à quelques dizaines de régions, et qui rend le reste tenable.
