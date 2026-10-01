FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # the non-root app user's home and caches (yfinance writes $XDG_CACHE_HOME/py-yfinance)
    HOME=/home/app \
    XDG_CACHE_HOME=/data/cache \
    # Chromium for the Playwright session method (NSE/BSE warm-up), shared by all users
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
# camelot: the PDF table fallback for annual reports (SPEC §3.6 step 3)
RUN uv sync --frozen --no-dev --extra camelot
# Headless Chromium only (the "headless shell", not full Chrome) plus its system libraries:
# the playwright session method of providers.yaml nse.session / bse.session.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# /data/raw: raw-file cache (RAW_DATA_DIR); /data/cache: XDG cache (yfinance). A named volume
# mounted on either inherits this owner. /home/app: the app user's home (HOME).
RUN useradd --system --uid 1000 --create-home --home-dir /home/app app \
    && mkdir -p /data/raw /data/cache \
    && chown -R app /app /data /home/app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
