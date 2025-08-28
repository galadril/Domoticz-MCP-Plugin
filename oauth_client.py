import urllib.parse
import requests
import Domoticz

class DomoticzOAuthClient:
    """Lightweight helper to discover Domoticz OIDC endpoints and perform OAuth authenticated requests."""
    def __init__(self, domoticz_base_url: str = "http://127.0.0.1:8080"):
        self.domoticz_base_url = domoticz_base_url.rstrip('/')
        self.session = requests.Session()
        self.oauth_config = None

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

    # ---- public API -------------------------------------------------------
    def discover_oauth_endpoints(self):
        try:
            well_known_url = f"{self.domoticz_base_url}/.well-known/openid-configuration"
            r = self.session.get(well_known_url, timeout=10)
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
        try:
            api_endpoint = f"{self.domoticz_base_url}/json.htm"
            headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
            r = self.session.get(api_endpoint, params=params, headers=headers, timeout=10)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                return {"error": "OAuth token expired or invalid", "status_code": 401}
            return {"error": f"Domoticz API call failed: {r.status_code}"}
        except Exception as e:
            return {"error": f"Domoticz OAuth API call error: {e}"}
