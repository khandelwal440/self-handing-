<#
.SYNOPSIS
  Break the cluster on purpose, then watch it heal itself.
.EXAMPLE
  ./scripts/demo.ps1 bad-release
  ./scripts/demo.ps1 reset
#>
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('pod-kill', 'unready', 'leak', 'bad-release', 'cpu-stress', 'node-failure', 'reset', 'status')]
  [string]$Scenario
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Cluster = 'self-healing'
$Selector = 'app.kubernetes.io/name=shop-api'

function Exec([scriptblock]$Block) {
  & $Block
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Block" }
}

function Get-ShopPod {
  $pod = kubectl -n shop get pods -l $Selector --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'
  if (-not $pod) { throw 'No running shop-api pod found. Is the lab up? (./scripts/up.ps1)' }
  return $pod
}

function Explain([string]$Breaks, [string]$Expect) {
  Write-Host "`nCHAOS IN    $Breaks" -ForegroundColor Red
  Write-Host "RECOVERY    $Expect" -ForegroundColor Green
  Write-Host "`nWatch it happen:"
  Write-Host '  Grafana   http://localhost:3000/d/self-healing'
  Write-Host '  Alerts    http://localhost:9090/alerts'
  Write-Host '  Healer    kubectl -n healer logs -f deployment/healer'
}

switch ($Scenario) {
  'pod-kill' {
    Exec { kubectl apply -f "$Root/chaos/pod-kill-schedule.yaml" }
    Explain 'Chaos Mesh kills a random shop-api pod every 2 minutes.' `
      'Kubernetes replaces it by itself (ReplicaSet). The healer should do nothing: this is the baseline it must not interfere with.'
  }
  'unready' {
    $pod = Get-ShopPod
    Exec { kubectl -n shop exec $pod -- python app.py fault unready }
    Explain "$pod's readiness probe now fails forever. Kubernetes stops routing to it but never restarts it." `
      "PodStuckNotReady fires after 1m; the healer deletes $pod and a fresh pod takes its place."
  }
  'leak' {
    $pod = Get-ShopPod
    Exec { kubectl -n shop exec $pod -- python app.py fault leak }
    Explain "$pod starts leaking ~1 MiB every 3s toward its 128Mi limit." `
      'PodMemoryNearLimit fires at 80% of the limit; the healer recycles the pod before the OOM killer does.'
  }
  'bad-release' {
    Exec { kubectl -n shop set env deployment/shop-api APP_VERSION=v2 ERROR_RATE=0.5 }
    Explain 'A bad release (v2) rolls out: half of all requests now return 500.' `
      'HighErrorRate fires; the healer rolls shop-api back to the previous revision (v1). Watch "Running version".'
  }
  'cpu-stress' {
    Exec { kubectl apply -f "$Root/chaos/cpu-stress.yaml" }
    Explain 'Chaos Mesh burns CPU inside every shop-api container for 6 minutes.' `
      'HighLatencyP95 fires; the healer scales shop-api up (capped at 8 by the healer.io/max-replicas annotation).'
  }
  'node-failure' {
    $nodes = kubectl -n shop get pods -l $Selector -o jsonpath='{.items[*].spec.nodeName}'
    $node = ($nodes -split ' ') | Where-Object { $_ -and $_ -notlike '*control-plane*' } | Select-Object -First 1
    if (-not $node) { throw 'No worker node is running shop-api pods.' }
    Exec { docker stop $node }
    Explain "Worker node $node is gone (its container was stopped)." `
      "NodeNotReady fires; the healer cordons $node and evicts its pods (~2 min, vs Kubernetes' default 5 min). Run 'reset' to bring the node back; the healer then uncordons it."
  }
  'reset' {
    Write-Host 'Removing chaos experiments...'
    kubectl delete -f "$Root/chaos/" --ignore-not-found | Out-Null
    Write-Host 'Restarting stopped kind nodes...'
    $stopped = docker ps -a --filter "label=io.x-k8s.kind.cluster=$Cluster" --filter 'status=exited' --format '{{.Names}}'
    foreach ($node in $stopped) { if ($node) { Exec { docker start $node } } }
    Write-Host 'Restoring shop-api to v1 with 3 replicas...'
    Exec { kubectl -n shop set env deployment/shop-api APP_VERSION=v1 ERROR_RATE=0 }
    Exec { kubectl -n shop scale deployment/shop-api --replicas=3 }
    Exec { kubectl -n shop rollout restart deployment/shop-api }
    Exec { kubectl -n shop rollout status deployment/shop-api --timeout=180s }
    Write-Host 'Lab reset.' -ForegroundColor Green
  }
  'status' {
    kubectl get nodes
    Write-Host ''
    kubectl -n shop get pods -o wide
    Write-Host "`nRecent healer decisions:"
    kubectl -n healer logs deployment/healer --tail=15
  }
}
