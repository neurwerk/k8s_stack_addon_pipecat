SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
WORKSPACE_ROOT ?= $(abspath ../..)
ADDON_TOOLING ?= $(WORKSPACE_ROOT)/tooling
CHARTS := $(sort $(shell find charts -name Chart.yaml -printf '%h\n'))
PACKAGES := namespaces/pipecat pipecat/setup pipecat/app

.PHONY: check addon-check lint schema compose static test build image-build
check: addon-check lint schema compose static test build

addon-check:
	uv run --frozen --project "$(ADDON_TOOLING)" addon-check "$(CURDIR)" --base "$(WORKSPACE_ROOT)/base"

lint:
	uv run --frozen ruff check src tests
	uv run --frozen ruff format --check src tests
	@for chart in $(CHARTS); do helm lint --strict "$$chart"; helm lint --strict "$$chart" --namespace pipecat -f validation/values.yaml; done

schema:
	@for chart in $(CHARTS); do helm template pipecat "$$chart" --namespace pipecat -f validation/values.yaml | kubeconform -strict -summary -ignore-missing-schemas; done
	@for package in $(PACKAGES); do kustomize build "releases/$$package" | kubeconform -strict -summary -ignore-missing-schemas; done

compose:
	docker compose config --quiet

static:
	uv run --frozen mypy
	cd frontend && npm run check

build:
	uv build --no-sources
	cd frontend && npm run build

test:
	uv run --frozen python -m unittest discover -s tests -v

image-build:
	docker build --tag neurwerk-pipecat:local .
