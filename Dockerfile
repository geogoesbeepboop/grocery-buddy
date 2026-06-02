FROM python:3.12-slim

# Install system deps for Playwright Chromium
RUN apt-get update && apt-get install -y \
    curl \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency files first (layer cache)
COPY pyproject.toml .
COPY src/ src/

# Install Python deps
RUN uv sync --no-dev

# Install Playwright browsers
RUN uv run playwright install chromium --with-deps

# Copy rest of project
COPY . .

# The .amazon-session volume is mounted at runtime
VOLUME ["/app/.amazon-session"]

# Default: run the Temporal worker
CMD ["uv", "run", "grocery-buddy", "worker"]
