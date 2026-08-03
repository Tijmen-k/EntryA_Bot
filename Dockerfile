FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=UTC

WORKDIR /app

RUN groupadd -r -g 1000 entrya && useradd -r -u 1000 -g entrya -d /app -s /sbin/nologin entrya

# Install Python deps first (layer cache) — engine-only set, no
# dashboard/Discord-bot packages (streamlit, plotly, pandas, discord.py,
# reportlab, matplotlib) since this image only ever runs main.py.
COPY requirements-engine.txt .
RUN pip install --no-cache-dir -r requirements-engine.txt

# Copy source
COPY . .

# Persistent volumes for logs and state — chown host-mounted dirs to uid/gid
# 1000 to match this user (e.g. `chown -R 1000:1000 logs state` on the host).
RUN chown -R entrya:entrya /app
VOLUME ["/app/logs", "/app/state"]

USER entrya

# Healthcheck: verify the process is alive
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python main.py" > /dev/null || exit 1

CMD ["python", "main.py"]
