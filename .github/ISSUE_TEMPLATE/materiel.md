---
name: Retour matériel
about: Résultat d'un test sur une carte, un capteur ou une tête PTZ réels
labels: matériel
---

**C'est la contribution la plus utile au projet** : la matrice matérielle est
aujourd'hui théorique.

## Matériel
- Carte / module (référence exacte) :
- SoC :
- Capteur :
- Objectif (focale, motorisé ou non) :
- Firmware OpenIPC (version) :
- Tête PTZ le cas échéant :

## Sorties à joindre
```
openvigie hw            # sur la cible
openvigie hw --soc <soc> --sensor <capteur>
openvigie doctor -c <votre-site.yaml>
openvigie capabilities -c <votre-site.yaml>
```

## Ce qui a fonctionné / échoué

## Mesures si disponibles
- Répétabilité de preset (`site_survey.py`) :
- Vibration du mât :
- Portée réelle observée, et visibilité ce jour-là :
- Occupation mémoire et durée d'un cycle :
