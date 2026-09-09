PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
BASE_URL ?= http://127.0.0.1:8080

.PHONY: install demo-local stop-local clean-local test lint test-demo test-web terraform-check plan
install:
	python3.12 -m venv .venv
	$(PYTHON) -m pip install -e 'limiter[test]'

demo-local:
	bash scripts/local-up.sh

stop-local:
	bash scripts/local-down.sh

clean-local:
	bash scripts/local-down.sh --volumes

test:
	PYTHON=$(PYTHON) bash scripts/test.sh

lint:
	$(RUFF) check .
	$(RUFF) format --check .

test-demo:
	mkdir -p artifacts
	$(PYTHON) -m ratelimit_demo.cli --base-url $(BASE_URL) --all --duration 6 --mode distributed --output artifacts/demo

test-web:
	DEMO_BASE_URL=$(BASE_URL) $(PYTHON) -m pytest -c pytest.ini -m web limiter/tests/test_web_smoke.py

terraform-check:
	terraform -chdir=infra/terraform fmt -check -recursive
	terraform -chdir=infra/terraform init -backend=false
	terraform -chdir=infra/terraform validate

plan:
	bash scripts/plan.sh
