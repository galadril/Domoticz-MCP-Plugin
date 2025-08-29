import asyncio
import json
import time
import urllib.parse
from typing import Dict, Any, List
import os

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

        # ---- Redirect bridge configuration --------------------------------
        # Domoticz enforces HTTPS redirect_uri. Local client (loopback) uses http://127.0.0.1:<port>/callback.
        # Strategy: intercept loopback HTTP redirect, store original, swap to HTTPS bridge, later forward.
        self.redirect_bridge_enabled = True
        self.debug_bridge_page = False  # set True for intermediate HTML page
        self.log_full_code = True       # logs full authorization codes (security trade-off)
        self.force_https_bridge = os.environ.get('FORCE_HTTPS_BRIDGE', '1') not in ('0', 'false', 'False')
        self.external_bridge_base = os.environ.get('REDIRECT_BRIDGE_EXTERNAL_BASE')  # e.g. https://your.host.name
        if self.redirect_bridge_enabled and not self.external_bridge_base:
            try:
                # Derive host from Domoticz base URL, else HOSTNAME env, else localhost
                domo_host = None
                if self.domoticz_oauth_client and getattr(self.domoticz_oauth_client, 'domoticz_base_url', None):
                    p = urllib.parse.urlparse(self.domoticz_oauth_client.domoticz_base_url)
                    domo_host = p.hostname
                if not domo_host:
                    domo_host = os.environ.get('HOSTNAME') or 'localhost'
                if self.force_https_bridge:
                    # Assume reverse proxy terminating TLS on :443
                    self.external_bridge_base = f"https://{domo_host}"
                    Domoticz.Log("Redirect bridge derived HTTPS base (expect reverse proxy): " + self.external_bridge_base)
                else:
                    # Use direct plugin port over HTTP
                    port_part = f":{self.port}" if self.port not in (80,) else ''
                    self.external_bridge_base = f"http://{domo_host}{port_part}"
                    Domoticz.Log("Redirect bridge derived HTTP base: " + self.external_bridge_base)
            except Exception as e:  # pragma: no cover
                Domoticz.Error(f"Failed to derive redirect bridge base: {e}")
        if self.force_https_bridge:
            Domoticz.Log("NOTE: Ensure external HTTPS forwards /redirect_bridge -> plugin (http://<host>:%d/redirect_bridge)" % self.port)

        # Mapping state -> original loopback redirect + timestamp
        self.redirect_bridge_map: Dict[str, Dict[str, Any]] = {}
        self.redirect_bridge_ttl = 600  # seconds
        self.recent_auth_codes: List[Dict[str, Any]] = []
        self.recent_codes_limit = 20

        if AIOHTTP_AVAILABLE:
            self.app = web.Application()
            self.setup_routes()
            self.setup_cors()
        Domoticz.Debug(f"MCP Server init host={self.host} port={self.port}")

    # ---- setup ------------------------------------------------------------
    def setup_cors(self):
        if not AIOHTTP_AVAILABLE:
            return
        try:
            cors = aiohttp_cors.setup(self.app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*")})
            for route in list(self.app.router.routes()):
                cors.add(route)
            Domoticz.Debug("CORS configured for all routes")
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
            self.app.router.add_get('/redirect_bridge', self.redirect_bridge_handler)
            self.app.router.add_get('/last_auth_codes', self.last_auth_codes_handler)
            Domoticz.Debug("Routes registered (/mcp,/health,/info,/authorize,/token,/redirect_bridge,/last_auth_codes)")
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
                    Domoticz.Debug("Lazy OAuth discovery via /info")
                    if self.domoticz_oauth_client.discover_oauth_endpoints():
                        info["authorization"] = self.domoticz_oauth_client.oauth_config
                except Exception as e:  # pragma: no cover
                    Domoticz.Log(f"Warning: OIDC fetch failed: {e}")
        return web.json_response(info)

    async def proxy_authorize(self, request: web_request.Request):
        try:
            Domoticz.Debug(f"/authorize query={dict(request.rel_url.query)}")
            if not self.domoticz_oauth_client:
                return web.json_response({"error": "OAuth client not configured"}, status=500)
            if not self.domoticz_oauth_client.oauth_config:
                Domoticz.Debug("Trigger discovery for /authorize")
                if not self.domoticz_oauth_client.discover_oauth_endpoints():
                    return web.json_response({"error": "OAuth discovery failed"}, status=500)
            auth_ep = self.domoticz_oauth_client.oauth_config.get('authorization_endpoint')
            if not auth_ep:
                return web.json_response({"error": "authorization_endpoint missing"}, status=500)
            qp = dict(request.rel_url.query)
            # Bridge logic: swap loopback http redirect for external https bridge
            try:
                orig_redirect = qp.get('redirect_uri')
                if (self.redirect_bridge_enabled and self.external_bridge_base and orig_redirect and
                        orig_redirect.startswith(('http://127.0.0.1', 'http://localhost')) and
                        not orig_redirect.startswith('https://')):
                    state = qp.get('state') or f"st_{int(time.time()*1000)}"
                    qp['state'] = state
                    self._purge_redirect_bridge()
                    self.redirect_bridge_map[state] = {"redirect": orig_redirect, "ts": time.time()}
                    qp['redirect_uri'] = f"{self.external_bridge_base.rstrip('/')}/redirect_bridge"
                    Domoticz.Log(f"Redirect bridge engaged state={state} orig={orig_redirect} via={qp['redirect_uri']}")
            except Exception as e:  # pragma: no cover
                Domoticz.Error(f"Redirect bridge setup failed: {e}")
            if 'client_secret' in qp:
                Domoticz.Log("Stripping client_secret from /authorize request")
                qp.pop('client_secret')
            target = auth_ep + ('?' + urllib.parse.urlencode(qp) if qp else '')
            Domoticz.Log(f"Proxy /authorize -> {target}")
            raise web.HTTPFound(location=target)
        except web.HTTPException:
            raise
        except Exception as e:
            Domoticz.Error(f"/authorize proxy error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def redirect_bridge_handler(self, request: web_request.Request):
        try:
            params = dict(request.rel_url.query)
            code = params.get('code')
            error = params.get('error')
            state = params.get('state')
            Domoticz.Debug(f"/redirect_bridge hit state={state} code_present={bool(code)} error={error}")
            if not state or state not in self.redirect_bridge_map:
                return web.Response(text="Redirect bridge state unknown or expired", status=400)
            entry = self.redirect_bridge_map.pop(state)
            orig = entry.get('redirect')
            record = {"ts": time.time(), "state": state, "code": code, "full_code_logged": True, "error": error, "forward_target": orig}
            self.recent_auth_codes.append(record)
            if len(self.recent_auth_codes) > self.recent_codes_limit:
                self.recent_auth_codes = self.recent_auth_codes[-self.recent_codes_limit:]
            if code:
                Domoticz.Log(f"OAuth authorization code captured state={state} code={code}")
            if error:
                Domoticz.Error(f"OAuth authorization error state={state} error={error}")
            if not orig or not orig.startswith(('http://127.0.0.1', 'http://localhost')):
                return web.Response(text="Original redirect invalid", status=400)
            sep = '&' if ('?' in orig) else '?'
            forward = orig + sep + (f"code={urllib.parse.quote(code)}" if code else f"error={urllib.parse.quote(error or 'unknown_error')}")
            if state:
                forward += f"&state={urllib.parse.quote(state)}"
            Domoticz.Debug(f"Redirect bridge forwarding -> {forward}")
            if self.debug_bridge_page:
                body = (f"<html><body><h3>Authorization Complete</h3>"
                        f"<p>State: {state}</p><p>Code: {code or error}</p>"
                        f"<p>Forward target: {forward}</p>"
                        f"<p>Close this tab if your client captured the code.</p>"
                        f"<script>setTimeout(function(){{window.location='{forward}';}},1500);</script>"
                        f"</body></html>")
                return web.Response(text=body, content_type='text/html')
            raise web.HTTPFound(location=forward)
        except web.HTTPException:
            raise
        except Exception as e:
            Domoticz.Error(f"/redirect_bridge error: {e}")
            return web.Response(text=f"Redirect bridge failure: {e}", status=500)

    async def last_auth_codes_handler(self, request: web_request.Request):
        return web.json_response({"recent": self.recent_auth_codes})

    def _purge_redirect_bridge(self):
        cutoff = time.time() - self.redirect_bridge_ttl
        to_del = [k for k,v in self.redirect_bridge_map.items() if v.get('ts',0) < cutoff]
        for k in to_del:
            self.redirect_bridge_map.pop(k, None)
        if to_del:
            Domoticz.Debug(f"Redirect bridge purged {len(to_del)} stale entries")

    async def proxy_token(self, request: web_request.Request):
        try:
            if not self.domoticz_oauth_client:
                return web.json_response({"error": "OAuth client not configured"}, status=500)
            if not self.domoticz_oauth_client.oauth_config:
                Domoticz.Debug("Trigger discovery for /token")
                if not self.domoticz_oauth_client.discover_oauth_endpoints():
                    return web.json_response({"error": "OAuth discovery failed"}, status=500)
            token_ep = self.domoticz_oauth_client.oauth_config.get('token_endpoint')
            if not token_ep:
                return web.json_response({"error": "token_endpoint missing"}, status=500)
            form = await request.post()
            form_data = dict(form)
            safe_log = {k: ('***' if any(s in k.lower() for s in ['secret','token','code','assertion','password']) else v) for k,v in form_data.items()}
            Domoticz.Debug(f"Proxy /token -> {token_ep} data={safe_log}")
            loop = asyncio.get_event_loop()
            def do_req():
                return requests.post(token_ep, data=form_data, headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
            resp = await loop.run_in_executor(None, do_req)
            try:
                data = resp.json()
            except Exception:
                data = {'raw': resp.text[:200]}
            safe_resp = {k: ('***' if any(s in k.lower() for s in ['secret','token','id_token','refresh','access']) else v) for k,v in data.items()} if isinstance(data, dict) else data
            Domoticz.Debug(f"/token response status={resp.status_code} body={safe_resp}")
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
            Domoticz.Debug(f"MCP request id={request_id} method={method}")
            if method == 'initialize':
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "domoticz-mcp-server", "version": "2.0.0"}}}
            elif method == 'tools/list':
                tools = await self.get_available_tools()
                Domoticz.Debug(f"tools/list -> {len(tools)} tools")
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
            elif method == 'tools/call':
                tool_name = params.get('name')
                arguments = params.get('arguments', {})
                Domoticz.Debug(f"tools/call name={tool_name} args={arguments}")
                auth_header = request.headers.get('Authorization')
                if not auth_header or not auth_header.startswith('Bearer '):
                    Domoticz.Error("Missing or invalid Authorization header for tools/call")
                    return web.Response(status=401, text="Missing or invalid access token", headers={'WWW-Authenticate': 'Bearer realm="Domoticz MCP"'})

                access_token = auth_header[7:]
                start = time.time()
                result = await self.execute_domoticz_tool(tool_name, arguments, access_token)
                Domoticz.Debug(f"tools/call done name={tool_name} elapsed={time.time()-start:.3f}s")
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}}}
            elif method == 'logging/setLevel':
                level = params.get('level', 'info')
                Domoticz.Log(f"Log level set to: {level}")
                resp = {"jsonrpc": "2.0", "id": request_id, "result": {}}
            else:
                Domoticz.Error(f"Unknown MCP method: {method}")
                resp = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return web.json_response(resp)
        except Exception as e:
            Domoticz.Error(f"Error handling MCP request: {e}")
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Internal error: {e}"}}, status=500)

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
                Domoticz.Error("OAuth client not configured for tool execution")
                return {"error": "Domoticz OAuth client not configured"}
            if name == "domoticz_get_version":
                Domoticz.Debug("Execute tool domoticz_get_version")
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getversion"})
            if name == "domoticz_list_devices":
                Domoticz.Debug("Execute tool domoticz_list_devices")
                params = {"type": "command", "param": "getdevices", "filter": arguments.get("filter", "all")}
                if arguments.get("used", True):
                    params["used"] = "true"
                return self.domoticz_oauth_client.make_authenticated_request(access_token, params)
            if name == "domoticz_device_status":
                idx = arguments.get("idx")
                Domoticz.Debug(f"Execute tool domoticz_device_status idx={idx}")
                if not idx:
                    return {"error": "idx parameter is required"}
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getdevices", "rid": str(idx)})
            if name == "domoticz_list_scenes":
                Domoticz.Debug("Execute tool domoticz_list_scenes")
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getscenes"})
            if name == "domoticz_get_log":
                Domoticz.Debug("Execute tool domoticz_get_log")
                return self.domoticz_oauth_client.make_authenticated_request(access_token, {"type": "command", "param": "getlog", "log": arguments.get("log_type", "status")})
            Domoticz.Error(f"Unknown tool requested: {name}")
            return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            Domoticz.Error(f"Tool execution failed: {e}")
            return {"error": f"Tool execution failed: {e}"}

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
        if self.force_https_bridge:
            Domoticz.Log("Redirect bridge expects external HTTPS at: " + self.external_bridge_base.rstrip('/') + "/redirect_bridge")
        return runner
