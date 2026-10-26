PYTHON ?= python

.PHONY: test run

test:
	$(PYTHON) -m unittest discover -s tests -t .

run:
	$(PYTHON) -m pocketagent
