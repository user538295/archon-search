# Configuration Reference

All configuration lives in `config.toml`. Every key can be overridden with an environment variable using the pattern `APP_{SECTION}_{KEY}` (uppercased).

## [server]
| Key | Default | Description |
|-----|---------|-------------|
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `8080` | Listen port |
| `workers` | `4` | Number of worker processes |
| `timeout` | `30` | Request timeout in seconds |

## [database]
| Key | Default | Description |
|-----|---------|-------------|
| `url` | — | Connection string (required) |
| `pool_size` | `10` | Maximum pool connections |
| `timeout` | `5` | Acquire timeout in seconds |

## [logging]
| Key | Default | Description |
|-----|---------|-------------|
| `level` | `"INFO"` | Log level: DEBUG, INFO, WARNING, ERROR |
| `format` | `"json"` | Output format: `json` or `text` |
| `file` | `null` | Log file path (null = stdout) |

## [cache]
| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable in-memory caching |
| `ttl_seconds` | `300` | Cache entry TTL |
| `max_entries` | `1024` | Maximum cache size |
