---
name: Problème de détection
about: Faux positif, faux négatif, ou localisation erronée
labels: détection
---

## Type
- [ ] Faux positif (alerte sans feu)
- [ ] Faux négatif (feu manqué)
- [ ] Localisation erronée

## Contexte
- Version de OpenVigie :
- Mode d'exploitation (`measure` / `shadow` / `alert`) :
- Tier et backend effectif (`openvigie capabilities`) :
- Site, saison, heure, météo, visibilité estimée :

## Sorties à joindre
```
openvigie doctor -c site.yaml
openvigie plan   -c site.yaml
```
Les features de l'événement (`event.features`) et, si possible, la séquence
d'images — **après application des masques de confidentialité**.

## Attendu / observé
