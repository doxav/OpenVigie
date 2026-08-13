#!/usr/bin/env bash
# Déploiement de OpenVigie sur une caméra OpenIPC.
#
# Volontairement agnostique du SoC : le script interroge la carte (ipctool,
# os-release, cli) et adapte le profil. Il fonctionne donc sur HiSilicon, Goke,
# SigmaStar ou Ingenic dès lors que la carte tourne sous OpenIPC.
#
#   ./scripts/openipc_deploy.sh 192.168.1.64                # inventaire + profil détection
#   ./scripts/openipc_deploy.sh 192.168.1.64 --apply        # applique le profil majestic
#   ./scripts/openipc_deploy.sh 192.168.1.64 --push-agent   # copie l'agent embarqué
#
set -euo pipefail

HOST="${1:-}"
[[ -z "$HOST" ]] && { echo "usage: $0 <ip-camera> [--apply] [--push-agent]" >&2; exit 2; }
shift || true

USER_NAME="${OPENIPC_USER:-root}"
APPLY=0
PUSH_AGENT=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --push-agent) PUSH_AGENT=1 ;;
    *) echo "option inconnue: $arg" >&2; exit 2 ;;
  esac
done

SSH="ssh -o StrictHostKeyChecking=accept-new ${USER_NAME}@${HOST}"

echo "== Inventaire de ${HOST} =="
$SSH 'cat /etc/os-release 2>/dev/null | head -3' || { echo "connexion SSH impossible" >&2; exit 1; }

echo
echo "-- SoC et capteur (ipctool) --"
SOC=""
SENSOR=""
if INV=$($SSH 'ipctool 2>/dev/null' || true); then
  echo "$INV" | head -25
  SOC=$(echo "$INV"  | sed -n 's/.*chip[Nn]ame *: *\([A-Za-z0-9_.-]*\).*/\1/p' | head -1 | tr 'A-Z' 'a-z')
  SENSOR=$(echo "$INV" | sed -n 's/.*model *: *\([A-Za-z0-9_.-]*\).*/\1/p' | head -1 | tr 'a-z' 'A-Z')
fi
[[ -z "$SOC" ]] && echo "ipctool indisponible : renseigner platform.soc manuellement dans la config."

echo
echo "-- Espace disponible --"
$SSH 'df -h /tmp /overlay 2>/dev/null | head -5' || true

if [[ -n "$SOC" && -n "$SENSOR" ]]; then
  echo
  echo "== Verdict de compatibilité =="
  PYTHONPATH="$(dirname "$0")/../src" python3 -m openvigie.cli hw --soc "$SOC" --sensor "$SENSOR" || true
fi

echo
echo "== Profil majestic recommandé pour la détection =="
PYTHONPATH="$(dirname "$0")/../src" python3 -m openvigie.cli majestic --host "$HOST" --user "$USER_NAME"

if [[ $APPLY -eq 1 ]]; then
  echo
  echo "== Application du profil =="
  # On sauvegarde la configuration existante avant toute modification : une
  # caméra sur pylône ne se reconfigure pas facilement à la main.
  $SSH 'cp /etc/majestic.yaml /etc/majestic.yaml.bak-openvigie 2>/dev/null || true'
  echo "   sauvegarde : /etc/majestic.yaml.bak-openvigie"
  PYTHONPATH="$(dirname "$0")/../src" python3 -m openvigie.cli majestic --host "$HOST" --user "$USER_NAME" \
    | grep -E '^ssh ' | while read -r cmd; do
        echo "   $cmd"
        eval "$cmd" || echo "     (échec — clé absente sur cette version de majestic, sans gravité)"
      done
  $SSH 'killall -HUP majestic 2>/dev/null || true'
  echo "   majestic rechargé"
fi

if [[ $PUSH_AGENT -eq 1 ]]; then
  echo
  echo "== Copie de l'agent embarqué =="
  # Sur une carte caméra, seul le coeur NumPy est déployé : l'agent capture,
  # exécute l'étage classique et remonte les candidats. La classification lourde
  # reste au centre si le SoC n'a pas de moteur neuronal.
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  # AUDIT P0-02 (corrigé 0.4.0) : la liste de modules était tenue à la main et
  # omettait events.py, transport.py, masking.py, dem.py et correlation.py.
  # `import openvigie` réussissait, `import openvigie.pipeline` échouait — et le script
  # affichait quand même « opérationnel ». On copie désormais le paquet entier
  # et on vérifie l'import du pipeline, pas seulement celui du paquet.
  mkdir -p "$TMP/openvigie"
  cp "$(dirname "$0")/../src/openvigie/"*.py "$TMP/openvigie/"
  tar czf "$TMP/openvigie-edge.tgz" -C "$TMP" openvigie
  SIZE=$(du -h "$TMP/openvigie-edge.tgz" | cut -f1)
  echo "   paquet: $SIZE"
  scp -q "$TMP/openvigie-edge.tgz" "${USER_NAME}@${HOST}:/tmp/" && echo "   copié dans /tmp/openvigie-edge.tgz"
  # Vérification réelle : le pipeline doit s'importer et le cœur doit tourner.
  $SSH 'cd /tmp && tar xzf openvigie-edge.tgz && OPENVIGIE_FORCE_NUMPY=1 python3 -c "
import sys; sys.path.insert(0, \"/tmp\")
import openvigie, openvigie.pipeline, openvigie.events, openvigie.transport, openvigie.masking
print(\"OpenVigie\", openvigie.__version__, \"- pipeline importable sur la cible\")
"' || echo "   ÉCHEC : Python 3 absent ou paquet incomplet. Utiliser le mode calcul externe."

  echo "   NOTE : /tmp est volatil sur OpenIPC. Une installation persistante et"
  echo "          supervisée reste à faire (AUDIT P0-03, roadmap v0.4)."
fi

cat <<MSG

Étapes suivantes :
  1. openvigie hw                                  (sur la carte, si Python y est disponible)
  2. python scripts/site_survey.py --snapshot-url http://${HOST}/image.jpg --config <site.yaml>
  3. python scripts/record_baseline.py --camera V00=http://${HOST}/image.jpg --days 30
MSG
