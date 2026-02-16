# Universal MCP Proxy

A simple Python HTTP proxy server that can bridge any MCP (Model Context Protocol) server over HTTP.

## Features

- **Universal**: configure the MCP command and working directory via environment variables or a JSON configuration file.
- **Configurable Port**: Run on any port.
- **Pass-through**: Forwards `stdin` and `stdout` JSON-RPC messages.
- **Notification Handling**: Correctly handles JSON-RPC notifications (no response expected).
- **Environment Inheritance**: Passes parent environment variables to the MCP process.

## Requirements

- Python 3.8+
- `fastapi`
- `uvicorn`

## Setup

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

## Usage

You can configure the server using environment variables OR a JSON configuration file (standard Anthropic format).

### Option 1: Using a Config File (Recommended)

Create a JSON file (e.g., `mcp_config.json`) with the standard MCP server configuration:

```json
{
  "mcpServers": {
    "my-security-server": {
      "command": "node",
      "args": ["dist/index.js"],
      "env": {
        "SIGMA_PATHS": "/path/to/sigma/rules"
      }
    }
  }
}
```

Run the proxy specifying the config file and the server name:

```bash
export MCP_CONFIG_FILE="mcp_config.json"
export MCP_SERVER_NAME="my-security-server"
export MCP_CWD="/path/to/mcp/project" # Optional
export MCP_PORT=8000 # Optional

uvicorn server:app --host 0.0.0.0 --port $MCP_PORT
```

### Option 2: Using Environment Variables

Set the `MCP_COMMAND` environment variable directly.

```bash
export MCP_COMMAND="node dist/index.js"
export MCP_CWD="/path/to/mcp/project" # Optional
export MCP_PORT=8000 # Optional

uvicorn server:app --host 0.0.0.0 --port $MCP_PORT
```

## API

- `POST /mcp`: Send JSON-RPC body. Returns JSON-RPC response.
- `GET /health`: Check if MCP process is running.

## Docker

You can containerize this proxy:

```dockerfile
FROM python:3.9-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```
