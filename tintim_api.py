"""API publica do TimTim (token DA CONTA do Instituto, env TINTIM_TOKEN). So leitura aqui.
Doc: s.tintim.app/api/v1/{ACCOUNT_CODE}/... ?token=  (lead/{PHONE}, leadstatus). Token nunca sai em resposta/log.
"""
import os, json, urllib.request, urllib.parse

BASE = "https://s.tintim.app/api/v1"
ACCOUNT = os.environ.get("TINTIM_ACCOUNT_CODE", "69d40573-a16a-4221-9bdc-5b44145efbde")

def _tok():
    return os.environ.get("TINTIM_TOKEN", "").strip()

def _get(path):
    url = "%s/%s/%s?token=%s" % (BASE, ACCOUNT, path, urllib.parse.quote(_tok()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 idm-backend", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def lead(phone_digits):
    return _get("lead/%s" % phone_digits)

def leadstatus():
    return _get("leadstatus")

def status():
    if not _tok():
        return {"ok": False, "erro": "TINTIM_TOKEN ausente no ambiente"}
    try:
        st = leadstatus()
        etapas = st if isinstance(st, list) else (st.get("data") or st.get("results") or st)
        return {"ok": True, "conta": ACCOUNT[:8] + "...", "etapas": etapas}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "erro": e.read().decode("utf-8", "ignore")[:300]}
    except Exception as e:
        return {"ok": False, "erro": str(e)[:300]}
