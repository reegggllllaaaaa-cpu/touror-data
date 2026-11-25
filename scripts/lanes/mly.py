"""Mapillary API helper. Token read from file only; never printed/logged."""
import json, os, time, urllib.request, urllib.parse, urllib.error
_TOK = None
def _tok():
    global _TOK
    if _TOK is None:
        p = os.path.join(os.environ["USERPROFILE"], ".claude", "secrets", "mapillary-token.txt")
        _TOK = open(p, encoding="utf-8").read().strip()
    return _TOK
_last = [0.0]
def _throttle(dt=0.12):
    w = _last[0] + dt - time.time()
    if w > 0: time.sleep(w)
    _last[0] = time.time()
def graph(path, **params):
    q = urllib.parse.urlencode(params)
    url = "https://graph.mapillary.com/" + path.lstrip("/") + ("?" + q if q else "")
    for attempt in range(4):
        _throttle()
        req = urllib.request.Request(url, headers={"Authorization": "OAuth " + _tok()})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace").replace(_tok(), "***")
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 2); continue
            raise RuntimeError(f"HTTP {e.code} on {path}: {body}") from None
        except Exception as e:
            if attempt < 3: time.sleep(2); continue
            raise RuntimeError(f"net error on {path}: {type(e).__name__}") from None
def tile(layer_url_tmpl, z, x, y):
    """tiles.mapillary.com requires access_token query param."""
    url = layer_url_tmpl.format(z=z, x=x, y=y) + "?access_token=" + urllib.parse.quote(_tok())
    for attempt in range(4):
        _throttle()
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404: return b""
            if attempt < 3: time.sleep(2 ** attempt * 2); continue
            raise RuntimeError(f"tile HTTP {e.code} {z}/{x}/{y}") from None
        except Exception as e:
            if attempt < 3: time.sleep(2); continue
            raise RuntimeError(f"tile net error {z}/{x}/{y}: {type(e).__name__}") from None
def fetch_bytes(url):
    """for signed CDN thumb urls returned by API (no token added)."""
    _throttle()
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()
