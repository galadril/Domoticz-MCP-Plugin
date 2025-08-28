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
        try:
            api_endpoint = f"{self.domoticz_base_url}/json.htm"
            headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
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
                return {"error": "OAuth token expired or invalid", "status_code": 401}
            Domoticz.Error(f"Domoticz API call failed: {r.status_code} body={r.text[:120]}")
            return {"error": f"Domoticz API call failed: {r.status_code}"}
        except Exception as e:
            Domoticz.Error(f"Domoticz OAuth API call error: {e}")
            return {"error": f"Domoticz OAuth API call error: {e}"}
