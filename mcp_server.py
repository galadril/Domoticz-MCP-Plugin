import asyncio
import json
import time
import urllib.parse
from typing import Dict, Any, List

import requests
import Domoticz

try:
    from aiohttp import web, web_request, web_response
    import aiohttp_cors
    AIOHTTP_AVAILABLE = True
except ImportError as e:  # pragma: no cover - runtime environment dependent
    AIOHTTP_AVAILABLE = False
    class web:  # type: ignore
        class Application: ...
        @staticmethod
        def json_response(*args, **kwargs): ...
    class web_request:  # type: ignore
        class Request: ...
    class web_response:  # type: ignore
        class Response: ...
    Domoticz.Error(f"aiohttp not available: {e}")

try:
    import mcp  # noqa: F401  # Only to check availability
    MCP_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    MCP_SDK_AVAILABLE = False

from oauth_client import DomoticzOAuthClient

class DomoticzMCPServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8765, domoticz_oauth_client: DomoticzOAuthClient = None):
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        self.domoticz_oauth_client = domoticz_oauth_client
        if AIOHTTP_AVAILABLE:
            self.app = web.Application()
            self.setup_routes()
            self.setup_cors()

    # ---- setup ------------------------------------------------------------
    def setup_cors(self):
        if not AIOHTTP_AVAILABLE:
            return
        try:
            cors = aiohttp_cors.setup(self.app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*")})
            for route in list(self.app.router.routes()):
                cors.add(route)
        except Exception as e:
            Domoticz.Error(f"Error setting up CORS: {e}")

    def setup_routes(self):
        if not AIOHTTP_AVAILABLE:
            return
        try:
            self.app.router.add_post('/mcp', self.handle_mcp_request)
            self.app.router.add_get('/health', self.health_check)
            self.app.router.add_get('/info', self.server_info)
            self.app.router.add_get('/authorize', self.proxy_authorize)
            self.app.router.add_post('/token', self.proxy_token)
        except Exception as e:
            Domoticz.Error(f"Error setting up routes: {e}")

    # ---- routes -----------------------------------------------------------
    async def health_check(self, request: web_request.Request):
        return web.json_response({"status": "healthy", "service": "domoticz-mcp"})

    async def server_info(self, request: web_request.Request):
        info = {"service": "Domoticz MCP Server", "version": "2.0.0", "protocol": "MCP 2025-06-18", "mcp_sdk_available": MCP_SDK_AVAILABLE, "aiohttp_available": AIOHTTP_AVAILABLE, "capabilities": {"tools": True, "logging": True}, "authentication_model": "oauth_2_1_passthrough", "description": "MCP 2025-06-18 compliant server for Domoticz with OAuth passthrough authentication"}
        if self.domoticz_oauth_client:
            if self.domoticz_oauth_client.oauth_config:
                info["authorization"] = self.domoticz_oauth_client.oauth_config
            else:
                try:
                    if self.domoticz_oauth_client.discover_oauth_endpoints():
                        info["authorization"] = self.domoticz_oauth_client.oauth_config
                except Exception as e:  # pragma: no cover
                    Domoticz.Log(f"Warning: OIDC fetch failed: {e}")
        return web.json_response(info)

    async def proxy_authorize(self, request: web_request.Request):
        try:
            if not self.domoticz_oauth_client:
                return web.json_response({"error": "OAuth client not configured"}, status=500)
            if not self.domoticz_oauth_client.oauth_config:
                if not self.domoticz_oauth_client.discover_oauth_endpoints():
                    return web.json_response({"error": "OAuth discovery failed"}, status=500)
            auth_ep = self.domoticz_oauth_client.oauth_config.get('authorization_endpoint')
            if not auth_ep:
                return web.json_response({"error": "authorization_endpoint missing"}, status=500)
            qp = dict(request.rel_url.query)
            if 'client_secret' in qp:
                Domoticz.Log("Stripping client_secret from /authorize request")
                qp.pop('client_secret')
            target = auth_ep + ('?' + urllib.parse.urlencode(qp) if qp else '')
            Domoticz.Debug(f"Proxy /authorize -> {target}")
            raise web.HTTPFound(location=target)
        except web.HTTPException:
            raise
        except Exception as e:
            Domoticz.Error(f"/authorize proxy error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def proxy_token(self, request: web_request.Request):
        try:
            if not self.domoticz_oauth_client:
                return web.json_response({"error": "OAuth client not configured"}, status=500)
            if not self.domoticz_oauth_client.oauth_config:
                if not self.domoticz_oauth_client.discover_oauth_endpoints():
                    return web.json_response({"error": "OAuth discovery failed"}, status=500)
            token_ep = self.domoticz_oauth_client.oauth_config.get('token_endpoint')
            if not token_ep:
                return web.json_response({"error": "token_endpoint missing"}, status=500)
            form = await request.post()
            form_data = dict(form)
            loop = asyncio.get_event_loop()
            def do_req():
                return requests.post(token_ep, data=form_data, headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
            resp = await loop.run_in_executor(None, do_req)
            try:
                data = resp.json()
            except Exception:
                data = {'raw': resp.text}
            return web.json_response(data, status=resp.status_code)
        except Exception as e:
            Domoticz.Error(f"/token proxy error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_mcp_request(self, request: web_request.Request):
        try:
            data = await request.json()
            method = data.get('method')
            params = data.get('params', {})
            request_id = data.get('id')
            Domoticz.Debug(f"MCP request: {method}")
            if method == 'initialize':
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "domoticz-mcp-server", "version": "2.0.0"}}}
            elif method == 'tools/list':
                tools = await self.get_available_tools()
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
            elif method == 'tools/call':
                tool_name = params.get('name')
                arguments = params.get('arguments', {})
                auth_header = request.headers.get('Authorization')
                if not auth_header or not auth_header.startswith('Bearer '):
                    return web.Response(status=401, text="Missing or invalid access token", headers={'WWW-Authenticate': 'Bearer realm="Domoticz MCP"'})
                access_token = auth_header[7:]
                result = await self.execute_domoticz_tool(tool_name, arguments, access_token)
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}}
            elif method == 'logging/setLevel':
                level = params.get('level', 'info')
                Domoticz.Log(f"Log level set to: {level}")
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {}}
            else:
                resp = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return web.json_response(resp)
        except Exception as e:
            Domoticz.Error(f"Error handling MCP request: {e}")
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Internal error: {e}"}}, status=500)

    # ---- tool handling ----------------------------------------------------
    async def get_available_tools(self) -> List[Dict[str, Any]]:
        return [
            {"name": "domoticz_get_version", "description": "Get Domoticz version information", "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
            {"name": "domoticz_list_devices", "description": "List all Domoticz devices with optional filtering", "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "enum": ["all", "light", "weather", "temperature", "utility"], "default": "all"}, "used": {"type": "boolean", "default": True}}, "required": [], "additionalProperties": False}},
            {"name": "domoticz_device_status", "description": "Get detailed status of a specific device", "inputSchema": {"type": "object", "properties": {"idx": {"type": "integer", "minimum": 1}}, "required": ["idx"], "additionalProperties": False}},
            {"name": "domoticz_list_scenes", "description": "List all scenes and groups", "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
            {"name": "domoticz_get_log", "description": "Retrieve Domoticz logs", "inputSchema": {"type": "object", "properties": {"log_type": {"type": "string", "enum": ["status", "error", "notification"], "default": "status"}}, "required": [], "additionalProperties": False}}
        ]

    async def execute_domoticz_tool(self, name: str, arguments: Dict[str, Any], access_token: str) -> Dict[str, Any]:
        try:
            if not self.domoticz_oauth_client:
                return {"error": "Domoticz OAuth client not configured"}
            if name == "domoticz_get_version":
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getversion"})
            if name == "domoticz_list_devices":
                params = {"type": "command", "param": "getdevices", "filter": arguments.get("filter", "all")}
                if arguments.get("used", True):
                    params["used"] = "true"
                return self.domoticz_oauth_client.make_authenticated_request(access_token, params)
            if name == "domoticz_device_status":
                idx = arguments.get("idx")
                if not idx:
                    return {"error": "idx parameter is required"}
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getdevices", "rid": str(idx)})
            if name == "domoticz_list_scenes":
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getscenes"})
            if name == "domoticz_get_log":
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getlog", "log": arguments.get("log_type", "status")})
            return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            Domoticz.Error(f"Tool execution failed: {e}")
            return {"error": f"Tool execution failed: {e}"}

    # ---- lifecycle --------------------------------------------------------
    async def start_server(self):
        if not AIOHTTP_AVAILABLE:
            Domoticz.Error("aiohttp not available - cannot start HTTP server")
            return None
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        Domoticz.Log(f"Domoticz MCP Server v2.0.0 started on http://{self.host}:{self.port}")
        Domoticz.Log(f"Health check: http://{self.host}:{self.port}/health")
        Domoticz.Log(f"Server info: http://{self.host}:{self.port}/info")
        Domoticz.Log(f"MCP endpoint: http://{self.host}:{self.port}/mcp")
        Domoticz.Log(f"Protocol: MCP 2025-06-18 compliant")
        Domoticz.Log(f"Authentication: OAuth 2.1 passthrough to Domoticz")
        return runner
