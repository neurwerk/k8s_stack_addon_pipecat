SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.PHONY: build frontend-dependencies build-prepared check dependencies
build: frontend-dependencies
	$(MAKE) build-prepared

frontend-dependencies:
	uv lock --check
	cd frontend && npm ci

build-prepared:
	cd frontend && npm run build

check:
	uv lock --check
	ruff check .
	ruff format --check .
	mypy
	cd frontend && npm run check

dependencies:
	@test -n "$(BASELINE_PROJECT)" || { echo 'Set BASELINE_PROJECT to the exact runtime image source'; exit 1; }
	uv run --frozen --group build python prepare_dependencies.py --baseline-project "$(BASELINE_PROJECT)"
