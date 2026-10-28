SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.PHONY: build check dependencies
build:
	uv lock --check
	cd frontend && npm ci && npm run build

check:
	uv lock --check
	ruff check .
	ruff format --check .
	mypy
	cd frontend && npm run check

dependencies:
	@test -n "$(BASELINE_PROJECT)" || { echo 'Set BASELINE_PROJECT to the exact runtime image source'; exit 1; }
	uv run --frozen --group build python prepare_dependencies.py --baseline-project "$(BASELINE_PROJECT)"
