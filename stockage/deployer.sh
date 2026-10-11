#!/usr/bin/env bash
# Stockage S3 de l'« équipe stockage » (module 6) : RustFS 1.0.1 dans l'espace de noms « stockage »,
# collecteur OpenTelemetry, puis préparation (bucket, versionnement, cycle de vie, politiques, comptes).
#   ./stockage/deployer.sh              déploie et prépare (idempotent)
#   ./stockage/deployer.sh --vider      vide le dossier scenes/ (toutes les versions)
#   ./stockage/deployer.sh --essai      tests de recevabilité (identifiants temporaires, refus attendus)
#   ./stockage/deployer.sh --cles       remet les clés de dépôt et des liens au studio (secret « stockage »)
# Secrets dans stockage/.env (créé au premier appel, hors git). Prérequis : docker, kubectl, k3d (ou un
# registre : remplacez « k3d image import » par un docker push).
set -euo pipefail
cd "$(dirname "$0")"
ENV=.env
[ -f "$ENV" ] || {
  echo "S3_RACINE_CLE=$(openssl rand -hex 10)" > "$ENV"
  for n in RACINE DEPOT LIENS CONSULTATION; do echo "S3_${n}_SECRET=$(openssl rand -hex 20)" >> "$ENV"; done
}
set -a; . "./$ENV"; set +a
CLUSTER=${K3D_CLUSTER:-tterm}

job() {  # lance le Job de préparation avec le mode donné, attend sa fin, affiche sa sortie
  kubectl delete job preparation -n stockage --ignore-not-found
  sed "s/\"MODE\"/\"$1\"/" preparation.yaml | kubectl apply -f -
  kubectl wait --for=condition=complete job/preparation -n stockage --timeout=180s || true
  kubectl logs job/preparation -n stockage
}

case "${1:-}" in
  --vider) job --vider; exit ;;
  --essai) job --essai; exit ;;
  --cles)
    kubectl create secret generic stockage -n studio3d \
      --from-literal=depot-cle=studio3d-depot --from-literal=depot-secret="$S3_DEPOT_SECRET" \
      --from-literal=liens-cle=studio3d-liens --from-literal=liens-secret="$S3_LIENS_SECRET" \
      --dry-run=client -o yaml | kubectl apply -f -
    exit ;;
esac

docker build -t stockage-preparation:1 preparation
k3d image import stockage-preparation:1 -c "$CLUSTER"
kubectl create namespace stockage --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic s3-racine -n stockage --from-literal=cle="$S3_RACINE_CLE" \
  --from-literal=secret="$S3_RACINE_SECRET" --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic comptes -n stockage --from-literal=depot-secret="$S3_DEPOT_SECRET" \
  --from-literal=liens-secret="$S3_LIENS_SECRET" --from-literal=consultation-secret="$S3_CONSULTATION_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap stockage-config -n stockage \
  --from-file=depot.json=politiques/depot.json --from-file=liens.json=politiques/liens.json \
  --from-file=consultation.json=politiques/consultation.json --from-file=cycle-de-vie.json=cycle-de-vie.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap otel-collecteur -n stockage --from-file=collecteur.yaml=collecteur.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s.yaml
kubectl rollout status deployment/s3 -n stockage --timeout=180s
kubectl rollout status deployment/otel-collecteur -n stockage --timeout=180s
job --preparer
