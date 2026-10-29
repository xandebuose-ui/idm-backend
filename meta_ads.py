"""Leitura do Meta Ads (Graph API) com o token do usuario do sistema 'Jarbas IDM'.
So leitura (ads_read). Token vem da env META_TOKEN; nunca aparece em resposta/log.
"""
import os, json, datetime as dt, urllib.request, urllib.parse

GRAPH = "https://graph.facebook.com/v21.0"
ACT = os.environ.get("META_AD_ACCOUNT", "act_371552207513376")

def _tok():
    return os.environ.get("META_TOKEN", "").strip()

def _get(path, params):
    params = dict(params); params["access_token"] = _tok()
    url = "%s/%s?%s" % (GRAPH, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"User-Agent": "idm-backend/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))

def _acoes(lst, tipo):
    for a in lst or []:
        if a.get("action_type") == tipo:
            try: return int(float(a.get("value") or 0))
            except Exception: return 0
    return 0

def insights_por_campanha(desde, ate):
    """Gasto e contatos por campanha no intervalo (datas 'YYYY-MM-DD', inclusive)."""
    j = _get("%s/insights" % ACT, {
        "level": "campaign",
        "fields": "campaign_id,campaign_name,spend,impressions,reach,actions",
        "time_range": json.dumps({"since": desde, "until": ate}),
        "limit": 100,
    })
    out = []
    for row in j.get("data", []):
        out.append({
            "campanha_id": row.get("campaign_id"),
            "campanha": row.get("campaign_name"),
            "gasto": round(float(row.get("spend") or 0), 2),
            "impressoes": int(row.get("impressions") or 0),
            "alcance": int(row.get("reach") or 0),
            "conversas_iniciadas": _acoes(row.get("actions"), "onsite_conversion.total_messaging_connection"),
            "novos_contatos": _acoes(row.get("actions"), "onsite_conversion.messaging_first_reply"),
        })
    return out

def status():
    if not _tok():
        return {"ok": False, "erro": "META_TOKEN ausente no ambiente"}
    hoje = dt.date.today()
    ontem = hoje - dt.timedelta(days=1)
    try:
        conta = _get(ACT, {"fields": "name,account_status,currency"})
        campanhas = insights_por_campanha(ontem.isoformat(), hoje.isoformat())
        return {"ok": True, "conta": conta.get("name"), "moeda": conta.get("currency"),
                "periodo": {"de": ontem.isoformat(), "ate": hoje.isoformat()},
                "gasto_total": round(sum(c["gasto"] for c in campanhas), 2),
                "campanhas": campanhas}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:600]
        try: body = json.loads(body).get("error", {})
        except Exception: pass
        if isinstance(body, dict): body.pop("fbtrace_id", None)
        return {"ok": False, "http": e.code, "erro": body}
    except Exception as e:
        return {"ok": False, "erro": str(e)[:300]}
