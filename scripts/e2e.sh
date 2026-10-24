#!/usr/bin/env bash
# End-to-end proof, run in CI: ship a bad release and assert the healer rolls it back unaided.
set -euo pipefail

NS=shop
TIMEOUT=${TIMEOUT:-480}

echo "Letting traffic build a healthy baseline..."
sleep 60

echo "Shipping a bad release (v2, 50% of requests fail)..."
kubectl -n "$NS" set env deployment/shop-api APP_VERSION=v2 ERROR_RATE=0.5
kubectl -n "$NS" rollout status deployment/shop-api --timeout=180s
start=$SECONDS

until [[ -n "$(kubectl -n "$NS" get deployment shop-api -o jsonpath='{.metadata.annotations.healer\.io/rolled-back-from}')" ]]; do
  if (( SECONDS - start > TIMEOUT )); then
    echo "FAIL: no rollback within ${TIMEOUT}s"
    kubectl -n healer logs deployment/healer --tail=50
    exit 1
  fi
  sleep 10
done

version=$(kubectl -n "$NS" get deployment shop-api \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="APP_VERSION")].value}')
if [[ "$version" != "v1" ]]; then
  echo "FAIL: rolled back to '$version', expected v1"
  exit 1
fi
kubectl -n "$NS" rollout status deployment/shop-api --timeout=180s

echo "PASS: healer detected the bad release and restored v1 $((SECONDS - start))s after it went live"
kubectl -n healer logs deployment/healer --tail=20
