"""
<plugin key="Domoticz-MCP-Server" name="Domoticz MCP Server Plugin" author="Mark Heinis" version="2.0.0" wikilink="https://github.com/galadril/Domoticz-MCP-Service/wiki" externallink="https://github.com/galadril/Domoticz-MCP-Service">
    <description>
        Plugin for running Domoticz MCP (Model Context Protocol) Server.
        Provides AI assistant access to Domoticz functionality through MCP protocol.
        Authentication is handled via OAuth 2.1 flow - plugin acts as OAuth client to Domoticz.
    </description>
    <params>
        <param field="Mode1" label="Auto Start Server" width="75px">
            <options>
                <option label="Yes" value="true" default="true"/>
                <option label="No" value="false"/>
            </options>
        </param>
        <param field="Mode2" label="Health Check interval (seconds)" width="30px" required="true" default="30"/>
        <param field="Mode3" label="Domoticz URL Override" width="200px" required="false" default="" placeholder="Leave empty for localhost:8080"/>
        <param field="Mode6" label="Debug" width="200px">
            <options>
                <option label="None" value="0" default="true"/>
                <option label="Python Only" value="2"/>
                <option label="Basic Debugging" value="62"/>
                <option label="Basic+Messages" value="126"/>
                <option label="Connections Only" value="16"/>
                <option label="Connections+Queue" value="144"/>
                <option label="All" value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import Domoticz
import asyncio
import threading
import time
import json
import os
import traceback
import requests
import sys
import hashlib
import base64
import logging
import secrets
import urllib.parse
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

# Add the plugin directory to Python path to ensure imports work
plugin_path = os.path.dirname(os.path.realpath(__file__))
if plugin_path not in sys.path:
    sys.path.insert(0, plugin_path)

# Try to import aiohttp for the MCP server
try:
    from aiohttp import web, web_request, web_response
    from aiohttp.web_runner import GracefulExit
    import aiohttp_cors
    AIOHTTP_AVAILABLE = True
    Domoticz.Debug("aiohttp available - full MCP server functionality enabled")
except ImportError as e:
    AIOHTTP_AVAILABLE = False
    # Create dummy classes for type hints when aiohttp is not available
    class web:
        class Application: pass
        @staticmethod
        def json_response(*args, **kwargs): pass
        
    class web_request:
        class Request: pass
        
    class web_response:
        class Response: pass
        
    class GracefulExit(Exception): pass
    
    class aiohttp_cors:
        @staticmethod
        def setup(*args, **kwargs): return None
        
        class ResourceOptions:
            def __init__(self, **kwargs): pass
    
    Domoticz.Error(f"aiohttp not available: {e}")
    Domoticz.Error("MCP server will run in simple mode without HTTP endpoints")

# Check MCP SDK availability for future use
try:
    import mcp
    MCP_SDK_AVAILABLE = True
except ImportError:
    MCP_SDK_AVAILABLE = False

class DomoticzOAuthClient:
    """OAuth client for authenticating with Domoticz - follows OAuth 2.1 standard"""
    
    def __init__(self, domoticz_base_url: str = "http://127.0.0.1:8080"):
        self.domoticz_base_url = domoticz_base_url.rstrip('/')
        self.session = requests.Session()
        self.oauth_config = None
        
    def discover_oauth_endpoints(self):
        """Discover OAuth endpoints from Domoticz's .well-known configuration"""
        try:
            # Try to get OAuth configuration from Domoticz
            well_known_url = f"{self.domoticz_base_url}/.well-known/openid-configuration"
            response = self.session.get(well_known_url, timeout=10)
            
            if response.status_code == 200:
                self.oauth_config = response.json()
                
                # Fix hostname issues - replace domoticz.local with actual IP
                base_url_parts = urllib.parse.urlparse(self.domoticz_base_url)
                actual_host = base_url_parts.netloc
                
                # Update endpoints to use actual host instead of domoticz.local
                for key in ['authorization_endpoint', 'token_endpoint', 'issuer']:
                    if key in self.oauth_config:
                        endpoint_url = self.oauth_config[key]
                        if 'domoticz.local' in endpoint_url:
                            # Replace domoticz.local with actual host
                            self.oauth_config[key] = endpoint_url.replace('domoticz.local:8080', actual_host)
                            Domoticz.Debug(f"Fixed endpoint {key}: {self.oauth_config[key]}")
                
                Domoticz.Log(f"Discovered Domoticz OAuth endpoints: {well_known_url}")
                return True
            else:
                Domoticz.Error(f"Failed to discover OAuth endpoints: {response.status_code}")
                return False
                
        except Exception as e:
            Domoticz.Error(f"Error discovering OAuth endpoints: {e}")
            return False
    
    def get_authorization_url(self, client_id: str, redirect_uri: str, state: str = None, 
                            code_challenge: str = None, code_challenge_method: str = None):
        """Generate authorization URL for OAuth flow"""
        if not self.oauth_config:
            if not self.discover_oauth_endpoints():
                return None
                
        auth_endpoint = self.oauth_config.get('authorization_endpoint')
        if not auth_endpoint:
            return None
            
        params = {
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'scope': 'read write'
        }
        
        if state:
            params['state'] = state
        if code_challenge:
            params['code_challenge'] = code_challenge
            params['code_challenge_method'] = code_challenge_method or 'S256'
            
        return f"{auth_endpoint}?{urllib.parse.urlencode(params)}"
    
    def exchange_code_for_tokens(self, client_id: str, client_secret: str, 
                               authorization_code: str, redirect_uri: str, 
                               code_verifier: str = None):
        """Exchange authorization code for access token"""
        if not self.oauth_config:
            if not self.discover_oauth_endpoints():
                return None
                
        token_endpoint = self.oauth_config.get('token_endpoint')
        if not token_endpoint:
            return None
            
        token_data = {
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': authorization_code,
            'redirect_uri': redirect_uri
        }
        
        if code_verifier:
            token_data['code_verifier'] = code_verifier
            
        try:
            response = self.session.post(
                token_endpoint,
                data=token_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=10
            )
            
            if response.status_code == 200:
                token_response = response.json()
                Domoticz.Log("Successfully obtained OAuth tokens from Domoticz")
                return token_response
            else:
                Domoticz.Error(f"Failed to exchange code for tokens: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            Domoticz.Error(f"Error exchanging code for tokens: {e}")
            return None
    
    def make_authenticated_request(self, access_token: str, params: dict):
        """Make authenticated API call to Domoticz using OAuth access token"""
        try:
            api_endpoint = f"{self.domoticz_base_url}/json.htm"
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            response = self.session.get(
                api_endpoint,
                params=params,
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                Domoticz.Debug(f"Domoticz OAuth API call successful: {params.get('param', 'unknown')}")
                return result
            elif response.status_code == 401:
                return {"error": "OAuth token expired or invalid", "status_code": 401}
            else:
                return {"error": f"Domoticz API call failed: {response.status_code}"}
                
        except Exception as e:
            return {"error": f"Domoticz OAuth API call error: {str(e)}"}

class DomoticzMCPServer:
    """
    Domoticz MCP Server - MCP Protocol 2025-06-18 Compliant
    Acts as OAuth client to Domoticz, provides MCP protocol interface
    """
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8765, 
                 domoticz_oauth_client: DomoticzOAuthClient = None):
        """Initialize the Domoticz MCP Server with MCP 2025-06-18 compliance"""
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        self.domoticz_oauth_client = domoticz_oauth_client
        
        if AIOHTTP_AVAILABLE:
            self.app = web.Application()
            self.setup_routes()
            self.setup_cors()
    
    def setup_cors(self):
        """Setup CORS for cross-origin requests"""
        if not AIOHTTP_AVAILABLE:
            return
            
        try:
            cors = aiohttp_cors.setup(self.app, defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                    allow_methods="*"
                )
            })
            
            # Add CORS to all routes
            for route in list(self.app.router.routes()):
                cors.add(route)
        except Exception as e:
            Domoticz.Error(f"Error setting up CORS: {e}")
    
    def setup_routes(self):
        """Setup HTTP routes for MCP protocol 2025-06-18"""
        if not AIOHTTP_AVAILABLE:
            return
            
        try:
            # MCP endpoint (handles all MCP protocol messages)
            self.app.router.add_post('/mcp', self.handle_mcp_request)
            
            # Health and info endpoints
            self.app.router.add_get('/health', self.health_check)
            self.app.router.add_get('/info', self.server_info)
            
        except Exception as e:
            Domoticz.Error(f"Error setting up routes: {e}")
    
    async def health_check(self, request: web_request.Request) -> web_response.Response:
        """Health check endpoint"""
        return web.json_response({"status": "healthy", "service": "domoticz-mcp"})
    
    async def server_info(self, request: web_request.Request) -> web_response.Response:
        """Server info endpoint"""
        info = {
            "service": "Domoticz MCP Server",
            "version": "2.0.0",
            "protocol": "MCP 2025-06-18",
            "mcp_sdk_available": MCP_SDK_AVAILABLE,
            "aiohttp_available": AIOHTTP_AVAILABLE,
            "capabilities": {
                "tools": True,
                "logging": True
            },
            "authentication_model": "oauth_2_1_passthrough",
            "description": "MCP 2025-06-18 compliant server for Domoticz with OAuth passthrough authentication"
        }
        return web.json_response(info)

    async def handle_mcp_request(self, request: web_request.Request) -> web_response.Response:
        """Handle all MCP protocol requests - MCP 2025-06-18 compliant"""
        try:
            data = await request.json()
            method = data.get('method')
            params = data.get('params', {})
            request_id = data.get('id')
            
            Domoticz.Debug(f"MCP request: {method}")
            
            # Handle initialization - MCP 2025-06-18 specification
            if method == 'initialize':
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {
                            "tools": {}
                        },
                        "serverInfo": {
                            "name": "domoticz-mcp-server",
                            "version": "2.0.0"
                        }
                    }
                }
            
            # Handle tools/list
            elif method == 'tools/list':
                tools = await self.get_available_tools()
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": tools
                    }
                }
                
            # Handle tools/call - requires OAuth authentication
            elif method == 'tools/call':
                tool_name = params.get('name')
                arguments = params.get('arguments', {})
                
                # Check for Authorization header (MCP client authentication)
                auth_header = request.headers.get('Authorization')
                if not auth_header or not auth_header.startswith('Bearer '):
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": "Bearer token required in Authorization header"
                        }
                    }
                else:
                    # Extract token from Authorization header
                    access_token = auth_header[7:]  # Remove 'Bearer ' prefix
                    result = await self.execute_domoticz_tool(tool_name, arguments, access_token)
                    
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2)
                                }
                            ]
                        }
                    }
                
            # Handle logging/setLevel
            elif method == 'logging/setLevel':
                level = params.get('level', 'info')
                Domoticz.Log(f"Log level set to: {level}")
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {}
                }
                
            # Handle unknown methods
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}"
                    }
                }
            
            return web.json_response(response)
            
        except Exception as e:
            Domoticz.Error(f"Error handling MCP request: {e}")
            error_response = {
                "jsonrpc": "2.0",
                "id": data.get('id') if 'data' in locals() else None,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {str(e)}"
                }
            }
            return web.json_response(error_response, status=500)

    async def get_available_tools(self) -> List[Dict[str, Any]]:
        """Get all available MCP tools - read-only Domoticz operations"""
        return [
            {
                "name": "domoticz_get_version",
                "description": "Get Domoticz version information",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            },
            {
                "name": "domoticz_list_devices",
                "description": "List all Domoticz devices with optional filtering",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "enum": ["all", "light", "weather", "temperature", "utility"],
                            "default": "all",
                            "description": "Filter devices by type"
                        },
                        "used": {
                            "type": "boolean",
                            "default": True,
                            "description": "Only show devices that are in use"
                        }
                    },
                    "required": [],
                    "additionalProperties": False
                }
            },
            {
                "name": "domoticz_device_status",
                "description": "Get detailed status of a specific device",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "idx": {
                            "type": "integer",
                            "description": "Device index",
                            "minimum": 1
                        }
                    },
                    "required": ["idx"],
                    "additionalProperties": False
                }
            },
            {
                "name": "domoticz_list_scenes",
                "description": "List all scenes and groups",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            },
            {
                "name": "domoticz_get_log",
                "description": "Retrieve Domoticz logs",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "log_type": {
                            "type": "string",
                            "enum": ["status", "error", "notification"],
                            "default": "status",
                            "description": "Type of log to retrieve"
                        }
                    },
                    "required": [],
                    "additionalProperties": False
                }
            }
        ]

    async def execute_domoticz_tool(self, name: str, arguments: Dict[str, Any], access_token: str) -> Dict[str, Any]:
        """Execute a Domoticz tool using OAuth access token"""
        try:
            Domoticz.Debug(f"Executing tool: {name}")

            if not self.domoticz_oauth_client:
                return {"error": "Domoticz OAuth client not configured"}

            # Tool execution using OAuth access token
            if name == "domoticz_get_version":
                return self.domoticz_oauth_client.make_authenticated_request(
                    access_token, {"type": "command", "param": "getversion"}
                )

            elif name == "domoticz_list_devices":
                filter_type = arguments.get("filter", "all")
                used = arguments.get("used", True)
                params = {"type": "command", "param": "getdevices", "filter": filter_type}
                if used:
                    params["used"] = "true"
                return self.domoticz_oauth_client.make_authenticated_request(access_token, params)

            elif name == "domoticz_device_status":
                idx = arguments.get("idx")
                if not idx:
                    return {"error": "idx parameter is required"}
                return self.domoticz_oauth_client.make_authenticated_request(
                    access_token, {"type": "command", "param": "getdevices", "rid": str(idx)}
                )

            elif name == "domoticz_list_scenes":
                return self.domoticz_oauth_client.make_authenticated_request(
                    access_token, {"type": "command", "param": "getscenes"}
                )

            elif name == "domoticz_get_log":
                log_type = arguments.get("log_type", "status")
                return self.domoticz_oauth_client.make_authenticated_request(
                    access_token, {"type": "command", "param": "getlog", "log": log_type}
                )

            else:
                return {"error": f"Unknown tool: {name}"}

        except Exception as e:
            Domoticz.Error(f"Tool execution failed: {e}")
            return {"error": f"Tool execution failed: {str(e)}"}

    async def start_server(self):
        """Start the HTTP server"""
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

class BasePlugin:
    """Main Domoticz MCP Server Plugin class"""
    
    def __init__(self):
        self.mcp_server = None
        self.server_thread = None
        self.event_loop = None
        self.server_runner = None
        self.run_again = 6
        self.health_check_interval = 30
        self.auto_start_server = True
        self.host = "0.0.0.0"
        self.port = 8765
        self.plugin_path = plugin_path
        self.server_running = False
        self.last_health_check = 0
        self.server_start_time = None
        self.restart_attempts = 0
        self.max_restart_attempts = 3
        self.domoticz_oauth_client = None
        
        # Optional Domoticz URL override
        self.default_domoticz_url = ""

    def onStart(self):
        Domoticz.Debug("onStart called")
        
        # Set health check interval from parameters
        if Parameters["Mode2"] != "":
            self.health_check_interval = int(Parameters["Mode2"])
        
        # Set auto start preference
        self.auto_start_server = Parameters["Mode1"] == "true"
        Domoticz.Log(f"Auto start server is {'enabled' if self.auto_start_server else 'disabled'}")
        
        # Set optional Domoticz URL override
        self.default_domoticz_url = Parameters.get("Mode3", "").strip()
        
        if self.default_domoticz_url:
            Domoticz.Log(f"Domoticz URL override: {self.default_domoticz_url}")
        else:
            Domoticz.Log("Using default Domoticz URL: localhost:8080")
        
        # Set up Domoticz OAuth client
        domoticz_base_url = self.default_domoticz_url if self.default_domoticz_url else "http://127.0.0.1:8080"
        self.domoticz_oauth_client = DomoticzOAuthClient(domoticz_base_url)
        
        # Try to discover OAuth endpoints
        if self.domoticz_oauth_client.discover_oauth_endpoints():
            Domoticz.Log("Domoticz OAuth endpoints discovered successfully")
        else:
            Domoticz.Error("Failed to discover Domoticz OAuth endpoints - OAuth features may not work")
        
        # Set Debugging
        Domoticz.Debugging(int(Parameters["Mode6"]))
        
        # Debug information
        Domoticz.Debug(f"Plugin path: {self.plugin_path}")
        Domoticz.Debug(f"aiohttp available: {AIOHTTP_AVAILABLE}")
        Domoticz.Debug(f"MCP SDK available: {MCP_SDK_AVAILABLE}")
        
        # Create status device first
        self._create_status_device()
        
        # Check if we can run the server
        if not AIOHTTP_AVAILABLE:
            Domoticz.Error("aiohttp module not available. Server cannot be started.")
            Domoticz.Error("Please install aiohttp: pip install aiohttp aiohttp-cors")
            self._update_status_device(False, "aiohttp not available")
            Domoticz.Heartbeat(10)
            return
        
        # Start MCP server if auto start is enabled and aiohttp is available
        if self.auto_start_server:
            self._start_mcp_server()
        else:
            Domoticz.Log("MCP Server auto-start is disabled. Use the switch to start manually.")
            self._update_status_device(False, "Auto-start disabled")
        
        Domoticz.Heartbeat(10)
        
    def onStop(self):
        Domoticz.Debug("onStop called")
        self._stop_mcp_server()

    def _create_status_device(self):
        """Create status and control devices"""
        # Server status device
        if 1 not in Devices:
            Domoticz.Device(Name="MCP Server Status", Unit=1, TypeName="Switch", 
                           Description="MCP Server running status and control").Create()
            
        # Server info device (text)
        if 2 not in Devices:
            Domoticz.Device(Name="MCP Server Info", Unit=2, TypeName="Text",
                           Description="MCP Server information and statistics").Create()

    def _start_mcp_server(self):
        """Start the MCP server in a separate thread"""
        if not AIOHTTP_AVAILABLE:
            Domoticz.Error("Cannot start MCP server - aiohttp not available")
            self._update_status_device(False, "aiohttp not available")
            return False
            
        if self.server_running:
            Domoticz.Log("MCP Server is already running")
            return True
            
        try:
            Domoticz.Log(f"Starting MCP Server on {self.host}:{self.port}")
            
            # Set server_running to True BEFORE starting the thread
            # so the async keep-alive loop doesn't exit immediately
            self.server_running = True
            
            # Create and start the server thread
            self.server_thread = threading.Thread(target=self._run_server_async, daemon=True)
            self.server_thread.start()
            
            # Give the server more time to start and try multiple times
            for attempt in range(5):
                time.sleep(1)  # Wait 1 second between attempts
                if self._check_server_health():
                    self.server_start_time = time.time()
                    self.restart_attempts = 0
                    Domoticz.Log("MCP Server started successfully")
                    self._update_status_device(True, "Running")
                    return True
                else:
                    Domoticz.Debug(f"Health check attempt {attempt + 1}/5 failed, retrying...")
            
            # If we get here, all health checks failed - stop the server
            self.server_running = False
            Domoticz.Error("Failed to start MCP Server - health check failed after 5 attempts")
            self._update_status_device(False, "Failed to start")
            return False
                
        except Exception as e:
            self.server_running = False
            Domoticz.Error(f"Error starting MCP Server: {str(e)}")
            Domoticz.Error(traceback.format_exc())
            self._update_status_device(False, f"Error: {str(e)}")
            return False

    def _run_server_async(self):
        """Run the MCP server in an async event loop"""
        try:
            Domoticz.Log("DIAGNOSTIC: Starting async server thread")
            
            # Create new event loop for this thread
            self.event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.event_loop)
            Domoticz.Log("DIAGNOSTIC: Event loop created")
            
            # Create and start MCP server with Domoticz OAuth client
            self.mcp_server = DomoticzMCPServer(
                host=self.host, 
                port=self.port, 
                domoticz_oauth_client=self.domoticz_oauth_client
            )
            Domoticz.Log(f"DIAGNOSTIC: MCP server instance created for {self.host}:{self.port}")
            
            # Run the server
            self.event_loop.run_until_complete(self._async_server_main())
            
        except Exception as e:
            Domoticz.Error(f"MCP Server thread error: {str(e)}")
            Domoticz.Error(traceback.format_exc())
            self.server_running = False
        finally:
            if self.event_loop:
                try:
                    self.event_loop.close()
                except:
                    pass
                self.event_loop = None

    async def _async_server_main(self):
        """Main async server loop"""
        try:
            Domoticz.Log("DIAGNOSTIC: Starting server in async loop")
            
            # Start the server
            self.server_runner = await self.mcp_server.start_server()
            Domoticz.Log(f"DIAGNOSTIC: Server runner created: {self.server_runner is not None}")
            
            if self.server_runner:
                Domoticz.Log("DIAGNOSTIC: Server started successfully, entering keep-alive loop")
                # Keep the server running
                while self.server_running:
                    await asyncio.sleep(1)
                Domoticz.Log("DIAGNOSTIC: Keep-alive loop ended")
            else:
                Domoticz.Error("DIAGNOSTIC: Server runner is None - server failed to start")
                
        except Exception as e:
            Domoticz.Error(f"Async server error: {str(e)}")
            Domoticz.Error(traceback.format_exc())
            raise
        finally:
            # Cleanup
            Domoticz.Log("DIAGNOSTIC: Cleaning up server runner")
            if self.server_runner:
                try:
                    await self.server_runner.cleanup()
                except:
                    pass

    def _stop_mcp_server(self):
        """Stop the MCP server"""
        if not self.server_running:
            Domoticz.Log("MCP Server is not running")
            return
            
        try:
            Domoticz.Log("Stopping MCP Server...")
            self.server_running = False
            
            # Give the server thread time to shutdown gracefully
            if self.server_thread and self.server_thread.is_alive():
                self.server_thread.join(timeout=5)
                
            # Force cleanup if needed
            if self.event_loop and not self.event_loop.is_closed():
                try:
                    # Cancel all running tasks
                    if self.event_loop.is_running():
                        for task in asyncio.all_tasks(self.event_loop):
                            task.cancel()
                except:
                    pass
                    
            self.mcp_server = None
            self.server_runner = None
            self.server_thread = None
            self.event_loop = None
            self.server_start_time = None
            
            Domoticz.Log("MCP Server stopped")
            self._update_status_device(False, "Stopped")
            
        except Exception as e:
            Domoticz.Error(f"Error stopping MCP Server: {str(e)}")
            Domoticz.Error(traceback.format_exc())

    def _check_server_health(self):
        """Check if the MCP server is responding"""
        try:
            # Use localhost/127.0.0.1 for health check instead of 0.0.0.0
            # since 0.0.0.0 is only for binding, not for making requests
            health_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            health_url = f"http://{health_host}:{self.port}/health"
            
            response = requests.get(health_url, timeout=3)
            
            if response.status_code == 200:
                data = response.json()
                is_healthy = data.get("status") == "healthy"
                Domoticz.Debug(f"Health check SUCCESS - Response: {data}")
                return is_healthy
            else:
                Domoticz.Debug(f"Health check FAILED - Status code: {response.status_code}")
                return False
                
        except requests.exceptions.ConnectionError as e:
            Domoticz.Debug(f"Health check CONNECTION ERROR: {str(e)}")
            return False
        except requests.exceptions.Timeout as e:
            Domoticz.Debug(f"Health check TIMEOUT: {str(e)}")
            return False
        except Exception as e:
            Domoticz.Debug(f"Health check UNEXPECTED ERROR: {str(e)}")
            return False

    def _get_server_info(self):
        """Get server information"""
        try:
            # Use localhost/127.0.0.1 for info request instead of 0.0.0.0
            info_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            info_url = f"http://{info_host}:{self.port}/info"
            response = requests.get(info_url, timeout=5)
            
            if response.status_code == 200:
                return response.json()
            else:
                return None
                
        except Exception as e:
            Domoticz.Debug(f"Info request failed: {str(e)}")
            return None

    def _update_status_device(self, is_running: bool, status_text: str):
        """Update the status device"""
        try:
            # Update switch device
            if 1 in Devices:
                if is_running:
                    Devices[1].Update(nValue=1, sValue="On")
                else:
                    Devices[1].Update(nValue=0, sValue="Off")
            
            # Update info device
            if 2 in Devices:
                info = {
                    "status": status_text,
                    "host": self.host,
                    "port": self.port,
                    "aiohttp_available": AIOHTTP_AVAILABLE,
                    "mcp_sdk_available": MCP_SDK_AVAILABLE,
                    "uptime": int(time.time() - self.server_start_time) if self.server_start_time else 0,
                    "last_check": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "restart_attempts": self.restart_attempts,
                    "protocol_version": "MCP 2025-06-18",
                    "authentication": "OAuth 2.1 passthrough",
                    "domoticz_oauth_configured": self.domoticz_oauth_client.oauth_config is not None
                }
                
                # Get additional server info if available
                server_info = self._get_server_info()
                if server_info:
                    info.update(server_info)
                
                Devices[2].Update(nValue=0, sValue=json.dumps(info, indent=2))
                
        except Exception as e:
            Domoticz.Error(f"Error updating status device: {str(e)}")

    def onHeartbeat(self):
        current_time = time.time()
        
        # Check server health periodically
        self.run_again -= 1
        if self.run_again <= 0:
            self.run_again = self.health_check_interval / 10  # Set for next health check interval
            self.last_health_check = current_time
            
            if self.server_running:
                # Check if server is still healthy
                if self._check_server_health():
                    Domoticz.Debug("MCP Server health check: OK")
                    self._update_status_device(True, "Running")
                else:
                    Domoticz.Error("MCP Server health check failed")
                    self.server_running = False
                    
                    # Try to restart if we haven't exceeded max attempts
                    if self.restart_attempts < self.max_restart_attempts:
                        self.restart_attempts += 1
                        Domoticz.Log(f"Attempting to restart MCP Server (attempt {self.restart_attempts}/{self.max_restart_attempts})")
                        self._stop_mcp_server()
                        time.sleep(2)
                        self._start_mcp_server()
                    else:
                        Domoticz.Error(f"Max restart attempts ({self.max_restart_attempts}) reached. Manual intervention required.")
                        self._update_status_device(False, "Failed - Max restarts exceeded")
            else:
                # Update status to show server is not running
                if self.auto_start_server and self.restart_attempts < self.max_restart_attempts and AIOHTTP_AVAILABLE:
                    # Try to restart if auto-start is enabled and we haven't exceeded attempts
                    Domoticz.Log("Server not running but auto-start enabled - attempting restart")
                    self._start_mcp_server()
                else:
                    status = "Not running"
                    if not AIOHTTP_AVAILABLE:
                        status = "aiohttp not available"
                    self._update_status_device(False, status)

    def onCommand(self, Unit, Command, Level, Hue):
        """Handle commands sent to devices"""
        Domoticz.Debug(f"onCommand called for Unit: {Unit} Command: {Command} Level: {Level}")
        
        try:
            if Unit == 1:  # Server control switch
                if Command == "On":
                    if not self.server_running:
                        if AIOHTTP_AVAILABLE:
                            self._start_mcp_server()
                        else:
                            Domoticz.Error("Cannot start MCP server - aiohttp not available")
                            self._update_status_device(False, "aiohttp not available")
                    else:
                        Domoticz.Log("MCP Server is already running")
                elif Command == "Off":
                    if self.server_running:
                        self._stop_mcp_server()
                    else:
                        Domoticz.Log("MCP Server is not running")
                        
        except Exception as e:
            Domoticz.Error(f"Error handling command: {str(e)}")
            Domoticz.Error(traceback.format_exc())

# Global plugin instance
_plugin = BasePlugin()

def onStart():
    global _plugin
    _plugin.onStart()

def onStop():
    global _plugin
    _plugin.onStop()

def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()

def onCommand(Unit, Command, Level, Hue):
    global _plugin
    _plugin.onCommand(Unit, Command, Level, Hue)