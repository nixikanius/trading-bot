.PHONY: help install run run-gunicorn test clean docker-build docker-run

DOCKER_IMAGE=nixikanius/trading-bot
DOCKER_PLATFORMS=linux/amd64,linux/arm64
VERSION=latest

# Default target
help:
	@echo "Available targets:"
	@echo "  install      - Install dependencies"
	@echo "  run          - Run the app"
	@echo "  run-gunicorn - Run with Gunicorn in development mode"
	@echo "  test         - Run tests"
	@echo "  clean        - Clean up temporary files and dependencies"
	@echo "  docker-build - Build Docker image"
	@echo "  docker-run   - Run Docker container"

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

run:
	.venv/bin/python run.py

run-gunicorn:
	.venv/bin/gunicorn --reload run:app

test:
	.venv/bin/python -m pytest tests/ -v

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
	docker buildx build --platform $(DOCKER_PLATFORMS) -t $(DOCKER_IMAGE):$(VERSION) .

docker-push:
	docker buildx build --platform $(DOCKER_PLATFORMS) -t $(DOCKER_IMAGE):$(VERSION) --push .

docker-run:
	docker run --rm -it \
		--name trading-bot \
		-p 8000:8000 \
		-v $(PWD)/config.yml:/app/config.yml:ro \
		$(DOCKER_IMAGE):$(VERSION)
