CLUSTER            ?= self-healing
KPS_VERSION        ?= 65.1.1
CHAOS_MESH_VERSION ?= 2.7.2

.PHONY: up down cluster images monitoring chaos-mesh deploy test e2e

up: cluster images monitoring chaos-mesh deploy

cluster:
	kind get clusters | grep -qx '$(CLUSTER)' || kind create cluster --config cluster/kind-config.yaml

images:
	docker build -t shop-api:v1 app
	docker build -t healer:v1 healer
	kind load docker-image shop-api:v1 healer:v1 --name $(CLUSTER)

monitoring:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm upgrade --install kps prometheus-community/kube-prometheus-stack \
	  --namespace monitoring --create-namespace --version $(KPS_VERSION) \
	  -f helm-values/kube-prometheus-stack.yaml --wait --timeout 10m

chaos-mesh:
	helm repo add chaos-mesh https://charts.chaos-mesh.org --force-update
	helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
	  --namespace chaos-mesh --create-namespace --version $(CHAOS_MESH_VERSION) \
	  -f helm-values/chaos-mesh.yaml --wait --timeout 10m

deploy:
	kubectl apply -k deploy/monitoring
	kubectl apply -k deploy/healer
	kubectl apply -k deploy/app
	kubectl -n healer rollout status deployment/healer --timeout=180s
	kubectl -n shop rollout status deployment/shop-api --timeout=180s

test:
	cd healer && python -m pytest -q

e2e:
	bash scripts/e2e.sh

down:
	kind delete cluster --name $(CLUSTER)
