"""
<plugin key="Domoticz-MCP-Server" name="Domoticz MCP Server Plugin" author="Mark Heinis" version="1.0.0" wikilink="https://github.com/galadril/Domoticz-MCP-Service/wiki" externallink="https://github.com/galadril/Domoticz-MCP-Service">
    <description>
        Plugin for running Domoticz MCP (Model Context Protocol) Server.
        Provides AI assistant access to Domoticz functionality through MCP protocol.
    </description>
    <params>
        <param field="Mode1" label="Auto Start Server" width="75px">
            <options>
                <option label="Yes" value="true" default="true"/>
                <option label="No" value="false"/>
            </options>
        </param>
        <param field="Mode2" label="Health Check interval (seconds)" width="30px" required="true" default="30"/>
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
from typing import Optional, Dict, Any, List
from datetime import datetime

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
    Domoticz.Error(f"aiohttp not available: {e}")
    Domoticz.Error("MCP server will run in simple mode without HTTP endpoints")

# Check MCP SDK availability for future use
try:
    import mcp
    MCP_SDK_AVAILABLE = True
except ImportError:
    MCP_SDK_AVAILABLE = False

class DomoticzMCPServer:
    """
    Embedded Domoticz MCP Server - MCP Protocol Compliant
    A Model Context Protocol server that provides secure access to Domoticz home automation APIs.
    """
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        """Initialize the Domoticz MCP Server with full protocol compliance"""
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        
        if AIOHTTP_AVAILABLE:
            self.app = web.Application()
            self.setup_routes()
            self.setup_cors()
    
    def setup_cors(self):
        """Setup CORS for cross-origin requests"""
        if not AIOHTTP_AVAILABLE:
            return
            
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
    
    def setup_routes(self):
        """Setup HTTP routes for MCP protocol"""
        if not AIOHTTP_AVAILABLE:
            return
            
        # MCP endpoint (handles all MCP protocol messages)
        self.app.router.add_post('/mcp', self.handle_mcp_request)
        
        # Health and info endpoints
        self.app.router.add_get('/health', self.health_check)
        self.app.router.add_get('/info', self.server_info)
    
    async def health_check(self, request: web_request.Request) -> web_response.Response:
        """Health check endpoint"""
        return web.json_response({"status": "healthy", "service": "domoticz-mcp"})
    
    async def server_info(self, request: web_request.Request) -> web_response.Response:
        """Server info endpoint"""
        info = {
            "service": "Domoticz MCP Server",
            "version": "3.1.0",
            "protocol": "MCP 1.0",
            "mcp_sdk_available": MCP_SDK_AVAILABLE,
            "aiohttp_available": AIOHTTP_AVAILABLE,
            "capabilities": {
                "tools": True,
                "logging": True,
                "dynamic_discovery": True
            },
            "authentication_model": "per_request_domoticz_credentials",
            "description": "MCP 1.0 compliant server for Domoticz home automation"
        }
        return web.json_response(info)

    async def handle_mcp_request(self, request: web_request.Request) -> web_response.Response:
        """Handle all MCP protocol requests with full compliance"""
        try:
            data = await request.json()
            method = data.get('method')
            params = data.get('params', {})
            request_id = data.get('id')
            
            Domoticz.Debug(f"MCP request: {method}")
            
            # Handle initialization
            if method == 'initialize':
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {},
                            "logging": {}
                        },
                        "serverInfo": {
                            "name": "domoticz-mcp",
                            "version": "3.1.0"
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
                
            # Handle tools/call
            elif method == 'tools/call':
                tool_name = params.get('name')
                arguments = params.get('arguments', {})
                
                result = await self.execute_domoticz_tool(tool_name, arguments)
                
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
                
            # Handle logging/setLevel (optional MCP feature)
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
        """Get all available MCP tools with proper schema"""
        return [
            {
                "name": "get_status",
                "description": "Get Domoticz system status information",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"}
                    },
                    "required": ["domoticz_url"]
                }
            },
            {
                "name": "get_version",
                "description": "Get Domoticz version information",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"}
                    },
                    "required": ["domoticz_url"]
                }
            },
            {
                "name": "list_devices",
                "description": "List all Domoticz devices with optional filtering",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"},
                        "filter": {"type": "string", "enum": ["all", "light", "weather", "temperature", "utility"], "default": "all"},
                        "used": {"type": "boolean", "default": True}
                    },
                    "required": ["domoticz_url"]
                }
            },
            {
                "name": "device_status",
                "description": "Get detailed status of a specific device",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"},
                        "idx": {"type": "integer", "description": "Device index"}
                    },
                    "required": ["domoticz_url", "idx"]
                }
            },
            {
                "name": "switch_device",
                "description": "Control device switching (on/off/toggle) and dimmer levels",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"},
                        "idx": {"type": "integer", "description": "Device index"},
                        "command": {"type": "string", "enum": ["On", "Off", "Toggle", "Set Level"], "description": "Switch command"},
                        "level": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Dimmer level (0-100)"}
                    },
                    "required": ["domoticz_url", "idx", "command"]
                }
            },
            {
                "name": "list_scenes",
                "description": "List all scenes and groups",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"}
                    },
                    "required": ["domoticz_url"]
                }
            },
            {
                "name": "run_scene",
                "description": "Execute a scene or group",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"},
                        "idx": {"type": "integer", "description": "Scene index"},
                        "action": {"type": "string", "enum": ["On", "Off"], "default": "On"}
                    },
                    "required": ["domoticz_url", "idx"]
                }
            },
            {
                "name": "set_thermostat",
                "description": "Set thermostat setpoint",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"},
                        "idx": {"type": "integer", "description": "Thermostat device index"},
                        "setpoint": {"type": "number", "description": "Temperature setpoint"}
                    },
                    "required": ["domoticz_url", "idx", "setpoint"]
                }
            },
            {
                "name": "send_notification",
                "description": "Send notification through Domoticz",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"},
                        "subject": {"type": "string", "description": "Notification subject"},
                        "message": {"type": "string", "description": "Notification message"},
                        "priority": {"type": "integer", "minimum": 0, "maximum": 4, "default": 0}
                    },
                    "required": ["domoticz_url", "subject", "message"]
                }
            },
            {
                "name": "get_log",
                "description": "Retrieve Domoticz logs",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "domoticz_url": {"type": "string", "description": "Domoticz JSON API URL"},
                        "domoticz_username": {"type": "string", "description": "Domoticz username"},
                        "domoticz_password": {"type": "string", "description": "Domoticz password"},
                        "log_type": {"type": "string", "enum": ["status", "error", "notification"], "default": "status"}
                    },
                    "required": ["domoticz_url"]
                }
            }
        ]

    async def execute_domoticz_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a Domoticz tool with the given arguments"""
        try:
            Domoticz.Debug(f"Executing tool: {name}")
            
            # Extract Domoticz credentials from arguments
            domoticz_url = arguments.get("domoticz_url")
            domoticz_username = arguments.get("domoticz_username", "")
            domoticz_password = arguments.get("domoticz_password", "")
            
            if not domoticz_url:
                return {"error": "domoticz_url is required in arguments"}
            
            # Remove credentials from arguments before processing tool logic
            tool_args = {k: v for k, v in arguments.items() 
                        if k not in ["domoticz_url", "domoticz_username", "domoticz_password"]}
            
            # Execute the appropriate tool
            if name == "get_status":
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password, 
                                              {"type":"command","param":"status"})
                
            elif name == "get_version":
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password,
                                              {"type":"command","param":"getversion"})
                
            elif name == "list_devices":
                filter_type = tool_args.get("filter", "all")
                used = tool_args.get("used", True)
                params = {"type":"command","param":"getdevices","filter":filter_type}
                if used:
                    params["used"] = "true"
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password, params)
                
            elif name == "device_status":
                idx = tool_args.get("idx")
                if not idx:
                    return {"error": "idx parameter is required"}
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password,
                                              {"type":"command","param":"getdevices","rid":str(idx)})
                
            elif name == "switch_device":
                idx = tool_args.get("idx")
                command = tool_args.get("command")
                level = tool_args.get("level")
                if not idx or not command:
                    return {"error": "idx and command parameters are required"}
                params = {"type":"command","param":"switchlight","idx":idx,"switchcmd":command}
                if level is not None:
                    params["level"] = level
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password, params)
                
            elif name == "list_scenes":
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password,
                                              {"type":"command","param":"getscenes"})
                
            elif name == "run_scene":
                idx = tool_args.get("idx")
                action = tool_args.get("action", "On")
                if not idx:
                    return {"error": "idx parameter is required"}
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password,
                                              {"type":"command","param":"switchscene","idx":idx,"switchcmd":action})
                
            elif name == "set_thermostat":
                idx = tool_args.get("idx")
                setpoint = tool_args.get("setpoint")
                if not idx or setpoint is None:
                    return {"error": "idx and setpoint parameters are required"}
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password,
                                              {"type":"command","param":"setsetpoint","idx":idx,"setpoint":setpoint})
                
            elif name == "send_notification":
                subject = tool_args.get("subject")
                message = tool_args.get("message")
                priority = tool_args.get("priority", 0)
                if not subject or not message:
                    return {"error": "subject and message parameters are required"}
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password, {
                    "type": "command",
                    "param": "sendnotification",
                    "subject": subject,
                    "body": message,
                    "priority": priority
                })
                
            elif name == "get_log":
                log_type = tool_args.get("log_type", "status")
                result = self.domoticz_api_call(domoticz_url, domoticz_username, domoticz_password,
                                              {"type":"command","param":"getlog","log":log_type})
                
            else:
                result = {"error": f"Unknown tool: {name}"}
                
            return result
            
        except Exception as e:
            Domoticz.Error(f"Tool execution failed: {e}")
            return {"error": f"Tool execution failed: {str(e)}"}

    def domoticz_api_call(self, domoticz_url: str, username: str, password: str, params: dict):
        """Call Domoticz JSON API directly with provided credentials"""
        try:
            # Use the proven cookie-based authentication method
            if username and password:
                # Create session and login
                requests_session = requests.Session()
                
                login_url = domoticz_url
                login_params = {"type": "command", "param": "logincheck"}
                
                username_b64 = base64.b64encode(username.encode('utf-8')).decode('utf-8')
                password_md5 = hashlib.md5(password.encode('utf-8')).hexdigest()
                
                login_data = {
                    'username': username_b64,
                    'password': password_md5,
                    'rememberme': 'false'
                }
                
                login_resp = requests_session.post(login_url, params=login_params, data=login_data, timeout=10)
                
                if login_resp.status_code == 200:
                    login_result = login_resp.json()
                    if login_result.get('status') == 'OK':
                        # Now make the actual API call
                        resp = requests_session.get(domoticz_url, params=params, timeout=10)
                        
                        if resp.status_code == 200:
                            api_result = resp.json()
                            return api_result
                        else:
                            return {"error": f"API call failed: {resp.status_code}"}
                    else:
                        return {"error": f"Login failed: {login_result}"}
                else:
                    return {"error": f"Login request failed: {login_resp.status_code}"}
            else:
                # No authentication - try direct call
                resp = requests.get(domoticz_url, params=params, timeout=10)
                resp.raise_for_status()
                return resp.json()
            
        except requests.exceptions.Timeout:
            return {"error": "Request timeout - check if Domoticz server is reachable"}
        except requests.exceptions.ConnectionError:
            return {"error": "Connection error - check Domoticz URL and network"}
        except Exception as e:
            return {"error": str(e)}

    async def start_server(self):
        """Start the HTTP server"""
        if not AIOHTTP_AVAILABLE:
            Domoticz.Error("aiohttp not available - cannot start HTTP server")
            return None
            
        runner = web.AppRunner(self.app)
        await runner.setup()
        
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        
        Domoticz.Log(f"Domoticz MCP Server v3.1.0 started on http://{self.host}:{self.port}")
        Domoticz.Log(f"Health check: http://{self.host}:{self.port}/health")
        Domoticz.Log(f"Server info: http://{self.host}:{self.port}/info")
        Domoticz.Log(f"MCP endpoint: http://{self.host}:{self.port}/mcp")
        Domoticz.Log(f"Protocol: MCP 1.0 compliant")
        Domoticz.Log(f"Authentication: Per-request Domoticz credentials")
        
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
        
    def onStart(self):
        Domoticz.Debug("onStart called")
        
        # Set health check interval from parameters
        if Parameters["Mode2"] != "":
            self.health_check_interval = int(Parameters["Mode2"])
        
        # Set auto start preference
        self.auto_start_server = Parameters["Mode1"] == "true"
        Domoticz.Log(f"Auto start server is {'enabled' if self.auto_start_server else 'disabled'}")
        
        # Set Debugging
        Domoticz.Debugging(int(Parameters["Mode6"]))
        
        # Debug information
        Domoticz.Debug(f"Plugin path: {self.plugin_path}")
        Domoticz.Debug(f"aiohttp available: {AIOHTTP_AVAILABLE}")
        Domoticz.Debug(f"MCP SDK available: {MCP_SDK_AVAILABLE}")
        
        # Check if we can run the server
        if not AIOHTTP_AVAILABLE:
            Domoticz.Error("aiohttp module not available. Server cannot be started.")
            Domoticz.Error("Please install aiohttp: pip install aiohttp aiohttp-cors")
            # Still create devices for monitoring
            self._create_status_device()
            return
        
        # Create status device
        self._create_status_device()
        
        # Start MCP server if auto start is enabled and aiohttp is available
        if self.auto_start_server and AIOHTTP_AVAILABLE:
            self._start_mcp_server()
        else:
            if not AIOHTTP_AVAILABLE:
                Domoticz.Log("MCP Server cannot start - aiohttp not available")
                self._update_status_device(False, "aiohttp not available")
            else:
                Domoticz.Log("MCP Server auto-start is disabled. Use the switch to start manually.")
        
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
            
            # Create and start the server thread
            self.server_thread = threading.Thread(target=self._run_server_async, daemon=True)
            self.server_thread.start()
            
            # Give the server a moment to start
            time.sleep(2)
            
            # Check if server started successfully
            if self._check_server_health():
                self.server_running = True
                self.server_start_time = time.time()
                self.restart_attempts = 0
                Domoticz.Log("MCP Server started successfully")
                self._update_status_device(True, "Running")
                return True
            else:
                Domoticz.Error("Failed to start MCP Server - health check failed")
                self._update_status_device(False, "Failed to start")
                return False
                
        except Exception as e:
            Domoticz.Error(f"Error starting MCP Server: {str(e)}")
            Domoticz.Error(traceback.format_exc())
            self._update_status_device(False, f"Error: {str(e)}")
            return False

    def _run_server_async(self):
        """Run the MCP server in an async event loop"""
        try:
            # Create new event loop for this thread
            self.event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.event_loop)
            
            # Create and start MCP server
            self.mcp_server = DomoticzMCPServer(host=self.host, port=self.port)
            
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
            # Start the server
            self.server_runner = await self.mcp_server.start_server()
            
            # Keep the server running
            while self.server_running:
                await asyncio.sleep(1)
                
        except Exception as e:
            Domoticz.Error(f"Async server error: {str(e)}")
            raise
        finally:
            # Cleanup
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
            health_url = f"http://{self.host}:{self.port}/health"
            response = requests.get(health_url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                return data.get("status") == "healthy"
            else:
                return False
                
        except Exception as e:
            Domoticz.Debug(f"Health check failed: {str(e)}")
            return False

    def _get_server_info(self):
        """Get server information"""
        try:
            info_url = f"http://{self.host}:{self.port}/info"
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
                    "restart_attempts": self.restart_attempts
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