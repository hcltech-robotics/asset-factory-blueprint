.PHONY: install diagrams site docs docs-serve

install:
	python -m pip install -e .[dev]

diagrams:
	PYTHONPATH=src python scripts/generate_diagrams.py --check

site:
	python -m mkdocs build --strict

docs: site

docs-serve:
	python -m mkdocs serve
