# Deployment Guide

## Production Checklist

Before deploying to production, verify:
- [ ] All environment variables are set (see Configuration Reference)
- [ ] Database connection pool size matches expected concurrency
- [ ] Log level set to `INFO` or `WARNING` (never `DEBUG` in production)
- [ ] TLS termination is handled by the reverse proxy
- [ ] Health check endpoint (`/health`) is reachable from the load balancer

## Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --no-dev
COPY . .
CMD ["uv", "run", "python", "main.py"]
```

## systemd

Install as a system service:

```ini
[Unit]
Description=App Service
After=network.target

[Service]
User=app
WorkingDirectory=/opt/app
ExecStart=/opt/app/.venv/bin/python main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Health Checks

`GET /health` returns `200 OK` when the service is ready, `503` when not.
