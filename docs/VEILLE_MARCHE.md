# Veille marché — solutions existantes de détection de feux de forêt

*Recherche du 16 août 2026, à revérifier périodiquement : ce secteur évolue
vite. Objectif : situer OpenVigie par rapport à ce qui existe déjà (Alibaba,
AliExpress, GitHub, Reddit et adjacents), et éviter la confusion la plus
courante — une caméra « IA feu/fumée » du commerce n'est presque jamais une
caméra de guet forestière. Voir [HARDWARE.md](HARDWARE.md) pour la version
courte de cette distinction.

---

## Méthode et fiabilité des sources

Trois familles de sources, de fiabilité très inégale — c'est volontairement
signalé dans la dernière colonne de chaque tableau :

- **Presse et sites officiels vérifiés** (France Bleu, ICI, Objectif Gard,
  sites vendeurs directs) — fiable.
- **Agrégateurs B2B généralistes** (Alibaba "supplier guides", accio.com) —
  souvent des pages générées automatiquement à partir de catalogues, avec des
  chiffres de synthèse (« 80 % de réduction des faux positifs ») **non
  vérifiables**. Traités comme indicatifs, jamais comme des faits.
- **Reddit** — très peu de résultats exploitables trouvés malgré plusieurs
  formulations de recherche. La discussion communautaire sur ce sujet précis
  semble se tenir ailleurs (forums OpenIPC, Discord Pyronear, GitHub Issues)
  plutôt que sur Reddit.

Aucun prix ni URL ci-dessous n'est inventé : quand un vendeur ne publie pas de
prix (modèle « sur devis », courant en B2G), c'est noté tel quel plutôt que
comblé par une estimation.

---

## A. Solutions institutionnelles établies (tours de guet, portée longue)

*Le segment directement comparable à OpenVigie : détection à plusieurs km
depuis un point haut, IA de classification, alerte à un centre opérationnel.*

| Produit | Fonctionnalités / périmètre | Prix | URL | Dispo. rapide commune FR | Autres infos |
|---|---|---|---|---|---|
| **IQ FireWatch** (IQ Technologies for Earth and Space, Berlin) | Capteur optique breveté (mono + couleur + NIR nuit, option thermique), 360°, portée annoncée jusqu'à 64 km, IA + algorithmes à base de règles. Le système le plus ancien et le plus déployé au monde (24 M ha protégés, 4 continents, 200+ sites en Allemagne, présent au Portugal et jusqu'à la frontière belge). | Non public — modèle B2G/B2B, devis projet | [iq-firewatch.com](https://www.iq-firewatch.com/) | **Non.** Vente par projet avec intégrateur (ex. Climatec aux USA) ; aucun canal d'achat direct identifié ; pas de présence commerciale française confirmée à ce jour | 20+ ans d'exploitation réelle, la référence du secteur ; conçu avec le DLR (agence spatiale allemande) |
| **Exavision — Nemosys Fire** (filiale d'Ineo Défense/Equans France) | Caméras thermiques + visibles combinées, IA de classification (élimine poussière de carrière, brûlage agricole), jumeau numérique 3D du territoire, détection <3 min, portée 25–40 km annoncée, couverture ~700 km²/tour. Déployé sur les tours de guet DFCI existantes. | Non public — partenariat SDIS, financement Fonds Vert/ministère | [Reportage France Bleu](https://www.francebleu.fr/emissions/l-invite-de-7h45-d-ici-gard-lozere/les-cameras-thermiques-de-ce-chef-d-entreprise-gardois-revolutionnent-la-lutte-et-la-surveillance-des-risques-incendie-8816975) · [Objectif Gard](https://www.objectifgard.com/actualites/gard-campagne-feux-de-foret-2026-presentation-dun-dispositif-de-detection-precoce-165298.php) | **Non.** Exclusivement en partenariat institutionnel avec un SDIS ; aucune vente directe à une commune identifiée | **Le plus proche concurrent français d'OpenVigie.** 10 tours opérationnelles dans le Gard mi-2026, objectif 12 fin 2026 ; 150 départs de feu détectés en 2025, 80 depuis le 1ᵉʳ juillet 2026 |
| **Pano AI** | Caméras 360° + IA, vérification humaine avant alerte, déployé aux USA (Arizona Public Service, Colorado). | Non public | [Reportage FireRescue1](https://www.firerescue1.com/artificial-intelligence/ai-joins-the-wildfire-watch-across-the-west) | **Non.** B2G exclusivement, marché nord-américain principalement | Modèle « human-in-the-loop » avant transmission, proche de la philosophie d'OpenVigie |
| **ALERTCalifornia / ALERTWest** (UC San Diego + Axis Communications) | Réseau de 1 200+ caméras Axis, IA, ~3 600 incidents détectés/an dont plus de la moitié avant tout appel au 911. | Financement public (CAL FIRE, fonds fédéraux) | [Security Today](https://securitytoday.com/articles/2026/06/23/ai-camera-network-boosts-early-california-wildfire-detection.aspx) | **Non.** Infrastructure publique californienne, non commercialisée | Le réseau le plus vaste au monde en nombre de caméras ; données partiellement publiques (HPWREN) |
| **SmartForestFire** (Fraunhofer IIS/IML, Allemagne) | Caméras + capteurs environnementaux + réseau radio mioty®, jumeau numérique. | Projet pilote, non commercial | [wiot-group.com](https://wiot-group.com/think/en/news/smartforestfire-ai-wildfire-early-warning/) | **Non.** Phase de test (3 sites caméra fin 2026), pas encore un produit | Intéressant pour la fusion caméra+capteurs, mais non déployable en l'état |

---

## B. Solutions commerciales à portée EU, potentiellement plus accessibles

| Produit | Fonctionnalités / périmètre | Prix | URL | Dispo. rapide commune FR | Autres infos |
|---|---|---|---|---|---|
| **SmokeD** (Pologne) | Caméras Classic (mono, 82°) ou DuoVue (double objectif, 164°), 12,3 MP, IA cloud, détection ~10 min à 15 km de rayon. 5 unités Classic ou 3 DuoVue pour un 360°. App mobile et web pour opérateurs. | Non public — « contactez-nous » | [smokedsystem.com/detector](https://smokedsystem.com/detector/) | **Incertain.** Entreprise UE (Pologne), vente par contact commercial ; **aucun déploiement français confirmé** dans les sources trouvées (référence connue : Hidden Hills, Californie) | Fiches techniques publiques détaillées (résolution, consommation, PoE) — rare dans ce secteur |
| **Dryad Networks — Silvanet** (Allemagne) | **Technologie différente : capteurs de gaz au sol (CO, VOC, particules), pas de caméra.** Détection dès la phase de smoldering, réseau maillé LoRaWAN + satellite (Kinéis), autonomie 10–15 ans sans batterie remplaçable. | **Prix publics rares dans ce secteur** : capteur ≈48 €, passerelle maillage ≈371 €, passerelle bordure ≈549 € (tarifs 2022, à revérifier) | [dryad.net/wildfiresensor](https://www.dryad.net/wildfiresensor) · [détail prix (ST Blog)](https://blog.st.com/silvanet/) | **Incertain.** Société allemande établie, déploiements connus (Liban et autres) ; pas de confirmation française trouvée | Complémentaire plutôt que concurrent d'OpenVigie : détecte le smoldering avant toute fumée visible, mais ne localise pas visuellement et ne fait pas de levée de doute par image |

---

## C. Caméras « IA feu/fumée » grand public et pro — une nuance importante

**Découverte utile de cette recherche** : les gammes « AI fire/smoke detection »
des grands fabricants de vidéosurveillance (Dahua, Hikvision, ANNKE) et la
plupart des annonces Alibaba génériques **ne sont pas des équivalents
d'OpenVigie**. Vérifié sur fiche produit Dahua officielle (distributeur
agréé) : couverture annoncée de 30 à 60 m² pour la détection fumée, portée
flamme de 10 m. Ce sont des produits de **sécurité incendie de bâtiment ou de
site industriel** (entrepôt, transformateur), pas des caméras de guet à
plusieurs kilomètres — malgré un marketing qui emploie le même vocabulaire
« IA », « détection précoce », « fumée et flamme ».

| Produit | Fonctionnalités / périmètre | Prix | URL | Dispo. rapide commune FR | Autres infos |
|---|---|---|---|---|---|
| **Dahua DHI-HY-SAV849HAP-E** | Caméra IP 5 MP, capteur IR anti-incendie intégré, couverture fumée **30–60 m²**, focale fixe 2 mm. | Sur devis (distributeur B2B) | [By Demes Group](https://bydemes.com/en/brands/dahua/cctv/network-cameras/fire-smoke-gas/DAHUA-3306-FO) | **Oui pour un bâtiment**, via distributeur agréé (Demes Group dessert l'Espagne/France) ; **non pertinent pour de la forêt** | Portée bâtiment/site industriel, pas forêt |
| **Dahua DHI-HY-FT121LDP-TD1F4** | Dôme détection de flamme, thermique 1,2 mm + visible 4 mm, portée annoncée **10 m** (zone 10×10 cm). | Sur devis | [By Demes Group](https://bydemes.com/en/brands/dahua/cctv/network-cameras/fire-smoke-gas/DAHUA-3450-FO) | idem | Portée métrique, pas kilométrique |
| **ANNKE Custos / caméra IA fire** | Caméra 4 MP grand public, détection fumée/flamme + comportements (fumeur, téléphone), IP67, PoE. | Prix catalogue non trouvé dans cette recherche | [annke.com](https://www.annke.com/products/fire-detection-cam) | **Oui**, vente directe grand public, livraison rapide plausible | Positionnement résidentiel/petit commerce, pas watch-tower |
| **Annonces génériques AliExpress/Alibaba** (« forest fire detection camera », « AI smoke PTZ ») | Très hétérogène : blocs PTZ thermiques 5,5–240 mm de fabricants chinois (Zhuangyuanxiang, Baijiang, Jinan Hope Wish, Ikevision, etc.), allant d'un simple seuil colorimétrique à une classification IA non documentée/non auditée. | **De ~$200** (kit forest fire + drone, Alibaba) **à $24 500–31 000** (PTZ thermique longue portée avec SDK) | [Showroom Alibaba](https://www.alibaba.com/showroom/forest-fire-detection.html) · [Guide fournisseurs](https://www.alibaba.com/supplier/infrared-detection-fire-camera.html) | **Oui**, achat direct, mais délais et SAV très variables selon fournisseur | **Aucune preuve indépendante de performance trouvée** pour la quasi-totalité de ces annonces ; les pages « guide fournisseur » elles-mêmes ont l'air largement générées automatiquement (chiffres de synthèse non sourcés) |

---

## D. Logiciel open source — GitHub

| Projet | Fonctionnalités / périmètre | Prix | URL | Dispo. rapide commune FR | Autres infos |
|---|---|---|---|---|---|
| **Pyronear** (pyro-engine, pyro-api, pyro-vision, pyro-sys-setup) | **Le seul véritable équivalent open source d'OpenVigie.** Pipeline de détection edge, API d'alerte, plateforme de supervision, adaptateurs caméra **Reolink** et **Linovision/Hikvision (ISAPI)**, déploiement documenté sur Raspberry Pi. Poids de modèle publiés (YOLOv8s, YOLO11s) sous licence ouverte. Partenariats SDIS français actifs (Gard notamment). | **Gratuit** (logiciel) ; matériel à assembler soi-même (caméra Reolink $150–600 + Raspberry Pi ~$80) | [github.com/pyronear](https://github.com/pyronear) · [pyro-engine](https://github.com/pyronear/pyro-engine) · [pyro-sys-setup](https://github.com/pyronear/pyro-sys-setup) | **Oui**, matériel Reolink/Raspberry Pi disponible en quelques jours ; assemblage et calibration à faire soi-même | Contrairement à OpenVigie, cible des caméras grand public existantes plutôt que le firmware OpenIPC ; pas de MNT, pas d'étalonnage géométrique par trafic aérien, pas de secteurs angulaires |
| **AI For Mankind — wildfire-smoke-detection-camera** | Tutoriel Docker pour entraîner un détecteur sur images HPWREN annotées. Dataset de référence (2 192 images) largement réutilisé par le secteur académique. | Gratuit | [github.com/aiformankind/wildfire-smoke-detection-camera](https://github.com/aiformankind/wildfire-smoke-detection-camera) | N/A (tutoriel, pas un produit) | Base pédagogique utile, pas un système déployable |
| **oct-firecam** (Open Climate Tech) | Pipeline de collecte/inférence sur caméras existantes (HPWREN), publié sur PyPI. | Gratuit | [pypi.org/project/oct-firecam](https://pypi.org/project/oct-firecam) | N/A | Apache-2.0, orienté recherche |
| Dizaines de dépôts « fire-detection »/« wildfire-detection » (YOLOv5/v8/v11, Faster R-CNN, CNN maison) | Modèles de classification/détection entraînés sur des jeux publics ou Kaggle, précision annoncée 92–99 % sans protocole de validation terrain. | Gratuit | [github.com/topics/wildfire-detection](https://github.com/topics/wildfire-detection) | N/A | **Aucun ne constitue un système** (pas de géométrie, pas de gestion PTZ, pas d'alerte structurée) — ce sont des modèles isolés, exactement le composant qu'OpenVigie encapsule dans un pipeline complet |
| **ScorchVision** (app iOS, éditeur indépendant) | App gratuite qui reçoit le flux d'une caméra Raspberry Pi DIY et fait tourner un modèle de détection feu en local sur le téléphone. | Gratuit | [App Store](https://apps.apple.com/ca/app/scorchvision/id6748545495) | Oui, mais projet DIY perso, pas un système opérationnel | Découvert en marge des recherches GitHub/Reddit ; illustre l'écosystème hobbyiste autour de Raspberry Pi + caméra |

---

## E. Reddit et communautés — résultat honnête

Plusieurs formulations de recherche (`site:reddit.com`, requêtes ciblées
« wildfire detection camera DIY », « self-hosted smoke detection ») n'ont
remonté **aucune discussion Reddit substantielle et récente** sur des projets
comparables à OpenVigie. Ce que la recherche a surtout fait remonter à la
place : des projets étudiants/académiques sur GitHub (Raspberry Pi + OpenCV,
souvent à portée métrique, pas kilométrique), et le forum/écosystème OpenIPC
lui-même — plus pertinent qu'un fil Reddit générique pour ce sujet très
spécifique de portage de pilotes caméra.

**Conclusion honnête** : soit la discussion communautaire sur ce sujet précis
se tient ailleurs (Discord Pyronear, forums DFCI, forum OpenIPC), soit elle
est simplement peu volumineuse à ce jour.

---

## Synthèse pour OpenVigie

**Ce qui fait vraiment doublon** : rien d'identique en open source. Pyronear
est le seul projet comparable en ambition, et il est complémentaire plutôt que
concurrent — il cible des caméras grand public (Reolink) là où OpenVigie cible
le firmware OpenIPC pour un coût matériel inférieur et une autonomie sans
dépendance à un fabricant fermé. Un rapprochement ou un partage de modèles
avec Pyronear (déjà amorcé dans le README d'OpenVigie via Pyro-SDIS et les
poids YOLO11s) reste la piste la plus rentable plutôt qu'une reconstruction
parallèle.

**Ce qui manque à tous les concurrents commerciaux identifiés** :
- **Aucun n'est achetable rapidement par une petite commune française.**
  IQ FireWatch, Exavision, Pano AI, ALERTWest sont tous des ventes de projet
  B2G, sans prix public, avec un cycle de décision institutionnel.
- **Aucun n'est open source.** Une commune ou une association ne peut ni
  auditer, ni adapter, ni redéployer ces systèmes ailleurs.

**Le vrai risque de confusion pour un acheteur non averti** : les caméras
« IA feu/fumée » très facilement achetables (Dahua, ANNKE, la plupart des
annonces AliExpress) sont conçues pour un bâtiment, pas une forêt — 30 à 60 m²
de couverture, pas plusieurs kilomètres. C'est une distinction que le
README/HARDWARE.md d'OpenVigie pourrait utilement expliciter, puisque c'est
précisément le genre de confusion marketing que quelqu'un cherchant à
équiper un point haut pourrait faire en tapant « caméra IA détection incendie »
dans un moteur de recherche.

---

*Recherche menée le 16 août 2026 par recherche web progressive (15 requêtes,
3 récupérations de pages complètes) sur Alibaba, AliExpress, GitHub, Reddit et
sources adjacentes. Les prix et statuts de disponibilité évoluent vite dans ce
secteur ; à revérifier avant toute décision d'achat.*
