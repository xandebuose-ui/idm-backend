# -*- coding: utf-8 -*-
"""
IDM - ETIQUETAS AUTOMATICAS na CliniChat (30/10/2026, aprovado pelo Xandao).
So LEADS (nunca paciente). So ADICIONA (nunca remove). So contato com cadastro no CE (externalId).
  - "Lead Pos Operatorio": conversa sobre pos-op corporal/facial ou pre-op (leitura da ficha viva)
  - "Trafego Pago Meta":   lead que chegou por anuncio (1a mensagem / TimTim)
Mesma rota do app do CE (acao em lote): PATCH api/person/tags {batch_mode:add}.
Roda a cada 15 min, das 06h00 as 23h59. Log por dia em escuta_etiquetas/{AAAA-MM-DD}.
"""
import os, json, time, datetime as dt
import urllib.request as _ur, urllib.error as _ue

import firebase_writer as _fw
import motor_cruzamento as _cruz

TAGS = {"lead_pos_op": {"uuid": "0aee1e4e-46f4-4ba2-97c4-1bf469192580", "label": "Lead Pós Operatório"},
        "trafego_meta": {"uuid": "0eb71244-31a9-40c7-b8c1-423bca594e30", "label": "Trafego Pago Meta"}}
POS = {"pos_op_corporal", "pos_op_facial", "pre_operatorio"}
FORA = {"fornecedor_parceiro", "teste_interno", "equipe_interna", "spam", "sem_telefone"}
ST = {"ultima": None, "ok": None, "erro": None, "aplicadas": {}, "plano": {}, "falhas": 0}
_FEITO = set()   # (externalId, tag) ja aplicados neste processo (evita repetir se a lista do CE demorar a refletir)

def _sp_now():
    return dt.datetime.utcnow() - dt.timedelta(hours=3)

def _patch_tags(person_ids, tag_uuid):
    app = _cruz._app_jwt()
    if not app:
        return False, "sem app_jwt"
    body = json.dumps({"batch_mode": "add", "tags": [tag_uuid], "person_ids": [str(p) for p in person_ids]}).encode()
    rq = _ur.Request(_cruz.INTERNAL + "/api/person/tags", data=body, method="PATCH", headers={
        "Authorization": "Bearer " + app, "User-Agent": _cruz.mf.UA, "Accept": "application/json",
        "Content-Type": "application/json"})
    try:
        with _ur.urlopen(rq, timeout=60) as r:
            return r.status in (200, 201, 204), None
    except _ue.HTTPError as e:
        return False, "http %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        return False, repr(e)[:160]

def plano():
    """O que falta etiquetar agora: lista por etiqueta."""
    db = _fw._get_db()
    fichas = {d.id: (d.to_dict() or {}) for d in db.collection("escuta_leads").stream()}
    manual = {d.id: (d.to_dict() or {}).get("tipo") for d in db.collection("escuta_tipo_manual").stream()}
    import fonte as _fonte
    convs = _fonte.indice() or _cruz._lista(None, cap=3000)
    out = {k: [] for k in TAGS}
    vistos = set()
    for c in convs:
        ct = c.get("contact") or {}
        ext = ct.get("externalId")
        f = _cruz._fone(ct.get("phone") or c.get("identifier"))
        if not ext or not f or f in vistos or ct.get("type") == "professional":
            continue
        vistos.add(f)
        fx = fichas.get(f) or {}
        tipo = manual.get(f) or fx.get("tipo_contato")
        ident = fx.get("identidade") or _cruz.classifica_identidade(f, ct.get("name"))
        if ident == "cliente" or tipo in FORA or fx.get("interno") or ct.get("type") == "patient" and ident == "cliente":
            continue
        tem = {str(t.get("uuid")) for t in (ct.get("tags") or []) if isinstance(t, dict)}
        serv = set(((fx.get("ia") or {}).get("servicos")) or [])
        quer = []
        if serv & POS:
            quer.append("lead_pos_op")
        if str(fx.get("origem_codigo") or "").startswith("trafego"):
            quer.append("trafego_meta")
        for k in quer:
            if TAGS[k]["uuid"] not in tem and (str(ext), k) not in _FEITO:
                out[k].append({"ext": str(ext), "fone": f, "nome": ct.get("name")})
    return out

def rodar(aplicar=True, motivo="poll"):
    if motivo == "poll" and _sp_now().hour < 6:
        return {"ok": True, "skip": "pausa 00h-06h"}
    try:
        p = plano()
        ST["plano"] = {k: len(v) for k, v in p.items()}
        if not aplicar:
            return {"ok": True, "simulacao": True, "plano": {k: [x["nome"] for x in v] for k, v in p.items()}}
        db = _fw._get_db()
        dia = _sp_now().date().isoformat()
        log = []
        aplicadas = {}
        for k, itens in p.items():
            for i in range(0, len(itens), 50):
                lote = itens[i:i + 50]
                ok, err = _patch_tags([x["ext"] for x in lote], TAGS[k]["uuid"])
                if ok:
                    aplicadas[k] = aplicadas.get(k, 0) + len(lote)
                    for x in lote:
                        _FEITO.add((x["ext"], k))
                        log.append({"fone": x["fone"], "nome": x["nome"], "etiqueta": TAGS[k]["label"],
                                    "em": _sp_now().isoformat(timespec="seconds")})
                else:
                    ST["falhas"] += 1
                    ST["erro"] = err
                time.sleep(0.5)
        if log:
            ref = db.collection("escuta_etiquetas").document(dia)
            s = ref.get()
            ant = (s.to_dict() or {}).get("itens", []) if s.exists else []
            tot = dict((s.to_dict() or {}).get("por_etiqueta", {})) if s.exists else {}
            for x in log:
                tot[x["etiqueta"]] = tot.get(x["etiqueta"], 0) + 1
            ref.set({"dia": dia, "itens": (ant + log)[-500:], "por_etiqueta": tot, "atualizado_em": _sp_now().isoformat(timespec="seconds")})
        ST.update({"ultima": _sp_now().isoformat(timespec="seconds"), "ok": True, "aplicadas": aplicadas})
        if log:
            print("[etiquetas] aplicadas %s" % aplicadas, flush=True)
        return {"ok": True, "aplicadas": aplicadas, "erro": ST.get("erro") if ST["falhas"] else None}
    except Exception as e:
        ST.update({"ok": False, "erro": repr(e)[:200]})
        print("[etiquetas] erro:", repr(e), flush=True)
        return {"ok": False, "erro": repr(e)[:200]}

def iniciar(sched):
    if sched is None or os.environ.get("ETIQUETAS_ON", "1") == "0":
        return
    sched.add_job(lambda: rodar(True, "poll"), "interval", minutes=15)
    print("[etiquetas] agendado a cada 15 min (06h-23h59)", flush=True)
