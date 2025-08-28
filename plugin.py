"""
<plugin key="Domoticz-MCP-Server" name="Domoticz MCP Server Plugin" author="Mark Heinis" version="2.0.0" wikilink="https://github.com/galadril/Domoticz-MCP-Service/wiki" externallink="https://github.com/galadril/Domoticz-MCP-Service">
    <description>
        Plugin for running Domoticz MCP (Model Context Protocol) Server.
        Provides AI assistant access to Domoticz functionality through MCP protocol.
        Authentication is handled via OAuth 2.1 flow - plugin acts as OAuth client to Domoticz (client/app credentials now supplied by external caller only).
    </description>
    <params>
        <param field="Mode1" label="Auto Start Server" width="75px">
            <options>
                <option label="Yes" value="true" default="true"/>
                <option label="No" value="false"/>
            </options>
        </param>
        <param field="Mode2" label="Health Check interval (seconds)" width="30px" required="true" default="30"/>
        <param field="Mode3" label="Domoticz URL Override" width="200px" required="false" default="" placeholder="Leave empty for http://127.0.0.1:8080"/>
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

# Slim wrapper that wires Domoticz plugin callbacks to refactored modules.

from base_plugin import BasePlugin

_plugin = BasePlugin()


def onStart():
    try:
        _plugin.onStart(Parameters)  # pass Domoticz Parameters dict explicitly
    except Exception as e:
        import Domoticz
        Domoticz.Error(f"Wrapper onStart failed: {e}")


def onStop():
    _plugin.onStop()


def onHeartbeat():
    _plugin.onHeartbeat()


def onCommand(Unit, Command, Level, Hue):
    _plugin.onCommand(Unit, Command, Level, Hue)
