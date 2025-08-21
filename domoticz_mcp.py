#!/usr/bin/env python3
"""
Domoticz MCP Server - MCP Protocol Compliant
A Model Context Protocol server that provides secure access to Domoticz home automation APIs.

Features:
- MCP 1.0 protocol compliance
- Dynamic tool discovery from Domoticz
- Per-request authentication with Domoticz credentials
- Proper JSON-RPC 2.0 implementation

Authentication Model:
- Clients provide Domoticz credentials via MCP client configuration
- No server-side user management required
- Direct authentication with Domoticz per request
"""
import asyncio
import json
import logging
from aiohttp import web, web_request, web_response
from aiohttp.web_runner import GracefulExit
import aiohttp_cors
from typing import Dict, Any, List, Optional
import sys
import signal
import requests
import base64
import hashlib
from datetime import datetime
import os

# Configure logging first
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Check MCP SDK availability for future use
try:
    import mcp
    MCP_SDK_AVAILABLE = True
    logger.info("MCP SDK available for future enhancements")
except ImportError:
    MCP_SDK_AVAILABLE = False
    logger.info("MCP SDK not available - using manual implementation")

class DomoticzMCPServer:
    def __init__(self, 
                 # Server settings
                 host: str = "0.0.0.0", 
                 port: int = 8765):
        """
        Initialize the Domoticz MCP Server with full protocol compliance
        
        Args:
            host: Server host to bind to
            port: Server port to bind to
        """
        # Server settings
        self.host = host
        self.port = port
        
        # HTTP server for transport
        self.app = web.Application()
        self.setup_routes()
        self.setup_cors()

    def setup_cors(self):
        """Setup CORS for cross-origin requests"""
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
            
            logger.info(f"MCP request: {method}")
            
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
                logger.info(f"Log level set to: {level}")
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
            logger.error(f"Error handling MCP request: {e}")
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
            logger.info(f"Executing tool: {name}")
            
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
            logger.error(f"Tool execution failed: {e}")
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
        runner = web.AppRunner(self.app)
        await runner.setup()
        
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        
        logger.info(f"🏠 Domoticz MCP Server v3.1.0 started on http://{self.host}:{self.port}")
        logger.info(f"📋 Health check: http://{self.host}:{self.port}/health")
        logger.info(f"📋 Server info: http://{self.host}:{self.port}/info")
        logger.info(f"🔗 MCP endpoint: http://{self.host}:{self.port}/mcp")
        logger.info(f"🔧 Protocol: MCP 1.0 compliant")
        logger.info(f"🔐 Authentication: Per-request Domoticz credentials")
        logger.info("🔍 Tool discovery: Dynamic based on Domoticz capabilities")
        logger.info("✅ AI Compatible: Full MCP 1.0 implementation")
        
        return runner

async def main(# Server settings only
               host: str = "0.0.0.0",
               port: int = 8765,
               # Legacy parameters (ignored for compatibility)
               **kwargs):
    """
    Main entry point for MCP 1.0 compliant server
    
    Args:
        host: Server host to bind to
        port: Server port to bind to
        **kwargs: Legacy parameters (ignored)
    """
    server = DomoticzMCPServer(host=host, port=port)
    runner = await server.start_server()
    
    # Setup graceful shutdown
    def signal_handler():
        logger.info("Received shutdown signal")
        raise GracefulExit()
    
    # Handle signals
    if sys.platform != 'win32':
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, signal_handler)
    
    try:
        # Keep the server running
        while True:
            await asyncio.sleep(1)
    except (GracefulExit, KeyboardInterrupt):
        logger.info("Shutting down server...")
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    # Server settings only
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "8765"))
    
    try:
        asyncio.run(main(host=host, port=port))
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
