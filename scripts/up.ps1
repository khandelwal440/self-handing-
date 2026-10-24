<#
.SYNOPSIS
  Builds the whole self-healing lab on a local kind cluster (Windows equivalent of `make up`).
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Cluster = 'self-healing'
$KpsVersion = '65.1.1'
$ChaosMeshVersion = '2.7.2'

function Exec([scriptblock]$Block) {
  & $Block
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Block" }
}

function Require-Tool([string]$Name, [string]$WingetId) {
  if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
  Write-Host "==> Installing $Name with winget" -ForegroundColor Cyan
  Exec { winget install --id $WingetId -e --accept-source-agreements --accept-package-agreements }
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "$Name was installed but is not on PATH yet. Open a new terminal and run this script again."
  }
}

function Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }

Push-Location $Root
try {
  Step 'Checking prerequisites'
  Require-Tool kind Kubernetes.kind
  Require-Tool helm Helm.Helm
  Require-Tool kubectl Kubernetes.kubectl
  docker info --format '{{.ServerVersion}}' | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop and try again.' }

  Step "Creating kind cluster '$Cluster' (1 control-plane + 3 workers)"
  if ((kind get clusters) -notcontains $Cluster) {
    Exec { kind create cluster --config cluster/kind-config.yaml }
  } else {
    Write-Host 'Cluster already exists, reusing it.'
  }

  Step 'Building and loading images'
  Exec { docker build -t shop-api:v1 app }
  Exec { docker build -t healer:v1 healer }
  Exec { kind load docker-image shop-api:v1 healer:v1 --name $Cluster }

  Step 'Installing kube-prometheus-stack (Prometheus, Alertmanager, Grafana)'
  Exec { helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update }
  Exec { helm upgrade --install kps prometheus-community/kube-prometheus-stack --namespace monitoring --create-namespace --version $KpsVersion -f helm-values/kube-prometheus-stack.yaml --wait --timeout 10m }

  Step 'Installing Chaos Mesh'
  Exec { helm repo add chaos-mesh https://charts.chaos-mesh.org --force-update }
  Exec { helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh --namespace chaos-mesh --create-namespace --version $ChaosMeshVersion -f helm-values/chaos-mesh.yaml --wait --timeout 10m }

  Step 'Deploying alert rules, dashboard, healer and shop-api'
  Exec { kubectl apply -k deploy/monitoring }
  Exec { kubectl apply -k deploy/healer }
  Exec { kubectl apply -k deploy/app }
  Exec { kubectl -n healer rollout status deployment/healer --timeout=180s }
  Exec { kubectl -n shop rollout status deployment/shop-api --timeout=180s }

  Write-Host "`nReady." -ForegroundColor Green
  Write-Host '  Grafana       http://localhost:3000/d/self-healing   (admin / admin)'
  Write-Host '  Prometheus    http://localhost:9090/alerts'
  Write-Host '  Alertmanager  http://localhost:9093'
  Write-Host '  shop-api      http://localhost:8080/api/products'
  Write-Host '  Chaos Mesh    kubectl -n chaos-mesh port-forward svc/chaos-dashboard 2333  ->  http://localhost:2333'
  Write-Host "`nNext: ./scripts/demo.ps1 bad-release"
} finally {
  Pop-Location
}
