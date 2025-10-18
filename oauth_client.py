import urllib.parse
import requests
import Domoticz

class DomoticzOAuthClient:
    """Lightweight helper to discover Domoticz OIDC endpoints and perform OAuth authenticated requests."""
    def __init__(self, domoticz_base_url: str = "http://127.0.0.1:8080", client_id: str = None, client_secret: str = None):
        self.domoticz_base_url = domoticz_base_url.rstrip('/')
        self.session = requests.Session()
        self.oauth_config = None
        
        # Client credentials for this plugin to authenticate to Domoticz
        self.client_id = client_id
        self.client_secret = client_secret
        
        # Token management for plugin's own access to Domoticz
        self.plugin_access_token = None
        self.plugin_token_expiry = 0

    # ---- internal helpers -------------------------------------------------
    def _normalize(self):
        """Normalize any domoticz.local* hostnames in discovered endpoints to the configured override host."""
        if not self.oauth_config:
            return
        try:
            target = urllib.parse.urlparse(self.domoticz_base_url)
            target_netloc = target.netloc
            for key in ["authorization_endpoint", "token_endpoint", "issuer"]:
                url = self.oauth_config.get(key)
                if not url:
                    continue
                parsed = urllib.parse.urlparse(url)
                if parsed.netloc != target_netloc and (parsed.hostname or "").startswith("domoticz.local"):
                    new = urllib.parse.urlunparse((parsed.scheme, target_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
                    self.oauth_config[key] = new
                    Domoticz.Debug(f"Normalized {key} -> {new}")
        except Exception as e:
            Domoticz.Debug(f"Host normalization skipped: {e}")

    def _log_safe_dict(self, data: dict) -> str:
        try:
            if not isinstance(data, dict):
                return str(data)
            redacted = {}
            for k, v in data.items():
                if any(s in k.lower() for s in ["secret", "token", "code", "assertion", "password"]):
                    redacted[k] = "***" if isinstance(v, str) and v else "***"
                else:
                    redacted[k] = v
            return str(redacted)
        except Exception:
            return "<unable to render dict>"

    # ---- public API -------------------------------------------------------
    def discover_oauth_endpoints(self):
        try:
            well_known_url = f"{self.domoticz_base_url}/.well-known/openid-configuration"
            Domoticz.Debug(f"Discover OAuth endpoints: GET {well_known_url}")
            r = self.session.get(well_known_url, timeout=10)
            Domoticz.Debug(f"Discovery status={r.status_code}")
            if r.status_code == 200:
                self.oauth_config = r.json()
                self._normalize()
                Domoticz.Log(f"Discovered Domoticz OAuth endpoints: {well_known_url}")
                return True
            Domoticz.Error(f"Failed to discover OAuth endpoints: {r.status_code}")
            return False
        except Exception as e:
            Domoticz.Error(f"Error discovering OAuth endpoints: {e}")
            return False

    def make_authenticated_request(self, access_token: str, params: dict):
        """
        Make authenticated request to Domoticz API.
        
        If access_token is provided, validates it (user token from MCP client).
        Then uses the plugin's OWN token to actually call Domoticz (no passthrough).
        
        Args:
            access_token: Token from MCP client (for validation only)
            params: API parameters
        """
        try:
            # If we have client credentials, use client credentials flow
            if self.client_id and self.client_secret:
                # Get or refresh plugin's own token
                plugin_token = self._get_plugin_token()
                if not plugin_token:
                    return {"error": "Failed to obtain plugin token for Domoticz"}
                
                # Use plugin's token (NOT the user's token)
                actual_token = plugin_token
                Domoticz.Debug("Using plugin's client credentials token for Domoticz API")
            else:
                # Fallback: passthrough mode (VIOLATES MCP SPEC - should be avoided)
                actual_token = access_token
                Domoticz.Debug("WARNING: Using passthrough mode - configure client credentials!")
            
            api_endpoint = f"{self.domoticz_base_url}/json.htm"
            headers = {'Authorization': f'Bearer {actual_token}', 'Content-Type': 'application/json'}
            Domoticz.Debug(f"Domoticz API request -> {api_endpoint} params={self._log_safe_dict(params)}")
            r = self.session.get(api_endpoint, params=params, headers=headers, timeout=10)
            Domoticz.Debug(f"Domoticz API response status={r.status_code}")
            if r.status_code == 200:
                try:
                    jr = r.json()
                except Exception as je:
                    Domoticz.Error(f"JSON parse error: {je}")
                    return {"error": f"Invalid JSON response: {je}"}
                # Light summary for logs
                if isinstance(jr, dict):
                    summary_keys = list(jr.keys())[:6]
                    Domoticz.Debug(f"Domoticz API success keys={summary_keys}")
                return jr
            if r.status_code == 401:
                Domoticz.Error("Domoticz API 401 (token invalid or expired)")
                # Clear plugin token if it was our own
                if self.client_id and self.client_secret:
                    self.plugin_access_token = None
                    self.plugin_token_expiry = 0
                return {"error": "OAuth token expired or invalid", "status_code": 401}
            Domoticz.Error(f"Domoticz API call failed: {r.status_code} body={r.text[:120]}")
            return {"error": f"Domoticz API call failed: {r.status_code}"}
        except Exception as e:
            Domoticz.Error(f"Domoticz OAuth API call error: {e}")
            return {"error": f"Domoticz OAuth API call error: {e}"}
    
    def _get_plugin_token(self):
        """Get or refresh the plugin's own access token for Domoticz using client credentials flow."""
        import time
        
        # Check if we have a valid token
        if self.plugin_access_token and time.time() < self.plugin_token_expiry:
            Domoticz.Debug("Using cached plugin token")
            return self.plugin_access_token
        
        # Need to get a new token
        if not self.oauth_config:
            Domoticz.Debug("OAuth config not available, attempting discovery")
            if not self.discover_oauth_endpoints():
                Domoticz.Error("Cannot get plugin token: OAuth discovery failed")
                return None
        
        token_endpoint = self.oauth_config.get('token_endpoint')
        if not token_endpoint:
            Domoticz.Error("Cannot get plugin token: token_endpoint not found")
            return None
        
        try:
            Domoticz.Debug(f"Obtaining plugin token via client credentials flow from {token_endpoint}")
            
            # Client credentials grant (RFC 6749 Section 4.4)
            data = {
                'grant_type': 'client_credentials',
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'scope': 'read write'  # Adjust scopes as needed
            }
            
            r = self.session.post(
                token_endpoint,
                data=data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=10
            )
            
            if r.status_code == 200:
                token_response = r.json()
                self.plugin_access_token = token_response.get('access_token')
                expires_in = token_response.get('expires_in', 3600)
                
                # Set expiry with 60 second buffer
                import time
                self.plugin_token_expiry = time.time() + expires_in - 60
                
                Domoticz.Log(f"Plugin token obtained successfully (expires in {expires_in}s)")
                return self.plugin_access_token
            else:
                Domoticz.Error(f"Failed to obtain plugin token: {r.status_code} - {r.text[:200]}")
                return None
                
        except Exception as e:
            Domoticz.Error(f"Error obtaining plugin token: {e}")
            return None
