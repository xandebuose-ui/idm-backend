# -*- coding: utf-8 -*-
"""
IDM - DIARIO 06h45 (passo 3 da SPEC-Coletor-Unico, 30/10/2026). ADITIVO. MODO PREVIA.

Gera o diario do DIA ANTERIOR lendo SO do banco (escuta_leads, escuta_crm, escuta_vendas, escuta_meta,
escuta_termometro, /saude) e grava escuta_diario/{AAAA-MM-DD}. NAO ENVIA NADA por WhatsApp: o envio pelo
CliniChat so liga depois que o Xandao aprovar o formato (DIARIO_ENVIO=1, a construir).
Terminologia: nunca "Pago" sozinho -> "Trafego pago · campanha".
"""
import os, json, datetime as dt
from collections import Counter

import firebase_writer as _fw

COLL = os.environ.get("DIARIO_COLL", "escuta_diario")
DIAS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

def _sp_now():
    return dt.datetime.utcnow() - dt.timedelta(hours=3)

def _dia_ce(v):
    """Data (AAAA-MM-DD, horario de SP) de um timestamp do CE/banco."""
    if not v:
        return None
    s = str(v)
    if "T" not in s and "Z" not in s and "+" not in s[10:]:
        return s[:10]                       # sem fuso = ja e horario local
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00")[:26] if "." in s else s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return (d.astimezone(dt.timezone.utc) - dt.timedelta(hours=3)).date().isoformat()
    except Exception:
        return s[:10]

def _brl(cent):
    v = (cent or 0) / 100.0
    return "R$ " + ("{:,.2f}".format(v)).replace(",", "X").replace(".", ",").replace("X", ".")

def _brl_reais(v):
    return "R$ " + ("{:,.2f}".format(v or 0)).replace(",", "X").replace(".", ",").replace("X", ".")

def gerar(dia=None, gravar=True):
    db = _fw._get_db()
    if not db:
        return {"ok": False, "erro": "firestore indisponivel"}
    d = dia or (_sp_now().date() - dt.timedelta(days=1)).isoformat()
    dd = dt.date.fromisoformat(d)
    todos = [x.to_dict() or {} for x in db.collection("escuta_leads").stream()]
    FORA = ("fornecedor_parceiro", "teste_interno", "equipe_interna", "spam", "sem_telefone")
    fora = [l for l in todos if l.get("tipo_contato") in FORA]
    leads = [l for l in todos if l.get("tipo_contato") not in FORA]   # fornecedor/teste/equipe/spam fora das contagens
    crm = [x.to_dict() or {} for x in db.collection(os.environ.get("ESCUTA_CRM_COLL", "escuta_crm")).stream() if x.id != "mapa"]
    vendas = [x.to_dict() or {} for x in db.collection("escuta_vendas").stream()]
    meta = (db.collection("escuta_meta").document(d).get().to_dict() or {}) if True else {}
    term = (db.collection(os.environ.get("TERM_COLL", "escuta_termometro")).document("atual").get().to_dict() or {})

    # ---- chegadas do dia
    # chegada = conversa nova em que a PESSOA mandou mensagem (conversa so com automatica nao conta)
    cheg = [l for l in leads if l.get("data_chegada_sp") == d and l.get("identidade") != "sem_telefone" and (l.get("msgs_lead") or 0) > 0]
    novos = [l for l in cheg if l.get("identidade") != "cliente"]
    clientes = [l for l in cheg if l.get("identidade") == "cliente"]
    def _orig(l):
        o = l.get("origem") or "Origem não informada"
        return "Origem não informada" if ("<" in o or "parameter" in o or len(o) > 80) else o
    por_origem = Counter(_orig(l) for l in novos)
    trafego = sum(n for o, n in por_origem.items() if str(o).startswith("Tráfego pago"))

    # ---- meta
    gasto = float(meta.get("gasto_total") or 0)
    conv_meta = int(meta.get("conversas_iniciadas_total") or 0)
    cpl = (gasto / trafego) if trafego else None

    # ---- CRM
    criadas = [o for o in crm if _dia_ce(o.get("criado_em_ce")) == d]
    mexidas = [o for o in crm if _dia_ce(o.get("atualizado_em_ce")) == d and o not in criadas]
    et_criadas = Counter((o.get("estagio") or "?") for o in criadas)

    # ---- orcamentos / vendas
    orc_d = [v for v in vendas if _dia_ce(v.get("data")) == d]
    ganhos_d = [v for v in vendas if v.get("status") == "won" and (_dia_ce(v.get("atualizado_em_ce")) == d or _dia_ce(v.get("data")) == d)]
    itens = Counter(i.get("nome") for v in ganhos_d for i in (v.get("itens") or []) if isinstance(i, dict) and i.get("nome"))

    # ---- furos (estado AGORA, para cobrar hoje)
    ativos = [l for l in leads if l.get("identidade") not in ("cliente", "sem_telefone") and l.get("situacao_crm") != "perdido"]
    sem_resp_all = [l for l in leads if "lead_sem_resposta" in (l.get("furos") or []) and l.get("identidade") != "cliente"]
    sem_resp = sorted([l for l in sem_resp_all if (l.get("horas_desde_ultima") or 0) <= 48], key=lambda l: -(l.get("horas_desde_ultima") or 0))
    sem_resp_velhos = [l for l in sem_resp_all if (l.get("horas_desde_ultima") or 0) > 48]
    sem_resp_cli = [l for l in leads if "lead_sem_resposta" in (l.get("furos") or []) and l.get("identidade") == "cliente"]
    sem_opp = [l for l in novos if "sem_oportunidade_no_crm" in (l.get("furos") or [])]
    cidade = [l for l in ativos if "cidade_nao_perguntada" in (l.get("furos") or [])]
    origem_np = [l for l in ativos if "origem_nao_perguntada" in (l.get("furos") or [])]
    sem_tarefa = sorted([l for l in ativos if "oportunidade_sem_tarefa" in (l.get("furos") or [])],
                        key=lambda l: str(l.get("ultima_msg_em") or ""), reverse=True)
    # nota de abordagem por atendente (conversas com movimento no dia)
    from collections import defaultdict as _dd
    _nt = _dd(list)
    for l in leads:
        if l.get("nota") is not None and str(l.get("ultima_msg_em") or "")[:10] and \
           _dia_ce(l.get("ultima_msg_em")) == d and (l.get("conduta") or {}).get("atendente"):
            _nt[(l["conduta"]["atendente"]).split()[0]].append(float(l["nota"]))
    notas = sorted(((a, round(sum(v) / len(v), 1), len(v)) for a, v in _nt.items()), key=lambda x: -x[2])
    t1 = [l["conduta"]["primeira_resposta_min"] for l in cheg if (l.get("conduta") or {}).get("primeira_resposta_min") is not None]
    t1_med = sorted(t1)[len(t1) // 2] if t1 else None
    lentos = sum(1 for x in t1 if x > 60)

    def nomes(arr, n=5, horas=False):
        out = []
        for l in arr[:n]:
            nm = (l.get("nome") or l.get("fone") or "?").strip()
            out.append(nm + (" (%dh)" % int(l.get("horas_desde_ultima") or 0) if horas else ""))
        return ", ".join(out) + (" +%d" % (len(arr) - n) if len(arr) > n else "")

    # ---- saude
    try:
        import coletor as _c
        sd = _c.saude()
        alertas = sd.get("alertas") or []
    except Exception as e:
        alertas = ["saude indisponivel: %s" % str(e)[:60]]

    try:                                    # 30/10 passo 4: aviso do cracha da CliniChat
        _ti = _fw.token_info()
        _hh = _ti.get("expira_em_horas")
        if _ti.get("vencido"):
            alertas.insert(0, "CRACHÁ DA CLINICHAT VENCIDO: renove já (nada novo entra no sistema)")
        elif _hh is not None and _hh <= 72:
            alertas.insert(0, "crachá da CliniChat vence em %d h: renove" % int(_hh))
    except Exception:
        pass
    c = (term.get("contagem") or {})
    L = []
    L.append("*IDM · Diário comercial · %s %s*" % (DIAS_PT[dd.weekday()], dd.strftime("%d/%m")))
    L.append("_gerado %s_" % _sp_now().strftime("%d/%m %H:%M"))
    L.append("")
    L.append("*📥 Chegadas:* %d conversas novas" % len(cheg))
    L.append("• Leads: *%d*" % len(novos) + ("" if not novos else ": " + " · ".join("%s: %d" % (o, n) for o, n in por_origem.most_common())))
    L.append("• Clientes que voltaram a falar: %d" % len(clientes))
    if meta:
        L.append("*📣 Meta Ads (%s):* gasto %s · conversas iniciadas %d" % (dd.strftime("%d/%m"), _brl_reais(gasto), conv_meta) +
                 (" · custo por lead (1ª msg de tráfego) %s" % _brl_reais(cpl) if cpl else (" · nenhum lead de tráfego pago identificado pela 1ª msg" if gasto else "")))
    else:
        L.append("*📣 Meta Ads:* sem dado do dia (coletor ainda não gravou)")
    _pc = Counter((l.get("campanha_tintim") or "campanha não identificada") for l in novos if str(l.get("origem_codigo") or "").startswith("trafego"))
    if _pc:
        L.append("• Tráfego pago por campanha: " + " · ".join("%s: %d" % (k, v) for k, v in _pc.most_common()))
    if meta:
        L.append("• Meta contou %d conversas · chegaram pelo anúncio: %d · sem marca do anúncio: %d" % (conv_meta, trafego, max(conv_meta - trafego, 0)))
    L.append("*📊 CRM:* oportunidades abertas: %d" % len(criadas) +
             ("" if not criadas else " (" + ", ".join("%s %d" % (e, n) for e, n in et_criadas.most_common()) + ")") +
             " · movimentadas: %d" % len(mexidas))
    L.append("*🧾 Orçamentos:* %d gerados (%s) · *vendas: %d (%s)*" % (len(orc_d), _brl(sum(int(v.get("valor_centavos") or 0) for v in orc_d)),
             len(ganhos_d), _brl(sum(int(v.get("valor_centavos") or 0) for v in ganhos_d))) +
             ("" if not itens else ": " + ", ".join("%s x%d" % (i, n) for i, n in itens.most_common(4))))
    L.append("")
    _at = list(term.get("atencao") or [])
    if _at:
        L.append("*🚨 Atenção agora (%d)*" % len(_at))
        for _a in _at[:8]:
            L.append("• %s: %s (%s)" % ((_a.get("nome") or "?").strip(), _a.get("problema"), str(_a.get("dono") or "sem dono").split()[0]))
        if len(_at) > 8:
            L.append("• e mais %d no Painel de Leads" % (len(_at) - 8))
        L.append("")
    L.append("*🚨 Furos para cobrar hoje*")
    L.append("• Leads esperando resposta (até 48h, agir hoje): *%d*" % len(sem_resp) + (": " + nomes(sem_resp, 6, True) if sem_resp else ""))
    if sem_resp_velhos:
        L.append("• Leads sem resposta há mais de 48h (esfriando): %d" % len(sem_resp_velhos))
    if sem_resp_cli:
        L.append("• Clientes esperando resposta: %d" % len(sem_resp_cli))
    L.append("• Leads novos de ontem sem oportunidade no CRM: *%d*" % len(sem_opp) + (": " + nomes(sem_opp) if sem_opp else ""))
    cid_d = [l for l in cidade if l.get("data_chegada_sp") == d]
    ori_d = [l for l in origem_np if l.get("data_chegada_sp") == d]
    L.append("• Nas chegadas de ontem: cidade/bairro não perguntados %d · origem não perguntada %d" % (len(cid_d), len(ori_d)))
    L.append("• Leads em andamento SEM próximo passo (tarefa com data ou agendamento): *%d*" % len(sem_tarefa) + (": " + nomes(sem_tarefa, 6) if sem_tarefa else ""))
    L.append("• Base ativa: cidade não perguntada %d · origem não perguntada %d" % (len(cidade), len(origem_np)))
    _cad_hoje = {}
    try:
        _hoje = _sp_now().date().isoformat()
        _cd = db.collection("escuta_cadencia").document(_hoje).get()
        _co = db.collection("escuta_cadencia").document(d).get()
        if _cd.exists or _co.exists:
            L.append("")
            L.append("*📌 Cadência: leads sem próximo passo no CRM*")
            if _cd.exists:
                _pd = (_cd.to_dict() or {}).get("por_dono") or {}
                _cad_hoje.update(_pd)
                L.append("• Hoje: " + (" · ".join("%s %d" % (k.split()[0], v) for k, v in sorted(_pd.items(), key=lambda x: -x[1])) or "nenhum"))
            if _co.exists:
                _cf = ((_co.to_dict() or {}).get("conferencia") or {})
                if _cf.get("total"):
                    L.append("• Ontem resolvidos: %d de %d" % (_cf.get("resolvidos", 0), _cf["total"]) +
                             " (" + " · ".join("%s %d/%d" % (k.split()[0], v["resolvidos"], v["total"]) for k, v in (_cf.get("por_dono") or {}).items()) + ")" +
                             (" · %d com tarefa SEM descrição" % _cf["sem_descricao"] if _cf.get("sem_descricao") else ""))
    except Exception as _ex:
        print("[diario] cadencia:", _ex, flush=True)
    if fora:
        _cf = Counter(l.get("tipo_contato") for l in fora if l.get("data_chegada_sp") == d)
        if _cf:
            L.append("_fora da conta ontem: " + ", ".join("%s %d" % (k.replace("_", " "), v) for k, v in _cf.items()) + "_")
    if t1_med is not None:
        L.append("• 1ª resposta (mediana ontem): %d min · %d acima de 1h" % (int(t1_med), lentos))
    if notas:
        L.append("")
        L.append("*⭐ Nota de abordagem (0 a 6), conversas de ontem*")
        L.append("• " + " · ".join("%s %s (%d)" % (a, str(n).replace(".", ","), q) for a, n, q in notas[:6]))
    L.append("")
    if c:
        L.append("*🌡️ Termômetro agora:* 🔥 %d · 🌤️ %d · ❄️ %d · 💰 %d cross-sell" % (c.get("quente", 0), c.get("morno", 0), c.get("frio", 0), c.get("cross_sell", 0)))
    try:
        _et = db.collection("escuta_etiquetas").document(d).get()
        if _et.exists:
            _pe = (_et.to_dict() or {}).get("por_etiqueta") or {}
            if _pe:
                L.append("🏷️ Etiquetas colocadas pelo sistema ontem: %d (%s)" % (sum(_pe.values()), ", ".join("%s %d" % (k, v) for k, v in _pe.items())))
    except Exception:
        pass
    L.append("Saúde: " + ("✅ todas as fontes OK" if not alertas else "⚠️ " + " | ".join(alertas[:3])))
    L.append("")
    L.append("_Responda *ok* para manter este canal aberto._")
    texto = "\n".join(L)

    dados = {"dia": d, "chegadas": len(cheg), "leads_novos": len(novos), "clientes_voltaram": len(clientes),
             "por_origem": dict(por_origem), "trafego_pago_1a_msg": trafego, "meta_gasto": gasto, "meta_conversas": conv_meta,
             "cpl": cpl, "opps_criadas": len(criadas), "opps_movimentadas": len(mexidas), "orcamentos": len(orc_d),
             "vendas": len(ganhos_d), "notas_atendente": [{"atendente": a, "nota": n, "conversas": q} for a, n, q in notas],
             "sem_resposta": len(sem_resp), "sem_resposta_48h_mais": len(sem_resp_velhos), "sem_oportunidade": len(sem_opp),
             "cidade_nao_perguntada": len(cidade), "origem_nao_perguntada": len(origem_np), "sem_tarefa": len(sem_tarefa),
             "t1_mediana_min": t1_med, "t1_acima_1h": lentos, "alertas": alertas,
             # 30/10: campos para a IMAGEM do diario
             "sem_tarefa_nomes": nomes(sem_tarefa, 5) if sem_tarefa else "", "sem_resposta_nomes": nomes(sem_resp, 3, True) if sem_resp else "",
             "sem_resposta_clientes": len(sem_resp_cli), "termometro": dict(c), "termometro_pendentes": dict(term.get("pendentes") or {}),
             "cadencia_hoje": dict(_cad_hoje),
             "atencao": [{"nome": a.get("nome"), "problema": a.get("problema")} for a in (term.get("atencao") or [])][:8],
             # 30/10: origem completa para a imagem
             "leads_lista": [{"nome": (l.get("nome") or l.get("fone") or "?").strip()[:28], "origem": _orig(l),
                              "campanha": (l.get("campanha_tintim") or "")[:34],
                              "etapa": ((l.get("crm") or {}).get("estagio") or ("sem oportunidade" if l.get("situacao_crm") == "sem_oportunidade" else "?"))}
                             for l in novos][:12],
             "por_campanha": dict(Counter((l.get("campanha_tintim") or "campanha não identificada") for l in novos
                                          if str(l.get("origem_codigo") or "").startswith("trafego"))),
             "nao_informada": sum(1 for l in novos if l.get("origem_codigo") == "nao_informada"),
             "nao_informada_perguntou": sum(1 for l in novos if l.get("origem_codigo") == "nao_informada"
                                            and (l.get("conduta") or {}).get("perguntou_origem")),
             "orcamentos_centavos": sum(int(v.get("valor_centavos") or 0) for v in orc_d),
             "vendas_centavos": sum(int(v.get("valor_centavos") or 0) for v in ganhos_d),
             "vendas_itens": ["%s x%d" % (i, n) for i, n in itens.most_common(4)]}
    if gravar:
        db.collection(COLL).document(d).set({"dia": d, "texto": texto, "dados": dados, "status": "previa",
                                             "gerado_em": _sp_now().isoformat(timespec="seconds")})
    print("[diario] %s gerado (%d chegadas, %d sem resposta)" % (d, len(cheg), len(sem_resp)), flush=True)
    return {"ok": True, "dia": d, "texto": texto, "dados": dados}

def ler(dia=None):
    db = _fw._get_db()
    if not db:
        return None
    d = dia or (_sp_now().date() - dt.timedelta(days=1)).isoformat()
    s = db.collection(COLL).document(d).get()
    return s.to_dict() if s.exists else None

def iniciar(sched):
    if sched is None or os.environ.get("DIARIO_ON", "1") == "0":
        return
    sched.add_job(lambda: gerar_e_enviar(), "cron", hour=6, minute=45)
    sched.add_job(lembrete_janela, "cron", hour=20, minute=0)
    print("[diario] agendado 06h45 (gera + envia pelo CliniChat=%s)" % DIARIO_ENVIO, flush=True)


# ============================================================================= ENVIO PELO CLINICHAT (30/10)
# Mesmo endpoint que o app do CE usa: POST clinichat/message/send {type,content,contactChannelId,senderType,senderId}.
# Destino: DIARIO_FONE (padrao (18) 99717-2800, Xandao). Envia como o usuario dono do cracha.
# Janela de 24h do WhatsApp: se o Xandao nao falou com o numero da clinica nas ultimas 24h, mensagem livre e recusada
# (precisa template aprovado 'automacao_jarbas'). Falha fica em ENVIO e no /saude; a pagina do diario segue valendo.
import urllib.request as _ur, urllib.error as _ue, urllib.parse as _up, base64 as _b64

DIARIO_FONE = os.environ.get("DIARIO_FONE", "18997172800")
DIARIO_ENVIO = os.environ.get("DIARIO_ENVIO", "1") == "1"
CLINI = "https://clinichat.clinicaexperts.com.br"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
ENVIO = {"ultimo_em": None, "ok": None, "erro": None, "canal_id": None, "dia": None}

def _jwt_sub(tok):
    try:
        p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
        return json.loads(_b64.urlsafe_b64decode(p)).get("sub")
    except Exception:
        return None

def _req(method, path, tok, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = _ur.Request(CLINI + path, data=data, method=method, headers={
        "Authorization": "Bearer " + tok, "User-Agent": UA, "Accept": "application/json",
        "Content-Type": "application/json"})
    try:
        with _ur.urlopen(r, timeout=40) as resp:
            txt = resp.read().decode("utf-8", "ignore")
            return resp.status, (json.loads(txt) if txt.strip() else None)
    except _ue.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
        except Exception:
            return e.code, None

def _norm(p):
    import re as _re
    d = _re.sub(r"\D", "", p or "")
    if len(d) >= 12 and d.startswith("55"):
        d = d[2:]
    return d[:2] + d[-8:] if len(d) >= 10 else d

def _canal_do_fone(tok, fone):
    alvo = _norm(fone)
    for filtro in ({"term": [alvo[-8:]]}, {}):
        frm = 0
        while frm < 3000:
            q = _up.quote(json.dumps(filtro))
            st, d = _req("GET", "/contact-channel?from=%d&to=%d&orderBy=lastMessageTime%%3ADESC&filters=%s" % (frm, frm + 50, q), tok)
            if st != 200 or not d:
                break
            ccs = d.get("contactChannels") or []
            for c in ccs:
                ph = (c.get("contact") or {}).get("phone") or c.get("identifier") or ""
                if _norm(ph) == alvo:
                    return c.get("id") or c.get("contactChannelId"), c
            total = (d.get("pagination") or {}).get("total") or 0
            frm += 50
            if not ccs or frm >= total:
                break
    return None, None

def enviar(texto, fone=None):
    import firebase_writer as _f
    tok = _f.get_token()
    ENVIO.update({"ultimo_em": _sp_now().isoformat(timespec="seconds"), "ok": False, "erro": None})
    if not tok:
        ENVIO["erro"] = "sem cracha do CliniChat"; return dict(ENVIO)
    cid, cc = _canal_do_fone(tok, fone or DIARIO_FONE)
    if not cid:
        ENVIO["erro"] = "conversa do numero %s nao encontrada no CliniChat" % (fone or DIARIO_FONE); return dict(ENVIO)
    ENVIO["canal_id"] = cid
    body = {"type": "text", "content": texto, "contactChannelId": cid, "senderType": "users",
            "senderId": _jwt_sub(tok), "status": "pending"}
    st, resp = _req("POST", "/message/send", tok, body)
    ok = st in (200, 201)
    erro = None
    if not ok:
        erro = "http %s: %s" % (st, (resp or {}).get("message") if isinstance(resp, dict) else resp)
    else:
        # o CE devolve a lista de itens criados; se algum vier com erro de entrega, registra
        itens = resp if isinstance(resp, list) else [resp]
        for it in itens:
            if isinstance(it, dict) and (it.get("errorMessage") or it.get("status") in ("failed", "error")):
                ok, erro = False, "entrega recusada: %s" % (it.get("errorMessage") or it.get("status"))
    ENVIO.update({"ok": ok, "erro": erro})
    print("[diario] envio clinichat ok=%s erro=%s" % (ok, erro), flush=True)
    return dict(ENVIO)

def imagem(r):
    """PNG do diario a partir do resultado de gerar()."""
    import diario_img as _di
    return _di.render(r.get("dados") or {}, r["dia"], "Gerado %s · dados do banco do IDM" % _sp_now().strftime("%d/%m às %H:%M"))

def _multipart(campos, arq_nome, arq_bytes, mime):
    import uuid as _uu
    b = "----idm" + _uu.uuid4().hex
    partes = []
    for k, v in campos.items():
        partes.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode())
    partes.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                   % (b, arq_nome, mime)).encode() + arq_bytes + b"\r\n")
    partes.append(("--%s--\r\n" % b).encode())
    return b, b"".join(partes)

ENVIO_IMG = {"ultimo_em": None, "ok": None, "erro": None, "etapa": None}

_CE_UID = {"id": None, "tentou": []}

def _ce_user_id(app):
    """id do usuario no CE (dono do app_jwt). O 'sub' do cracha da CliniChat NAO serve (422 user_id)."""
    if os.environ.get("CE_USER_ID"):
        return os.environ["CE_USER_ID"]
    if _CE_UID["id"]:
        return _CE_UID["id"]
    import motor_cruzamento as _cruz
    for path in ("/api/auth/user", "/api/auth/me", "/api/me", "/api/user", "/api/users/me"):
        rq = _ur.Request(_cruz.INTERNAL + path, headers={"Authorization": "Bearer " + app, "User-Agent": UA, "Accept": "application/json"})
        try:
            with _ur.urlopen(rq, timeout=20) as resp:
                d = json.loads(resp.read().decode("utf-8", "ignore") or "{}")
        except Exception as e:
            _CE_UID["tentou"].append("%s:%s" % (path, getattr(e, "code", repr(e)[:30])))
            continue
        for cand in ((d.get("user") or {}).get("id") if isinstance(d.get("user"), dict) else None,
                     ((d.get("data") or {}).get("user") or {}).get("id") if isinstance(d.get("data"), dict) and isinstance((d.get("data") or {}).get("user"), dict) else None,
                     (d.get("data") or {}).get("id") if isinstance(d.get("data"), dict) else None,
                     d.get("id")):
            if cand:
                _CE_UID["id"] = str(cand)
                _CE_UID["tentou"].append("%s:ok" % path)
                return _CE_UID["id"]
        _CE_UID["tentou"].append("%s:sem id (%s)" % (path, ",".join(list(d.keys())[:6])))
    return None

def enviar_imagem(png, fone=None, legenda=""):
    """Mesmo caminho do app do CE: 1) POST api/file/upload (app_jwt, fileable clini_chat)
    2) POST clinichat /message/send type image com fileData/filePath/fileMeta."""
    import firebase_writer as _f
    import motor_cruzamento as _cruz
    ENVIO_IMG.update({"ultimo_em": _sp_now().isoformat(timespec="seconds"), "ok": False, "erro": None, "etapa": "inicio"})
    tok = _f.get_token()
    app = _cruz._app_jwt()
    if not tok or not app:
        ENVIO_IMG.update({"erro": "sem cracha" if not tok else "sem app_jwt"}); return dict(ENVIO_IMG)
    try:
        p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
        pl = json.loads(_b64.urlsafe_b64decode(p))
    except Exception:
        pl = {}
    clinic = str(pl.get("clinic_id") or os.environ.get("CE_CLINIC_ID", "57166"))
    uid = str(pl.get("sub") or "")
    cid, cc = _canal_do_fone(tok, fone or DIARIO_FONE)
    if not cid:
        ENVIO_IMG.update({"erro": "conversa nao encontrada"}); return dict(ENVIO_IMG)
    nome = "diario-%s.png" % _sp_now().strftime("%Y%m%d-%H%M")
    campos = {"fileable_type": "clini_chat", "fileable_group": "clini_chat", "fileable_id": clinic,
              "permanent": "false", "clinic_id": clinic}
    ce_uid = _ce_user_id(app)
    if ce_uid:
        campos["user_id"] = ce_uid
    ENVIO_IMG["ce_user"] = {"id": ce_uid, "tentou": _CE_UID["tentou"][-6:]}
    b, corpo = _multipart(campos, nome, png, "image/png")
    ENVIO_IMG["etapa"] = "upload"
    rq = _ur.Request(_cruz.INTERNAL + "/api/file/upload", data=corpo, method="POST", headers={
        "Authorization": "Bearer " + app, "User-Agent": UA, "Accept": "application/json",
        "Content-Type": "multipart/form-data; boundary=" + b})
    try:
        with _ur.urlopen(rq, timeout=60) as resp:
            up = json.loads(resp.read().decode("utf-8", "ignore") or "{}")
    except _ue.HTTPError as e:
        ENVIO_IMG.update({"erro": "upload http %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])}); return dict(ENVIO_IMG)
    except Exception as e:
        ENVIO_IMG.update({"erro": "upload: %s" % repr(e)[:160]}); return dict(ENVIO_IMG)
    if isinstance(up, dict) and isinstance(up.get("data"), dict) and not up.get("url"):
        up = up["data"]
    if not (isinstance(up, dict) and up.get("url")):
        ENVIO_IMG.update({"erro": "upload sem url: %s" % str(up)[:200]}); return dict(ENVIO_IMG)
    # passo 2 (igual ao app): avisa o CE dos arquivos que vao para o chat
    ENVIO_IMG["etapa"] = "handle-files"
    try:
        rq2 = _ur.Request(_cruz.INTERNAL + "/api/clini-chat/handle-files", method="POST",
                          data=json.dumps({"file_ids": [up.get("id")]}).encode(), headers={
                              "Authorization": "Bearer " + app, "User-Agent": UA, "Accept": "application/json",
                              "Content-Type": "application/json"})
        with _ur.urlopen(rq2, timeout=40) as resp2:
            ENVIO_IMG["handle_files"] = resp2.status
    except _ue.HTTPError as e:
        ENVIO_IMG["handle_files"] = "http %s" % e.code
    except Exception as e:
        ENVIO_IMG["handle_files"] = repr(e)[:80]
    # passo 3 (igual ao app): message/send em multipart COM o arquivo + os campos da mensagem
    ENVIO_IMG["etapa"] = "send"
    campos2 = {"type": "image", "content": legenda or "", "contactChannelId": cid, "senderType": "users",
               "senderId": uid, "status": "pending", "fileData": up.get("url"),
               "filePath": "buckets/%s/%s.%s" % (up.get("path"), up.get("key"), up.get("extension") or "png"),
               "fileMeta": json.dumps(up, ensure_ascii=False, default=str)}
    b2, corpo2 = _multipart(campos2, nome, png, "image/png")
    rq3 = _ur.Request(CLINI + "/message/send", data=corpo2, method="POST", headers={
        "Authorization": "Bearer " + tok, "User-Agent": UA, "Accept": "application/json",
        "Content-Type": "multipart/form-data; boundary=" + b2})
    try:
        with _ur.urlopen(rq3, timeout=60) as resp3:
            st = resp3.status
            txt3 = resp3.read().decode("utf-8", "ignore")
            resp = json.loads(txt3) if txt3.strip() else None
    except _ue.HTTPError as e:
        st, resp = e.code, e.read().decode("utf-8", "ignore")[:300]
    ENVIO_IMG["resposta"] = (str([{k: (it.get(k) if isinstance(it, dict) else None) for k in ("type", "status", "errorMessage", "fileData")}
                                  for it in (resp if isinstance(resp, list) else [resp])])[:400])
    ok = st in (200, 201)
    erro = None if ok else "send http %s: %s" % (st, str(resp)[:200])
    if ok:
        for it in (resp if isinstance(resp, list) else [resp]):
            if isinstance(it, dict) and (it.get("errorMessage") or it.get("status") in ("failed", "error")):
                ok, erro = False, "entrega recusada: %s" % (it.get("errorMessage") or it.get("status"))
    ENVIO_IMG.update({"ok": ok, "erro": erro, "etapa": "fim"})
    print("[diario] imagem ok=%s erro=%s" % (ok, erro), flush=True)
    return dict(ENVIO_IMG)

def gerar_e_enviar():
    try:
        r = gerar()
    except Exception as ex:
        import traceback as _tb
        ENVIO.update({"ultimo_em": _sp_now().isoformat(timespec="seconds"), "ok": False,
                      "erro": "falha ao GERAR o diario: %s" % repr(ex)[:160]})
        print("[diario] erro ao gerar:", _tb.format_exc(), flush=True)
        return {"ok": False, "erro": repr(ex)[:200]}
    if DIARIO_ENVIO and r.get("ok"):
        ei = {}
        if os.environ.get("DIARIO_IMAGEM", "1") == "1":     # 30/10: imagem primeiro, texto completo logo abaixo
            try:
                ei = enviar_imagem(imagem(r))
            except Exception as _ex:
                ei = {"ok": False, "erro": "imagem: %s" % repr(_ex)[:160]}
        e = enviar(r["texto"])
        e = dict(e, imagem=ei)
        try:
            db = _fw._get_db()
            db.collection(COLL).document(r["dia"]).set({"envio": e, "status": "enviado" if e.get("ok") else "falha_envio"}, merge=True)
        except Exception:
            pass
        ENVIO["dia"] = r["dia"]
    return r


def lembrete_janela():
    """20h: se o Xandao nao respondeu desde o Diario das 06h45, lembra de responder (mantem a janela de 24h)."""
    db = _fw._get_db()
    if not db:
        return {"ok": False}
    f = db.collection("escuta_conversas").document(_norm(DIARIO_FONE)).get()
    v = f.to_dict() or {} if f.exists else {}
    corte = (_sp_now().replace(hour=6, minute=40, second=0, microsecond=0) + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    respondeu = v.get("ultima_msg_de") == "lead" and str(v.get("ultima_msg_em") or "") >= corte
    if respondeu:
        return {"ok": True, "lembrete": False}
    r = enviar("🔔 Jarbas: responda *ok* aqui para o Diário de amanhã chegar (o WhatsApp só deixa enviar se você falou com este número nas últimas 24h).")
    return {"ok": r.get("ok"), "lembrete": True, "erro": r.get("erro")}
