"""Painel VIVO do marketing (06/11/2026) — mesmo modelo do relatório v3 (PADRAO-RELATORIO-TRAFEGO.md), calculado
na hora a partir do banco (escuta_leads, escuta_crm, escuta_vendas), agenda (API oficial) e Meta (Graph).
So leitura. Correcoes manuais de origem/exclusao vivem em mkt_origem_override (so gestao grava).
Regra A (02/11): venda do periodo de lead de qualquer safra conta; 'Vendas a clientes novos (LEADS)'.
"""
import re, time, json, datetime as dt
import firebase_writer as _fw
import motor_cruzamento as _cruz
import motor_fila as mf

COLL_LEADS, COLL_CRM, COLL_VENDAS, COLL_OVR = "escuta_leads", "escuta_crm", "escuta_vendas", "mkt_origem_override"
WA_CAP = ("CAM 04", "CAM 06", "CAM 07", "CAM 08")   # captacao por WhatsApp; CAM 09 = reativacao
CONTRATO = 4000.0
BUCKETS = [("trafego", "Tráfego pago — captação"), ("reativacao", "Tráfego — Reativação"), ("mkt", "Orgânico"),
           ("rel", "Indicação e relacionamento"), ("nao", "Origem não informada")]
CANAIS = ["Bio", "Direct/Instagram", "Site", "Google", "IA (ChatGPT)"]
_CACHE = {}

def _cache(k, ttl, fn):
    c = _CACHE.get(k)
    if c and time.time() - c[0] < ttl:
        return c[1]
    v = fn(); _CACHE[k] = (time.time(), v); return v

def _sp(iso):
    if not iso: return None
    try:
        x = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")[:19])
        return x - dt.timedelta(hours=3) if ("Z" in str(iso) or "+00" in str(iso)) else x
    except Exception:
        return None

def _camp(nome):
    m = re.search(r"CAM\s*0?(\d+)", nome or "")
    return ("CAM 0%s" % m.group(1)) if m else "OUTRAS"

# ------------------------------------------------------------------ fontes
def _leads_db():
    db = _fw._get_db()
    return [dict(d.to_dict() or {}, _id=d.id) for d in db.collection(COLL_LEADS).stream()]

def _crm_db():
    out = {}
    for d in _fw._get_db().collection(COLL_CRM).stream():
        o = d.to_dict() or {}
        if o.get("fone"):
            out.setdefault(o["fone"], []).append(o)
    return out

def _vendas_db():
    out = {}
    for d in _fw._get_db().collection(COLL_VENDAS).stream():
        v = d.to_dict() or {}
        if v.get("fone"):
            out.setdefault(v["fone"], []).append(v)
    return out

def _ovr_db():
    return {d.id: (d.to_dict() or {}) for d in _fw._get_db().collection(COLL_OVR).stream()}

def _bookings(de, ate):
    def f():
        ini = "%sT00:00:00-03:00" % de
        fim = (dt.date.fromisoformat(ate) + dt.timedelta(days=60)).isoformat() + "T23:59:59-03:00"
        bs, pg = [], 1
        while pg <= 80:
            d = mf.api("bookings?starts_at=%s&ends_at=%s&per_page=100&page=%d" % (ini, fim, pg)) or {}
            data = d.get("data") or []; meta = d.get("meta") or {}
            bs += data
            tot = meta.get("of") or 0; last = meta.get("last_page")
            if not data or (tot and len(bs) >= tot) or (last and pg >= last): break
            pg += 1
        out = {}
        for b in bs:
            p = b.get("patient") or {}
            fo = _cruz._fone(p.get("phone") if not isinstance(p.get("phone"), dict) else p["phone"].get("number"))
            if not fo: continue
            procs = b.get("procedures") or []
            nome = procs[0].get("name", "") if procs and isinstance(procs[0], dict) else ""
            out.setdefault(fo, []).append({"quando": str(b.get("starts_at"))[:16], "status": str(b.get("status") or ""), "proc": nome,
                                          "criado": str(b.get("created_at") or "")[:16]})
        return out
    return _cache("bk:%s:%s" % (de, ate), 600, f)

def _meta(de, ate):
    import meta_ads as _m
    if not _m._tok():
        return {"ok": False, "dias": [], "erro": "META_TOKEN ausente"}
    try:
        return _cache("meta:%s:%s" % (de, ate), 1800, lambda: {"ok": True, "dias": _m.insights_por_dia(de, ate),
                                                                "camp": _m.insights_por_campanha(de, ate)})
    except Exception as e:
        return {"ok": False, "dias": [], "erro": str(e)[:120]}

# ------------------------------------------------------------------ classificacao (mesmas regras do relatorio manual)
_REAT = re.compile(r"quero entender se meu p[oó]s", re.I)
_RECLIQUE = re.compile(r"vi o an[uú]ncio|quais s[aã]o os procedimentos de p[oó]s", re.I)
_DECL = {"instagram": ("mkt", "Direct/Instagram"), "google": ("mkt", "Google"), "site": ("mkt", "Site"),
         "indicacao_paciente": ("rel", None), "indicacao_cirurgiao": ("rel", None), "indicacao_outro": ("rel", None),
         "anuncio": ("trafego", None)}
_COD = {"trafego_pago": ("trafego", None), "organico_bio": ("mkt", "Bio"), "organico_site": ("mkt", "Site"),
        "organico_google": ("mkt", "Google"), "organico_instagram": ("mkt", "Direct/Instagram"),
        "relacionamento_indicacao": ("rel", None)}
_FORA = ("fornecedor_parceiro", "teste_interno", "equipe_interna", "spam")

def classifica(l, ovr):
    """-> (bucket, canal, prova) ou (None, motivo, None) quando nao e lead."""
    o = ovr.get(l.get("fone")) or {}
    if o.get("nao_lead"):
        return None, "marcado 'não é lead' pela gestão", None
    ia = l.get("ia") or {}
    if o.get("bucket"):
        return o["bucket"], o.get("canal"), (o.get("prova") or "informada pela gestão") + " (correção manual)"
    if l.get("interno") or ia.get("tipo_contato") in _FORA:
        return None, "fora da conta (%s)" % (ia.get("tipo_contato") or "interno"), None
    txt1 = l.get("primeira_msg_lead") or ""
    if _REAT.search(txt1):
        return "reativacao", None, "anúncio de reativação (1ª mensagem)"
    cod = l.get("origem_codigo") or "nao_informada"
    if cod in _COD:
        b, c = _COD[cod]
        return b, c, ("mensagem do anúncio" if b == "trafego" else "1ª mensagem")
    d = ia.get("origem_declarada")
    if d in _DECL:
        b, c = _DECL[d]
        return b, c, "declarada na conversa (%s)" % d
    return "nao", None, "não informada"

def _reclique(fo, de, ate):
    """Lead antigo que mandou de novo a frase do anuncio dentro do periodo -> (data, True)."""
    def f():
        ref = _fw._get_db().collection("escuta_conversas").document(fo)
        meses = sorted({de[:7], ate[:7]})
        for mes in meses:
            for d in ref.collection("msgs_" + mes).stream():
                m = d.to_dict() or {}
                if m.get("de") != "lead":
                    continue
                q = _sp(m.get("data"))
                if not q: continue
                dia = q.date().isoformat()
                if de <= dia <= ate and _RECLIQUE.search(m.get("texto") or ""):
                    return dia
        return None
    return _cache("rc:%s:%s:%s" % (fo, de, ate), 600, f)

def _data_ganho(v):
    if v.get("status") == "won" and v.get("atualizado_em_ce"):
        x = _sp(v["atualizado_em_ce"])
        if x: return x.date().isoformat()
    return v.get("data") or ""

# ------------------------------------------------------------------ montagem
def montar(de, ate, versao="gestao"):
    leads, crm, vendas, ovr = _leads_db(), _crm_db(), _vendas_db(), _ovr_db()
    bks = _bookings(de, ate)
    lim = ate + "T23:59"
    L, fora = [], []
    for l in leads:
        ch = l.get("data_chegada_sp")
        fo = l.get("fone")
        reclique = False
        if ch and ch < de and (l.get("origem_codigo") == "trafego_pago") and (_sp(l.get("ultima_msg_em")) or dt.datetime(2000, 1, 1)).date().isoformat() >= de \
                and not any((v.get("status") == "won") for v in (vendas.get(fo) or [])):
            rc = _reclique(fo, de, ate)
            if rc:
                ch, reclique = rc, True
        if not ch or not (de <= ch <= ate):
            continue
        ops = crm.get(fo) or []
        vs = vendas.get(fo) or []
        ia = l.get("ia") or {}
        # cliente antigo (comprou antes de chegar) = base, fora da contagem de leads
        ganhou_antes = any(v.get("status") == "won" and _data_ganho(v) < (l.get("data_chegada_sp") or ch) for v in vs)
        opp_lead = [o for o in ops if (o.get("pipeline") or "").upper().startswith(("LEADS", "REATIV"))]
        o_ = ovr.get(fo) or {}
        if (ganhou_antes or (_cruz._eh_comprador(fo) and not opp_lead)) and not o_.get("bucket"):
            fora.append({"nome": l.get("nome"), "motivo": "cliente da base"}); continue
        if not l.get("primeira_msg_lead") and not l.get("external_id") and not o_.get("bucket"):
            fora.append({"nome": l.get("nome"), "motivo": "sem conteúdo legível (mensagem que o sistema não exibe)"}); continue
        b, canal, prova = classifica(l, ovr)
        if reclique and not o_.get("bucket"):
            b, canal, prova = "trafego", None, "reclique (lead antigo que voltou pelo anúncio)"
        if b == "nao" and not l.get("primeira_msg_lead") and l.get("external_id"):
            prova = "chegou por fora (conversa começa com agendamento da equipe) — origem a descobrir"
        if b is None:
            fora.append({"nome": l.get("nome"), "motivo": canal}); continue
        bk = [x for x in bks.get(fo, []) if x["quando"][:10] >= ch and x["criado"] <= lim]
        bk_fe = [x for x in bk if x["quando"][:16] <= lim and x["status"].lower() == "done"]
        on = any("on-line" in x["proc"].lower() for x in bk_fe); pr = any("on-line" not in x["proc"].lower() for x in bk_fe)
        q = [v for v in vs if (v.get("data") or "") >= ch and (v.get("data") or "") <= ate]
        won = [v for v in vs if v.get("status") == "won" and ch <= _data_ganho(v) <= ate and (v.get("data") or "") >= (l.get("data_chegada_sp") or ch)]; opn = [v for v in q if v.get("status") in ("open", "due")]
        porta = None
        if bk:
            porta = "on" if "on-line" in sorted(bk, key=lambda x: x["quando"])[0]["proc"].lower() else "pr"
        L.append({"fone": fo, "nome": l.get("nome"), "chegada": ch, "bucket": b, "canal": canal, "prova": prova,
                  "campanha": l.get("campanha_tintim"), "opp": bool(opp_lead or ops), "etapa": (l.get("crm") or {}).get("estagio"),
                  "dono": (l.get("crm") or {}).get("vendedor"), "ag": bool(bk), "porta": porta, "at": bool(bk_fe), "on": on, "pr": pr,
                  "orc": len(q), "orcv": sum(int(v.get("valor_centavos") or 0) for v in q) / 100.0,
                  "won": len(won), "wonv": sum(int(v.get("valor_centavos") or 0) for v in won) / 100.0,
                  "opnv": sum(int(v.get("valor_centavos") or 0) for v in opn) / 100.0,
                  "prox": [x["quando"] + " " + x["proc"] for x in bk if x["quando"][:16] > lim][:1],
                  "resumo": (ia.get("resumo") or "")[:400], "furos": l.get("furos") or [],
                  "primeira_resp_min": (l.get("conduta") or {}).get("primeira_resposta_min"),
                  "respondeu": (l.get("msgs_lead") or 0) > 1})
    fones_L = {x["fone"] for x in L}
    # Regra A: vendas no periodo a leads de safras anteriores (1a compra)
    antigos = []
    for fo, vs in vendas.items():
        if fo in fones_L: continue
        w = [v for v in vs if v.get("status") == "won" and de <= _data_ganho(v) <= ate]
        if not w: continue
        if any(v.get("status") == "won" and _data_ganho(v) < de for v in vs): continue
        ops = crm.get(fo) or []
        if not any((o.get("pipeline") or "").upper().startswith(("LEADS", "REATIV")) for o in ops): continue
        antigos.append({"nome": w[0].get("comprador"), "valor": sum(int(v.get("valor_centavos") or 0) for v in w) / 100.0})
    meta = _meta(de, ate)
    return {"ok": True, "de": de, "ate": ate, "versao": versao, "gerado": (dt.datetime.utcnow() - dt.timedelta(hours=3)).isoformat()[:16],
            "leads": L, "fora": fora[:200], "antigos": antigos, "meta": meta, "contrato": CONTRATO,
            "buckets": BUCKETS, "canais": CANAIS, "wa_cap": list(WA_CAP)}


def debug(fo, de, ate):
    db = _fw._get_db()
    d = db.collection(COLL_LEADS).document(fo).get()
    l = d.to_dict() if d.exists else {}
    ref = db.collection("escuta_conversas").document(fo)
    msgs = []
    for mes in sorted({de[:7], ate[:7]}):
        for x in ref.collection("msgs_" + mes).stream():
            m = x.to_dict() or {}
            msgs.append([str(m.get("data"))[:19], m.get("de"), m.get("tipo"), (m.get("texto") or "")[:60]])
    msgs.sort()
    keep = {k: l.get(k) for k in ("fone", "nome", "external_id", "primeira_chegada", "data_chegada_sp", "ultima_msg_em", "origem_codigo",
                                   "primeira_msg_lead", "msgs_lead", "identidade", "interno")}
    keep["ia_tipo"] = (l.get("ia") or {}).get("tipo_contato"); keep["ia_origem"] = (l.get("ia") or {}).get("origem_declarada")
    return {"lead": keep, "comprador": _cruz._eh_comprador(fo), "msgs": msgs[:12], "n_msgs": len(msgs),
            "reclique": _reclique(fo, de, ate)}
