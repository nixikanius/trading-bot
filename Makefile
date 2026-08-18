.PHONY: deps run run-gunicorn test clean docker-build docker-run help

DOCKER_IMAGE=nixikanius/trading-bot
DOCKER_PLATFORMS=linux/amd64,linux/arm64
VERSION=latest
PYTHON = pyenv exec python
VENV ?= .venv

# Default target
deps:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements-dev.txt

run:
	$(VENV)/bin/python run.py

run-gunicorn:
	$(VENV)/bin/gunicorn --reload run:app

test:
	$(VENV)/bin/python -m pytest tests/ -v

clean:
	rm -rf .venv
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf htmlcov

# Docker
docker-build:
	@echo "Building docker image $(DOCKER_IMAGE) for version $(VERSION) (platforms $(DOCKER_PLATFORMS))..."
	docker buildx build --platform $(DOCKER_PLATFORMS) -t $(DOCKER_IMAGE):$(VERSION) .

docker-build-local:
	@echo "Building local docker image $(DOCKER_IMAGE) for version $(VERSION)..."
	docker build -t $(DOCKER_IMAGE):$(VERSION) .

docker-push:	
	@echo "Pushing docker image $(DOCKER_IMAGE) for version $(VERSION) (platforms $(DOCKER_PLATFORMS))..."
	docker buildx build --platform $(DOCKER_PLATFORMS) -t $(DOCKER_IMAGE):$(VERSION) --push .

docker-run:
	docker run --rm -it \
		--name trading-bot \
		-p 8000:8000 \
		-v $(PWD)/config.yml:/app/config.yml:ro \
		$(DOCKER_IMAGE):$(VERSION)

help:
	@echo "Available targets:"
	@echo "  deps               - Install dependencies"
	@echo "  run                - Run the app"
	@echo "  run-gunicorn       - Run with Gunicorn in development mode"
	@echo "  test               - Run tests"
	@echo "  clean              - Clean up temporary files and dependencies"
	@echo "  docker-build       - Build Docker image"
	@echo "  docker-build-local - Build local Docker image"
	@echo "  docker-run         - Run Docker container"
	@echo ""
	@echo "Local Python version is defined in .python-version."
