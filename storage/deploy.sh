#!/usr/bin/env bash
# S3 storage of the "storage team" (module 6): RustFS 1.0.1 in the "storage" namespace,
# OpenTelemetry collector, then preparation (bucket, versioning, lifecycle, policies, accounts).
#   ./storage/deploy.sh              deploys and prepares (idempotent)
#   ./storage/deploy.sh --empty      empties the scenes/ folder (all versions)
#   ./storage/deploy.sh --test       acceptance tests (temporary credentials, expected refusals)
#   ./storage/deploy.sh --keys       hands the upload and links keys to the studio ("storage" secret)
# Secrets in storage/.env (created on the first run, outside git). Prerequisites: docker, kubectl, k3d (or a
# registry: replace "k3d image import" with a docker push).
set -euo pipefail
cd "$(dirname "$0")"
ENV=.env
[ -f "$ENV" ] || {
  echo "S3_ROOT_KEY=$(openssl rand -hex 10)" > "$ENV"
  for n in ROOT UPLOAD LINKS VIEWER; do echo "S3_${n}_SECRET=$(openssl rand -hex 20)" >> "$ENV"; done
}
set -a; . "./$ENV"; set +a
CLUSTER=${K3D_CLUSTER:-tterm}

job() {  # runs the prepare Job with the given mode, waits for it to finish, prints its output
  kubectl delete job prepare -n storage --ignore-not-found
  sed "s/\"MODE\"/\"$1\"/" prepare.yaml | kubectl apply -f -
  kubectl wait --for=condition=complete job/prepare -n storage --timeout=180s || true
  kubectl logs job/prepare -n storage
}

case "${1:-}" in
  --empty) job --empty; exit ;;
  --test) job --test; exit ;;
  --keys)
    kubectl create secret generic storage -n studio3d \
      --from-literal=upload-key=studio3d-upload --from-literal=upload-secret="$S3_UPLOAD_SECRET" \
      --from-literal=links-key=studio3d-links --from-literal=links-secret="$S3_LINKS_SECRET" \
      --dry-run=client -o yaml | kubectl apply -f -
    exit ;;
esac

docker build -t storage-prepare:1 prepare
k3d image import storage-prepare:1 -c "$CLUSTER"
kubectl create namespace storage --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic s3-root -n storage --from-literal=key="$S3_ROOT_KEY" \
  --from-literal=secret="$S3_ROOT_SECRET" --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic accounts -n storage --from-literal=upload-secret="$S3_UPLOAD_SECRET" \
  --from-literal=links-secret="$S3_LINKS_SECRET" --from-literal=viewer-secret="$S3_VIEWER_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap storage-config -n storage \
  --from-file=upload.json=policies/upload.json --from-file=links.json=policies/links.json \
  --from-file=viewer.json=policies/viewer.json --from-file=lifecycle.json=lifecycle.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap otel-collector -n storage --from-file=collector.yaml=collector.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s.yaml
kubectl rollout status deployment/s3 -n storage --timeout=180s
kubectl rollout status deployment/otel-collector -n storage --timeout=180s
job --prepare
