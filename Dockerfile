FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Install required system packages
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies (no-deps flag is important to avoid version conflicts)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt --no-deps

# Copy application code
COPY . .

# Create non-root user for security - disabled due to FinamPy writes config.pkl to the library directory (ugly bug)
# RUN useradd --create-home --shell /bin/bash app && \
#     chown -R app:app /app
# USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Run the application with gunicorn for production
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "run:app"]
