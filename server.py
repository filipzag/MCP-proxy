import subprocess
import json
import threading
import os
import sys
import shlex
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# Configuration
# 1. MCP_CONFIG_FILE: Path to the JSON configuration file (e.g., claude_desktop_config.json)
# 2. MCP_SERVER_NAME: Name of the server to run from the config file
# 3. Fallback: MCP_COMMAND, MCP_CWD, MCP_ARGS from env vars if config file is not used

MCP_CONFIG_FILE = os.environ.get("MCP_CONFIG_FILE")
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME")

MCP_COMMAND = []
MCP_CWD = os.environ.get("MCP_CWD", os.getcwd())
MCP_ENV = os.environ.copy()

if MCP_CONFIG_FILE and MCP_SERVER_NAME:
    print(f"Loading configuration for '{MCP_SERVER_NAME}' from {MCP_CONFIG_FILE}")
    try:
        with open(MCP_CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        server_config = config.get("mcpServers", {}).get(MCP_SERVER_NAME)
        if not server_config:
            print(f"Error: Server '{MCP_SERVER_NAME}' not found in 'mcpServers' configuration.")
            sys.exit(1)
            
        command = server_config.get("command")
        args = server_config.get("args", [])
        env_vars = server_config.get("env", {})
        
        if not command:
            print(f"Error: No 'command' specified for server '{MCP_SERVER_NAME}'.")
            sys.exit(1)
            
        MCP_COMMAND = [command] + args
        MCP_ENV.update(env_vars)
        
    except FileNotFoundError:
        print(f"Error: Config file not found at {MCP_CONFIG_FILE}")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Failed to parse JSON config file at {MCP_CONFIG_FILE}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

elif os.environ.get("MCP_COMMAND"):
    # Fallback to direct environment variable configuration
    MCP_COMMAND_STR = os.environ.get("MCP_COMMAND")
    MCP_COMMAND = shlex.split(MCP_COMMAND_STR)
    # CWD and other env vars are already set/inherited

else:
    print("Error: Configuration missing.")
    print("Set MCP_CONFIG_FILE and MCP_SERVER_NAME, or MCP_COMMAND.")
    sys.exit(1)

MCP_PORT = int(os.environ.get("MCP_PORT", 8000))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")

print(f"Starting MCP Proxy for command: {MCP_COMMAND}")
print(f"Working Directory: {MCP_CWD}")

class MCPProcess:
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        try:
            self.process = subprocess.Popen(
                MCP_COMMAND,
                cwd=MCP_CWD,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=MCP_ENV,
                text=True,
                bufsize=1  # Line buffered
            )
            print("MCP Server started.")
        except Exception as e:
            print(f"Failed to start MCP server: {e}")
            raise

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait()
            print("MCP Server stopped.")

    def send_request(self, request_data: dict) -> dict:
        if not self.process:
            raise HTTPException(status_code=500, detail="MCP backend not running")

        # Check if this is a notification (no id)
        is_notification = "id" not in request_data

        with self.lock:
            try:
                # Prepare JSON-RPC message
                json_str = json.dumps(request_data) + "\n"
                
                # Write to stdin
                self.process.stdin.write(json_str)
                self.process.stdin.flush()
                
                if is_notification:
                    return {"status": "notification_sent"}

                # Read response from stdout
                while True:
                    response_line = self.process.stdout.readline()
                    
                    if not response_line:
                         # Check if process crashed
                        if self.process.poll() is not None:
                             stderr = self.process.stderr.read()
                             print(f"MCP Process crashed. Stderr: {stderr}")
                             raise HTTPException(status_code=500, detail=f"MCP process exited. Stderr: {stderr}")
                        return {"error": "No response from MCP server"}
                    
                    try:
                        response_json = json.loads(response_line)
                        
                        # Match response ID to request ID
                        if "id" in response_json and response_json["id"] == request_data["id"]:
                            return response_json
                        else:
                            # Log ignored messages (notifications, logs, mismatched IDs)
                            print(f"Ignored message from server: {response_line.strip()}")
                            
                    except json.JSONDecodeError:
                        print(f"Failed to decode JSON from server: {response_line}")

            except Exception as e:
                print(f"Error communicating with MCP: {e}")
                raise HTTPException(status_code=500, detail=str(e))

mcp_backend = MCPProcess()

@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp_backend.start()
    yield
    mcp_backend.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/mcp")
def handle_mcp_request(request: dict):
    """
    Forwards JSON-RPC requests to the MCP server.
    """
    response = mcp_backend.send_request(request)
    return response

@app.get("/health")
def health_check():
    if mcp_backend.process and mcp_backend.process.poll() is None:
        return {"status": "healthy", "pid": mcp_backend.process.pid}
    return {"status": "unhealthy", "detail": "MCP process not running"}

if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
