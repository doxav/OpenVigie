.PHONY: help install test test-edge test-all lint plan doctor selftest hw schema capabilities viewshed calibrate clean

help:
	@echo "install    installe le paquet en mode développement"
	@echo "test       suite de tests (avec OpenCV/SciPy)"
	@echo "test-edge  suite de tests en NumPy pur (comme sur carte caméra)"
	@echo "test-all   les deux — c'est ce que doit passer toute contribution"
	@echo "lint       ruff"
	@echo "plan       dimensionnement des trois tiers"
	@echo "doctor     vérifications de configuration des trois tiers"
	@echo "selftest   pipeline bout en bout, cas positif et négatif"
	@echo "hw         matrice de compatibilité OpenIPC"
	@echo "schema     schéma d'événement et cycle de vie"
	@echo "calibrate  validation de l'étalonnage par trafic aérien"
	@echo "viewshed   portée par secteur sur un relief de démonstration"

install:
	pip install -e ".[dev,full,ptz]"

test:
	pytest -q

test-edge:
	OPENVIGIE_FORCE_NUMPY=1 pytest -q

test-all: test test-edge

lint:
	ruff check src tests

plan:
	@for t in minimal medium full; do echo "== $$t"; python -m openvigie.cli plan -t $$t; echo; done

doctor:
	@for t in minimal medium full; do echo "== $$t"; python -m openvigie.cli doctor -t $$t; echo; done

selftest:
	python -m openvigie.cli selftest -t medium --mode plume
	python -m openvigie.cli selftest -t medium --mode cloud

hw:
	python -m openvigie.cli hw --matrix

schema:
	python -m openvigie.cli schema

capabilities:
	python -m openvigie.cli capabilities -t full

calibrate:
	python -m openvigie.cli calibrate -t full --simulate
	python -m openvigie.cli calibrate -t full --simulate --clock-error 1.5

viewshed:
	python -m openvigie.cli viewshed -t full --synthetic --sectors 16

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache build dist *.egg-info
