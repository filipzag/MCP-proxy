import subprocess
import json
import threading
import os
import sys
import shlex
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from pydantic import BaseModel
import uvicorn

# Configuration
# 1. MCP_CONFIG_FILE: Path to the JSON configuration file (e.g., claude_desktop_config.json)
# 2. MCP_SERVER_NAME: Name of the server to run from the config file
# 3. Fallback: MCP_COMMAND, MCP_CWD, MCP_ARGS from env vars if config file is not used

MCP_CONFIG_FILE = os.environ.get("MCP_CONFIG_FILE")
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")

security = HTTPBearer()

async def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if MCP_AUTH_TOKEN and credentials.credentials != MCP_AUTH_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid authentication token")
    return credentials


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
        self.lock = asyncio.Lock()
        self.response_futures: dict[str, asyncio.Future] = {}
        self.sse_queues: list[asyncio.Queue] = []
        self.reader_task = None

    async def start(self):
        try:
            self.process = subprocess.Popen(
                MCP_COMMAND,
                cwd=MCP_CWD,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, # Keep stderr separate
                env=MCP_ENV,
                text=True,
                bufsize=1  # Line buffered
            )
            print("MCP Server started.")
            
            # Start background reader
            self.reader_task = asyncio.create_task(self._read_loop())
            
        except Exception as e:
            print(f"Failed to start MCP server: {e}")
            raise

    async def stop(self):
        if self.process:
            self.process.terminate()
            # self.process.wait() # avoid blocking
            print("MCP Server stopped.")
        if self.reader_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self):
        """Reads stdout from the MCP process and dispatches messages."""
        loop = asyncio.get_event_loop()
        while self.process and self.process.poll() is None:
            try:
                # Run blocking readline in executor to avoid blocking the event loop
                line = await loop.run_in_executor(None, self.process.stdout.readline)
                if not line:
                    break
                
                await self._dispatch_response(line)
                
            except Exception as e:
                print(f"Error reading from MCP: {e}")
                break
        
        print("MCP Process exited or stream ended.")
        # Cleanup
        for future in self.response_futures.values():
            if not future.done():
                future.set_exception(Exception("MCP process exited"))

    async def _dispatch_response(self, line: str):
        """Parses the response line and routes it to futures and SSE queues."""
        try:
            # 1. Send to all SSE clients
            for queue in self.sse_queues:
                await queue.put(f"data: {line.strip()}\n\n")

            # 2. Check for matching request ID via Future
            response_json = json.loads(line)
            if "id" in response_json:
                req_id = response_json["id"]
                # JSON-RPC IDs can be int or str. requests map uses what was sent.
                # We need to handle potential type mismatches if necessary, 
                # but usually we control the ID generation or pass it through.
                
                # Check string keys first (common dict key type) or raw
                if req_id in self.response_futures:
                    future = self.response_futures.pop(req_id)
                    if not future.done():
                        future.set_result(response_json)
                        
        except json.JSONDecodeError:
            print(f"Failed to decode JSON from server: {line}")
        except Exception as e:
            print(f"Error dispatching response: {e}")

    async def send_request(self, request_data: dict) -> dict:
        if not self.process:
            raise HTTPException(status_code=500, detail="MCP backend not running")

        request_id = request_data.get("id")
        should_wait = request_id is not None

        if should_wait:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self.response_futures[request_id] = future

        async with self.lock:
            try:
                json_str = json.dumps(request_data) + "\n"
                self.process.stdin.write(json_str)
                self.process.stdin.flush()
            except Exception as e:
                if should_wait and request_id in self.response_futures:
                     del self.response_futures[request_id]
                raise HTTPException(status_code=500, detail=str(e))

        if should_wait:
            try:
                # Wait for response
                return await asyncio.wait_for(future, timeout=30.0) # 30s timeout
            except asyncio.TimeoutError:
                if request_id in self.response_futures:
                    del self.response_futures[request_id]
                raise HTTPException(status_code=504, detail="MCP request timed out")
        
        return {"status": "notification_sent"}

    async def send_message(self, request_data: dict):
        """Sends a message without waiting for a direct response (used for /messages)."""
        if not self.process:
            raise HTTPException(status_code=500, detail="MCP backend not running")
            
        async with self.lock:
            try:
                json_str = json.dumps(request_data) + "\n"
                self.process.stdin.write(json_str)
                self.process.stdin.flush()
            except Exception as e:
                 raise HTTPException(status_code=500, detail=str(e))
        return {"status": "sent"}

mcp_backend = MCPProcess()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await mcp_backend.start()
    yield
    await mcp_backend.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/mcp")
async def handle_mcp_request(request: dict, token: HTTPAuthorizationCredentials = Depends(verify_token)):
    """
    Forwards JSON-RPC requests to the MCP server.
    Waits for a response if 'id' is present.
    """
    response = await mcp_backend.send_request(request)
    return response

@app.get("/sse")
async def handle_sse(request: Request, token: HTTPAuthorizationCredentials = Depends(verify_token)):
    """
    Establishes an SSE stream for MCP output.
    """
    queue = asyncio.Queue()
    mcp_backend.sse_queues.append(queue)
    
    async def event_generator():
        try:
            # Yield initial connection message if desired, or just wait for data
            yield "event: open\ndata: {\"status\": \"connected\"}\n\n"
            
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                    
                data = await queue.get()
                yield data
                queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            mcp_backend.sse_queues.remove(queue)
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/messages")
async def handle_messages(request: dict, token: HTTPAuthorizationCredentials = Depends(verify_token)):
    """
    Sends a JSON-RPC message to the MCP server efficiently (no wait for response).
    Responses will appear in the SSE stream.
    """
    return await mcp_backend.send_message(request)


@app.get("/health")
def health_check():
    if mcp_backend.process and mcp_backend.process.poll() is None:
        return {"status": "healthy", "pid": mcp_backend.process.pid}
    return {"status": "unhealthy", "detail": "MCP process not running"}

if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
