SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
BASE ?=
ADDON_BASE ?= $(BASE)
TOOLING := uv run --locked --project tooling
TOOLING_IMAGE := ghcr.io/neurwerk/k8s-stack-addon-pipecat-tooling
CHARTS := $(sort $(shell find charts -name Chart.yaml -printf '%h\n'))

.PHONY: check addon-check lint schema compose static test build image-build tooling-check tooling-integration tooling-build
check: tooling-check addon-check lint schema compose static test build

addon-check:
	@test -n "$(BASE)" || { echo 'Set BASE to the explicit Base candidate checkout'; exit 1; }
	$(TOOLING) pipecat-validate "$(CURDIR)"
	$(TOOLING) addon-check "$(CURDIR)" --base "$(BASE)"

lint:
	uv run --frozen ruff check src tests apps/simple-chatbot
	uv run --frozen ruff format --check src tests apps/simple-chatbot
	@for chart in $(CHARTS); do helm lint --strict "$$chart"; helm lint --strict "$$chart" --namespace pipecat -f validation/values.yaml; done

schema:
	@for chart in $(CHARTS); do helm template pipecat "$$chart" --namespace pipecat -f validation/values.yaml | kubeconform -strict -summary -ignore-missing-schemas; done
	@packages="$$($(TOOLING) pipecat-validate "$(CURDIR)" --list-packages)"; for package in $$packages; do kustomize build "$$package" | kubeconform -strict -summary -ignore-missing-schemas; done

compose:
	docker compose config --quiet

static:
	uv run --frozen mypy
	cd apps/simple-chatbot && ../../.venv/bin/mypy
	cd apps/simple-chatbot/frontend && npm run check

build:
	uv build --no-sources
	$(MAKE) -C apps/simple-chatbot build dependencies BASELINE_PROJECT="$(CURDIR)"
	$(TOOLING) pipecat-apps validate --apps-dir apps --app simple-chatbot

test:
	@test -n "$(BASE)" || { echo 'Set BASE to the explicit Base candidate checkout'; exit 1; }
	BASE="$(BASE)" PYTHONPATH="$(CURDIR)/apps/simple-chatbot" uv run --frozen python -m unittest discover -s tests -v
	PYTHONPATH="$(CURDIR)/apps/simple-chatbot" uv run --frozen python -m unittest discover -s apps/simple-chatbot/tests -v
	uv run --frozen python -m unittest discover -s validation -p test_charts.py -v

image-build:
	docker build --tag neurwerk-pipecat:local .

tooling-check:
	@test -f tooling/uv.lock || { echo 'Release prerequisite missing: publish shared Tooling 0.15.8/setup 0.8.4 sources, then generate and review tooling/uv.lock'; exit 1; }
	cd tooling && uv lock --check
	$(TOOLING) ruff check tooling
	$(TOOLING) ruff format --check tooling
	$(TOOLING) ty check --project tooling
	$(MAKE) tooling-integration ADDON_BASE="$(ADDON_BASE)"
	cd tooling && uv build --no-sources

tooling-integration:
	@test -n "$(ADDON_BASE)" || { echo 'Required integration prerequisite missing: set ADDON_BASE explicitly'; exit 1; }
	ADDON_BASE="$(ADDON_BASE)" $(TOOLING) pytest tooling/tests

tooling-build:
	@test -n "$(VERSION)" -a -n "$(REVISION)" || { echo 'Set VERSION and REVISION for image metadata'; exit 1; }
	docker build --build-arg VERSION="$(VERSION)" --build-arg REVISION="$(REVISION)" --tag "$(TOOLING_IMAGE):$(VERSION)" tooling
