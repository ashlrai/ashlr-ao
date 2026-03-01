# ── Ashlar AO — Agent Orchestrator ──
# Python 3.12 + tmux + Claude Code CLI

FROM python:3.12-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# App directory
WORKDIR /app

# Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App files
COPY ashlar_server.py .
COPY ashlar_dashboard.html .
COPY "White Ashlar logo copy.png" .

# Data directory
RUN mkdir -p /root/.ashlar

# Expose port
EXPOSE 5111

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5111/api/health || exit 1

# Run server
ENV ASHLAR_HOST=0.0.0.0
CMD ["python", "ashlar_server.py"]
