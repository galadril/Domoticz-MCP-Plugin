#!/usr/bin/env python3
"""
Start script for Domoticz MCP Server
Simple startup script for per-request authentication
"""
import asyncio
import os
from domoticz_mcp import main

def get_server_config():
    """Get server configuration from environment variables or defaults"""
    # Server settings
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "8765"))
    
    return {
        'mcp_api_keys': None,       # No API keys needed for per-request auth
        'mcp_users': None,          # No pre-configured users
        'mcp_jwt_secret': None,     # No JWT needed for per-request auth
        'require_auth': False,      # Per-request authentication
        'host': host,
        'port': port
    }

def print_server_info(config):
    """Print server startup information"""
    print("\n" + "="*60)
    print("     ?? Domoticz MCP Server v3.0")
    print("     Per-Request Authentication")
    print("="*60)
    print()
    print("?? SECURITY MODEL: Per-request Domoticz credentials")
    print("?? NO server-side user management required")
    print("?? Each request includes its own Domoticz credentials")
    print()
    print(f"?? Server URL: http://{config['host']}:{config['port']}")
    print(f"?? MCP endpoint: POST http://{config['host']}:{config['port']}/mcp")
    print(f"??  Health check: GET http://{config['host']}:{config['port']}/health")
    print()
    print("?? USAGE:")
    print("   - Include domoticz_url, domoticz_username, domoticz_password in MCP tool arguments")
    print("   - No server-side authentication required")
    print("   - Each request is independent and stateless")
    print()
    print("?? TESTING:")
    print("   Test with: python test_mcp_service.py")

if __name__ == "__main__":
    print("?? Domoticz MCP Server - Per-Request Authentication")
    print("=" * 60)
    print("?? Stateless server - credentials provided per request")
    print("?? Clean startup - no user configuration needed")
    print()
    
    # Get configuration (simple - just host/port)
    config = get_server_config()
    
    # Print server information
    print_server_info(config)
    
    print("\n?? Starting server...")
    print("The server will run until you stop it with Ctrl+C.")
    print()
    
    try:
        asyncio.run(main(**config))
    except KeyboardInterrupt:
        print("\n?? Server stopped by user")
    except Exception as e:
        print(f"\n?? Server error: {e}")
        input("Press Enter to exit...")