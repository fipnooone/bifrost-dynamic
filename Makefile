SHELL := /bin/bash
.DEFAULT_GOAL := help
IMAGE ?= bifrost-dynamic:local
.PHONY: help test prepare build smoke validate
help:
	@printf '%s\n' 'make test      repository checks and Python tests (no Docker)' 'make build     build linux/amd64 runtime and CI plugin' 'make smoke     test the built image on native Linux/amd64' 'make validate  build and run the real Bifrost/plugin test'
test:
	python3 scripts/check.py
	python3 -m unittest discover -s tests -p 'test_*.py' -v
	bash -n scripts/build.sh scripts/prepare.sh scripts/publish.sh
	sh -n scripts/collect-licenses.sh
prepare:
	./scripts/prepare.sh
build:
	IMAGE="$(IMAGE)" ./scripts/build.sh
smoke:
	python3 scripts/smoke.py "$(IMAGE)"
validate: build
	$(MAKE) smoke IMAGE="$(IMAGE)"
