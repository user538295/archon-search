# Getting Started

## Prerequisites
- Python 3.12 or later
- `uv` package manager (recommended) or `pip`
- Docker Desktop (optional, for local infrastructure)

## Installation

```bash
uv sync
```

This installs all dependencies from the lockfile for reproducibility.

## Configuration

Copy the example configuration file:

```bash
cp config.example.toml config.toml
```

Edit `config.toml` to set your database URL, API keys, and log level. Environment variables prefixed with `APP_` override any TOML key.

## Running the Server

```bash
uv run python main.py
```

By default the server listens on port 8080. Set `APP_SERVER_PORT` to change it.

## Running Tests

```bash
uv run pytest
```

Tests require no external services—all dependencies are mocked or use in-memory implementations.
