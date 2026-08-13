#!/usr/bin/env bash
# Initialisation de l'environnement de développement OpenVigie.
#
#   ./scripts/bootstrap.sh            # poste de dev complet
#   ./scripts/bootstrap.sh --edge     # cible embarquée : NumPy seul
#
set -euo pipefail

EDGE=0
[[ "${1:-}" == "--edge" ]] && EDGE=1

PY=${PYTHON:-python3}
VENV=${VENV:-.venv}

echo "== OpenVigie bootstrap =="
"$PY" --version

if [[ ! -d "$VENV" ]]; then
  echo "-- création du venv $VENV"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip -q

if [[ $EDGE -eq 1 ]]; then
  echo "-- installation minimale (cible embarquée : NumPy + PyYAML uniquement)"
  pip install -e . -q
  export OPENVIGIE_FORCE_NUMPY=1
else
  echo "-- installation complète (dev + full + ptz)"
  pip install -e ".[dev,full,ptz]" -q
fi

echo "-- vérification de l'installation"
python -c "import openvigie; print('OpenVigie', openvigie.__version__)"

echo "-- génération des configurations de référence"
mkdir -p config/tiers
for tier in minimal medium full; do
  openvigie init "$tier" -o "config/tiers/${tier}.yaml" --site-id "site-${tier}" --force >/dev/null
  echo "   config/tiers/${tier}.yaml"
done

echo "-- capacités réellement disponibles"
openvigie capabilities -t full | tail -3 | sed 's/^/   /'

echo "-- vérifications statiques de configuration"
for tier in minimal medium full; do
  echo "   [$tier]"
  # doctor renvoie un code non nul si une capacité déclarée est absente : c'est
  # attendu tant que NNIE/ONNX ne sont pas installés (AUDIT P0-05).
  openvigie doctor -c "config/tiers/${tier}.yaml" | sed 's/^/     /' || true
done

echo "-- schéma d'événement"
openvigie schema | head -3 | sed 's/^/   /'

echo "-- validation de l'étalonnage par trafic aérien"
openvigie calibrate -t full --simulate >/dev/null && echo "   pose vraie retrouvée (OK)"

echo "-- autotest du pipeline (positif + négatif)"
openvigie selftest -t medium --mode plume  >/dev/null && echo "   panache : alerte émise  (OK)"
openvigie selftest -t medium --mode cloud  >/dev/null && echo "   nuage   : aucune alerte (OK)"

if [[ $EDGE -eq 0 ]]; then
  echo "-- suite de tests"
  pytest -q
  echo "-- suite de tests en mode embarqué (sans OpenCV/SciPy)"
  OPENVIGIE_FORCE_NUMPY=1 pytest -q
fi

cat <<'MSG'

Bootstrap terminé.

  source .venv/bin/activate
  openvigie plan   -c config/tiers/medium.yaml     # dimensionnement et budget de balayage
  openvigie doctor -c config/tiers/medium.yaml     # vérifications de configuration
  openvigie ptz-test -t minimal                    # trames Pelco-D et fenêtres d'analyse
  openvigie schema                                 # schéma d'événement et cycle de vie
  openvigie viewshed --synthetic                   # ce que le relief laisse voir
  openvigie outbox --dir data/outbox               # file d'attente hors ligne
  openvigie calibrate -t full --simulate           # étalonnage par trafic aérien

Avant toute mise en service, exécuter la campagne de mesure :
  python scripts/site_survey.py --config config/tiers/minimal.yaml --host <ip-camera>

MSG
