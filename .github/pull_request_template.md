## Ce que fait ce changement

## Vérifications

- [ ] `make test-all` passe (avec **et** sans OpenCV/SciPy)
- [ ] `make lint` passe
- [ ] Les nouveaux seuils sont exprimés en unités physiques, pas en pixels
- [ ] Les fonctions parlant au matériel prennent leurs dépendances en argument
- [ ] Un test négatif accompagne tout nouveau critère de détection
- [ ] Aucune dégradation silencieuse : elle apparaît dans `summary()` et le heartbeat
- [ ] Aucune dépendance copyleft ajoutée au cœur
- [ ] `openvigie capabilities` reste exact si une capacité a été ajoutée ou retirée

## Impact sur la maturité

Ce changement fait-il passer une fonction de « code de bibliothèque » à
« intégrée de bout en bout » ou « validée terrain » ? Si oui, mettre à jour
ROADMAP.md.
