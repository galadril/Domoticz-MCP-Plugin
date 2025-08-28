import asyncio
import json
import os
import threading
import time
from typing import Optional

import Domoticz
import requests

from oauth_client import DomoticzOAuthClient
from mcp_server import DomoticzMCPServer, AIOHTTP_AVAILABLE, MCP_SDK_AVAILABLE

plugin_path = os.path.dirname(os.path.realpath(__file__))

class BasePlugin:
    def __init__(self):
        self.mcp_server: Optional[DomoticzMCPServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None
        self.server_runner = None
        self.run_again = 6
        self.health_check_interval = 30
        self.auto_start_server = True
        self.host = "0.0.0.0"
        self.port = 8765
        self.plugin_path = plugin_path
        self.server_running = False
        self.last_health_check = 0
        self.server_start_time: Optional[float] = None
        self.restart_attempts = 0
        self.max_restart_attempts = 3
        self.domoticz_oauth_client: Optional[DomoticzOAuthClient] = None
        self.default_domoticz_url = ""

    # ---- Domoticz callbacks ----------------------------------------------
    def onStart(self):
        Domoticz.Debug("onStart called")
        if Parameters["Mode2"] != "":
            self.health_check_interval = int(Parameters["Mode2"])
        self.auto_start_server = Parameters["Mode1"] == "true"
        Domoticz.Log(f"Auto start server is {'enabled' if self.auto_start_server else 'disabled'}")
        self.default_domoticz_url = Parameters.get("Mode3", "").strip()
        Domoticz.Log(f"Domoticz URL override: {self.default_domoticz_url}" if self.default_domoticz_url else "Using default Domoticz URL: http://127.0.0.1:8080")
        domoticz_base_url = self.default_domoticz_url if self.default_domoticz_url else "http://127.0.0.1:8080"
        self.domoticz_oauth_client = DomoticzOAuthClient(domoticz_base_url)
        if self.domoticz_oauth_client.discover_oauth_endpoints():
            Domoticz.Log("Domoticz OAuth endpoints discovered successfully")
        else:
            Domoticz.Error("Failed to discover Domoticz OAuth endpoints - OAuth features may not work")
        Domoticz.Debugging(int(Parameters["Mode6"]))
        self._create_status_device()
        if not AIOHTTP_AVAILABLE:
            Domoticz.Error("aiohttp module not available. Server cannot be started.")
            self._update_status_device(False, "aiohttp not available")
            Domoticz.Heartbeat(10)
            return
        if self.auto_start_server:
            self._start_mcp_server()
        else:
            self._update_status_device(False, "Auto-start disabled")
        Domoticz.Heartbeat(10)

    def onStop(self):
        self._stop_mcp_server()

    def onHeartbeat(self):
        self.run_again -= 1
        if self.run_again <= 0:
            self.run_again = self.health_check_interval / 10
            if self.server_running:
                if self._check_server_health():
                    self._update_status_device(True, "Running")
                else:
                    self.server_running = False
                    if self.restart_attempts < self.max_restart_attempts and AIOHTTP_AVAILABLE:
                        self.restart_attempts += 1
                        self._stop_mcp_server()
                        time.sleep(2)
                        self._start_mcp_server()
                    else:
                        self._update_status_device(False, "Failed - Max restarts exceeded")
            else:
                if self.auto_start_server and self.restart_attempts < self.max_restart_attempts and AIOHTTP_AVAILABLE:
                    self._start_mcp_server()
                else:
                    self._update_status_device(False, "Not running" if AIOHTTP_AVAILABLE else "aiohttp not available")

    def onCommand(self, Unit, Command, Level, Hue):
        try:
            if Unit == 1:
                if Command == "On" and not self.server_running:
                    if AIOHTTP_AVAILABLE:
                        self._start_mcp_server()
                    else:
                        self._update_status_device(False, "aiohttp not available")
                elif Command == "Off" and self.server_running:
                    self._stop_mcp_server()
        except Exception as e:
            Domoticz.Error(f"Error handling command: {e}")

    # ---- internal helpers -------------------------------------------------
    def _create_status_device(self):
        if 1 not in Devices:
            Domoticz.Device(Name="MCP Server Status", Unit=1, TypeName="Switch", Description="MCP Server running status and control").Create()
        if 2 not in Devices:
            Domoticz.Device(Name="MCP Server Info", Unit=2, TypeName="Text", Description="MCP Server information and statistics").Create()

    def _start_mcp_server(self):
        if not AIOHTTP_AVAILABLE:
            self._update_status_device(False, "aiohttp not available")
            return False
        if self.server_running:
            return True
        try:
            self.server_running = True
            self.server_thread = threading.Thread(target=self._run_server_async, daemon=True)
            self.server_thread.start()
            for _ in range(5):
                time.sleep(1)
                if self._check_server_health():
                    self.server_start_time = time.time()
                    self.restart_attempts = 0
                    self._update_status_device(True, "Running")
                    return True
            self.server_running = False
            self._update_status_device(False, "Failed to start")
            return False
        except Exception as e:
            self.server_running = False
            self._update_status_device(False, f"Error: {e}")
            return False

    def _run_server_async(self):
        try:
            self.event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.event_loop)
            self.mcp_server = DomoticzMCPServer(host=self.host, port=self.port, domoticz_oauth_client=self.domoticz_oauth_client)
            self.event_loop.run_until_complete(self._async_server_main())
        except Exception as e:
            Domoticz.Error(f"MCP Server thread error: {e}")
            self.server_running = False
        finally:
            if self.event_loop:
                try:
                    self.event_loop.close()
                except:  # pragma: no cover
                    pass
                self.event_loop = None

    async def _async_server_main(self):
        try:
            self.server_runner = await self.mcp_server.start_server()
            if self.server_runner:
                while self.server_running:
                    await asyncio.sleep(1)
        finally:
            if self.server_runner:
                try:
                    await self.server_runner.cleanup()
                except:  # pragma: no cover
                    pass

    def _stop_mcp_server(self):
        if not self.server_running:
            return
        self.server_running = False
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=5)
        self.mcp_server = None
        self.server_runner = None
        self.server_thread = None
        self.event_loop = None
        self.server_start_time = None
        self._update_status_device(False, "Stopped")

    def _check_server_health(self):
        try:
            host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            r = requests.get(f"http://{host}:{self.port}/health", timeout=3)
            return r.status_code == 200 and r.json().get("status") == "healthy"
        except Exception:
            return False

    def _get_server_info(self):
        try:
            host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            r = requests.get(f"http://{host}:{self.port}/info", timeout=5)
            if r.status_code == 200:
                return r.json()
        except Exception:  # pragma: no cover
            pass
        return None

    def _update_status_device(self, is_running: bool, status_text: str):
        try:
            if 1 in Devices:
                Devices[1].Update(nValue=1 if is_running else 0, sValue="On" if is_running else "Off")
            if 2 in Devices:
                info = {"status": status_text, "host": self.host, "port": self.port, "aiohttp_available": AIOHTTP_AVAILABLE, "mcp_sdk_available": MCP_SDK_AVAILABLE, "uptime": int(time.time() - self.server_start_time) if self.server_start_time else 0, "last_check": time.strftime("%Y-%m-%d %H:%M:%S"), "restart_attempts": self.restart_attempts, "protocol_version": "MCP 2025-06-18", "authentication": "OAuth 2.1 passthrough", "domoticz_oauth_configured": self.domoticz_oauth_client.oauth_config is not None}
                extra = self._get_server_info()
                if extra:
                    info.update(extra)
                Devices[2].Update(nValue=0, sValue=json.dumps(info, indent=2))
        except Exception as e:
            Domoticz.Error(f"Error updating status device: {e}")
