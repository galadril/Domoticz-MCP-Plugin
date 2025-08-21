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
from typing import Optional

# Add the plugin directory to Python path to ensure imports work
plugin_path = os.path.dirname(os.path.realpath(__file__))
if plugin_path not in sys.path:
    sys.path.insert(0, plugin_path)

# Import our MCP server module with better error handling
try:
    # Try different import methods
    try:
        from domoticz_mcp import DomoticzMCPServer
        Domoticz.Debug("Successfully imported DomoticzMCPServer from domoticz_mcp")
    except ImportError:
        # Try importing from the current directory
        import domoticz_mcp
        DomoticzMCPServer = domoticz_mcp.DomoticzMCPServer
        Domoticz.Debug("Successfully imported DomoticzMCPServer via domoticz_mcp module")
    
    MCP_MODULE_AVAILABLE = True
    Domoticz.Log("MCP module loaded successfully")
    
except ImportError as e:
    Domoticz.Error(f"Failed to import domoticz_mcp module: {e}")
    Domoticz.Error(f"Plugin path: {plugin_path}")
    Domoticz.Error(f"Python path: {sys.path}")
    MCP_MODULE_AVAILABLE = False
    DomoticzMCPServer = None
except Exception as e:
    Domoticz.Error(f"Unexpected error importing domoticz_mcp module: {e}")
    Domoticz.Error(traceback.format_exc())
    MCP_MODULE_AVAILABLE = False
    DomoticzMCPServer = None

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
        Domoticz.Debug(f"MCP module available: {MCP_MODULE_AVAILABLE}")
        
        # Check if MCP module is available
        if not MCP_MODULE_AVAILABLE:
            Domoticz.Error("MCP module not available. Server cannot be started.")
            Domoticz.Error("Please ensure domoticz_mcp.py is in the plugin directory and has no syntax errors.")
            return
        
        # Create status device
        self._create_status_device()
        
        # Start MCP server if auto start is enabled
        if self.auto_start_server:
            self._start_mcp_server()
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
                Domoticz.Log("✅ MCP Server started successfully")
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
            
            Domoticz.Log("✅ MCP Server stopped")
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
                if self.auto_start_server and self.restart_attempts < self.max_restart_attempts:
                    # Try to restart if auto-start is enabled and we haven't exceeded attempts
                    Domoticz.Log("Server not running but auto-start enabled - attempting restart")
                    self._start_mcp_server()
                else:
                    self._update_status_device(False, "Not running")

    def onCommand(self, Unit, Command, Level, Hue):
        """Handle commands sent to devices"""
        Domoticz.Debug(f"onCommand called for Unit: {Unit} Command: {Command} Level: {Level}")
        
        try:
            if Unit == 1:  # Server control switch
                if Command == "On":
                    if not self.server_running:
                        self._start_mcp_server()
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