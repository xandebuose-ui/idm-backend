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

# ============================================================================ v2 (15/11/2026)
# Regra unica: chegadas e origem vem do MESMO calculo do painel de Marketing (painel_mkt.montar).
# Formato aprovado pelo Xandao: uma informacao por linha, total na frente, nada separado por virgula.
_BKT = {"trafego": "Tráfego pago · Captação", "reativacao": "Tráfego pago · Remarketing", "mkt": "Orgânico",
        "rel": "Indicação", "nao": "Sem origem"}
_ORDEM_PIPE = ("LEADS", "CLIENTES ATIVOS", "REATIVA", "BLACK FRIDAY")
_FECHADO = ("ganho", "perdido", "desqualificado")
FOTO_COLL = os.environ.get("DIARIO_FOTO_COLL", "escuta_funil_foto")

def _pct(a, b):
    return "%d%%" % round(100.0 * a / b) if b else "0%"

def _canal_txt(c):
    c = c or "outro canal"
    return "I.A.s" if "IA" in c or "ChatGPT" in c else c

def _origem_linhas(L, base, ind=""):
    """Origem agrupada por balde, com quebra por canal/campanha. base = total para o %."""
    out = []
    grupos = {}
    for l in L:
        grupos.setdefault(l.get("bucket") or "nao", []).append(l)
    for b in sorted(grupos, key=lambda k: -len(grupos[k])):
        arr = grupos[b]
        out.append("%s%d · %s · %s" % (ind, len(arr), _BKT.get(b, b), _pct(len(arr), base)))
        if b in ("trafego", "reativacao"):
            sub = Counter((x.get("campanha") or "campanha não identificada") for x in arr)
        elif b == "mkt":
            sub = Counter(_canal_txt(x.get("canal")) for x in arr)
        else:
            sub = None
        if sub and (len(sub) > 1 or b != "nao"):
            for k, v in sub.most_common():
                out.append("%s   %d · %s" % (ind, v, k))
    return out

def _raw_stage(o):
    try:
        st = (json.loads(o.get("raw") or "{}") or {}).get("stage") or {}
        return st.get("order"), st.get("type")
    except Exception:
        return None, None

def _funis(crm, d, db):
    """{pipeline: [(etapa, qtd, ordem, fechado)]} + foto do dia gravada + foto anterior para a variacao."""
    por = {}
    for o in crm:
        pz = o.get("pipeline") or "?"
        et = o.get("estagio") or "?"
        ordem, tipo = _raw_stage(o)
        fech = tipo in ("won", "lost") or any(w in et.lower() for w in _FECHADO)
        k = por.setdefault(pz, {})
        if et not in k:
            k[et] = [0, ordem if ordem is not None else 99, fech]
        k[et][0] += 1
    foto = {pz: {et: v[0] for et, v in ets.items()} for pz, ets in por.items()}
    ant = {}
    try:
        db.collection(FOTO_COLL).document(_sp_now().date().isoformat()).set(
            {"dia": _sp_now().date().isoformat(), "funis": foto, "gravado_em": _sp_now().isoformat(timespec="seconds")})
        prev = (_sp_now().date() - dt.timedelta(days=1)).isoformat()
        s = db.collection(FOTO_COLL).document(prev).get()
        ant = (s.to_dict() or {}).get("funis") or {} if s.exists else {}
    except Exception as ex:
        print("[diario] foto funil:", ex, flush=True)
    return por, ant

def gerar(dia=None, gravar=True):
    db = _fw._get_db()
    if not db:
        return {"ok": False, "erro": "firestore indisponivel"}
    import painel_mkt as _pm
    d = dia or (_sp_now().date() - dt.timedelta(days=1)).isoformat()
    dd = dt.date.fromisoformat(d)
    mes_ini = dd.replace(day=1).isoformat()
    aviso = []
    todos = [x.to_dict() or {} for x in db.collection("escuta_leads").stream()]
    ficha = {l.get("fone"): l for l in todos if l.get("fone")}
    FORA = ("fornecedor_parceiro", "teste_interno", "equipe_interna", "spam", "sem_telefone")
    leads = [l for l in todos if l.get("tipo_contato") not in FORA]
    crm = [x.to_dict() or {} for x in db.collection(os.environ.get("ESCUTA_CRM_COLL", "escuta_crm")).stream() if x.id != "mapa"]
    vendas = [x.to_dict() or {} for x in db.collection("escuta_vendas").stream()]
    meta = (db.collection("escuta_meta").document(d).get().to_dict() or {})
    term = (db.collection(os.environ.get("TERM_COLL", "escuta_termometro")).document("atual").get().to_dict() or {})

    # ---- chegadas e origem: MESMO calculo do painel de Marketing
    try:
        P = _pm.montar(d, d)
    except Exception as ex:
        P = {"leads": [], "fora": []}
        aviso.append("painel indisponível (%s)" % repr(ex)[:60])
    try:
        PM = _pm.pronto(mes_ini, d)
    except Exception as ex:
        PM = {"leads": []}
        aviso.append("mês indisponível (%s)" % repr(ex)[:60])
    L = P.get("leads") or []
    F = P.get("fora") or []
    cli = [f for f in F if str(f.get("motivo") or "").startswith("cliente da base")
           and (ficha.get(f.get("fone")) or {}).get("data_chegada_sp") == d and ((ficha.get(f.get("fone")) or {}).get("msgs_lead") or 0) > 0]
    conferir = [f for f in F if "legível" in str(f.get("motivo") or "")]
    outros = [f for f in F if f not in cli and f not in conferir and not str(f.get("motivo") or "").startswith("cliente da base")]
    trafego = sum(1 for l in L if l.get("bucket") in ("trafego", "reativacao"))

    def _quem(fo):
        f = ficha.get(fo) or {}
        a = (f.get("conduta") or {}).get("atendente") or ((f.get("crm") or {}).get("vendedor")) or ""
        return a.split()[0] if a else "sem atendente"

    def _tel(fo):
        fo = str(fo or "")
        return "%s %s" % (fo[:2], fo[2:]) if len(fo) == 10 else fo

    # ---- meta
    gasto = float(meta.get("gasto_total") or 0)
    conv_meta = int(meta.get("conversas_iniciadas_total") or 0)
    cpl = (gasto / trafego) if trafego else None

    # ---- orcamentos / vendas / perdidos / agendamentos
    orc_d = [v for v in vendas if _dia_ce(v.get("data")) == d]
    ganhos_d = [v for v in vendas if v.get("status") == "won" and (_dia_ce(v.get("atualizado_em_ce")) == d or _dia_ce(v.get("data")) == d)]
    itens = Counter(i.get("nome") for v in ganhos_d for i in (v.get("itens") or []) if isinstance(i, dict) and i.get("nome"))
    perd_d = [o for o in crm if _dia_ce(o.get("atualizado_em_ce")) == d and "perdido" in str(o.get("estagio") or "").lower()]
    ag_pres = ag_on = 0
    try:
        bks = _pm._bookings(d, d)
        for fo, arr in (bks or {}).items():
            for b in arr:
                if str(b.get("criado") or "")[:10] == d and "avalia" in str(b.get("proc") or "").lower():
                    if "on-line" in str(b.get("proc") or "").lower() or "online" in str(b.get("proc") or "").lower():
                        ag_on += 1
                    else:
                        ag_pres += 1
    except Exception as ex:
        aviso.append("agenda indisponível")
        print("[diario] bookings:", ex, flush=True)

    # ---- funis do CRM (foto diaria para a variacao)
    por, ant = _funis(crm, d, db)

    # ---- furos que ja existiam (estado AGORA)
    ativos = [l for l in leads if l.get("identidade") not in ("cliente", "sem_telefone") and l.get("situacao_crm") != "perdido"]
    sem_resp_all = [l for l in leads if "lead_sem_resposta" in (l.get("furos") or []) and l.get("identidade") != "cliente"]
    sem_resp = sorted([l for l in sem_resp_all if (l.get("horas_desde_ultima") or 0) <= 48], key=lambda l: -(l.get("horas_desde_ultima") or 0))
    sem_resp_velhos = [l for l in sem_resp_all if (l.get("horas_desde_ultima") or 0) > 48]
    sem_resp_cli = [l for l in leads if "lead_sem_resposta" in (l.get("furos") or []) and l.get("identidade") == "cliente"]
    sem_opp = [l for l in L if not l.get("opp")]
    sem_tarefa = sorted([l for l in ativos if "oportunidade_sem_tarefa" in (l.get("furos") or [])],
                        key=lambda l: str(l.get("ultima_msg_em") or ""), reverse=True)
    from collections import defaultdict as _dd
    _nt = _dd(list)
    for l in leads:
        if l.get("nota") is not None and _dia_ce(l.get("ultima_msg_em")) == d and (l.get("conduta") or {}).get("atendente"):
            _nt[(l["conduta"]["atendente"]).split()[0]].append(float(l["nota"]))
    notas = sorted(((a, round(sum(v) / len(v), 1), len(v)) for a, v in _nt.items()), key=lambda x: -x[2])
    t1 = [l.get("primeira_resp_min") for l in L if l.get("primeira_resp_min") is not None]
    t1 = sorted(t1)
    t1_med = t1[len(t1) // 2] if t1 else None
    lentos = sum(1 for x in t1 if x > 60)

    try:
        import coletor as _c
        alertas = _c.saude().get("alertas") or []
    except Exception as e:
        alertas = ["saude indisponivel: %s" % str(e)[:60]]
    try:
        _ti = _fw.token_info()
        _hh = _ti.get("expira_em_horas")
        if _ti.get("vencido"):
            alertas.insert(0, "CRACHÁ DA CLINICHAT VENCIDO: renove já (nada novo entra no sistema)")
            aviso.append("crachá vencido")
        elif _hh is not None and _hh <= 72:
            alertas.insert(0, "crachá da CliniChat vence em %d h: renove" % int(_hh))
    except Exception:
        pass

    # ================================================================ TEXTO
    T = []
    T.append("*IDM · Diário comercial · %s %s*" % (DIAS_PT[dd.weekday()], dd.strftime("%d/%m")))
    T.append("_gerado %s_" % _sp_now().strftime("%d/%m %H:%M"))
    if aviso:
        T.append("⚠️ *Dados incompletos (%s): não usar para cobrança.*" % "; ".join(aviso))
    T.append("")
    T.append("*📥 Chegadas*")
    T.append("%d · Leads novos" % len(L))
    T.append("%d · Clientes que mandaram mensagem" % len(cli))
    T.append("%d · Conferir (mensagem ilegível)" % len(conferir))
    if outros:
        T.append("%d · Outros (fora da conta)" % len(outros))
    if L:
        T.append("")
        T.append("*🧭 Origem dos %d leads*" % len(L))
        T += _origem_linhas(L, len(L))
    T.append("")
    T.append("*📅 Avaliações marcadas no dia*")
    T.append("%d · Total" % (ag_pres + ag_on))
    T.append("%d · Presencial" % ag_pres)
    T.append("%d · On-line" % ag_on)
    T.append("")
    T.append("*💰 Resultado do dia*")
    T.append("%d · Perdidos (funil LEADS)" % sum(1 for o in perd_d if str(o.get("pipeline") or "").upper().startswith("LEADS")))
    T.append("%d · Orçamentos · %s" % (len(orc_d), _brl(sum(int(v.get("valor_centavos") or 0) for v in orc_d))))
    T.append("%d · Vendas · %s" % (len(ganhos_d), _brl(sum(int(v.get("valor_centavos") or 0) for v in ganhos_d))))
    for i, n in itens.most_common(4):
        T.append("   %d · %s" % (n, i))
    T.append("")
    T.append("*📣 Meta Ads*")
    if meta:
        T.append("%s · Gasto" % _brl_reais(gasto))
        T.append("%d · Conversas no Meta" % conv_meta)
        T.append("%d · Chegaram pelo anúncio" % trafego)
        if cpl:
            T.append("%s · Custo por lead" % _brl_reais(cpl))
    else:
        T.append("sem dado do dia (coletor ainda não gravou)")
    # cobranca: sem origem
    so = [l for l in L if l.get("bucket") == "nao"]
    if so:
        T.append("")
        T.append("*🚨 Sem origem, cobrar (%d)*" % len(so))
        for l in so:
            fu = l.get("furos") or []
            if "chegou por fora" in str(l.get("prova") or ""):
                mot = "chegou por fora"
            elif "origem_nao_perguntada" in fu:
                mot = "não perguntou"
            else:
                mot = "perguntou, sem resposta"
            T.append("• %s · %s · %s · %s" % ((l.get("nome") or "?").strip()[:28], _tel(l.get("fone")), _quem(l.get("fone")), mot))
    if conferir:
        T.append("")
        T.append("*🔍 Conferir (%d)*" % len(conferir))
        for f in conferir:
            T.append("• %s · mensagem ilegível" % _tel(f.get("fone")))
    if sem_opp:
        T.append("")
        T.append("*📋 Leads de ontem sem oportunidade no CRM (%d)*" % len(sem_opp))
        for l in sem_opp:
            T.append("• %s · %s · %s" % ((l.get("nome") or "?").strip()[:28], _tel(l.get("fone")), _quem(l.get("fone"))))
    # funis
    pipes = sorted(por.keys(), key=lambda p: next((i for i, k in enumerate(_ORDEM_PIPE) if p.upper().startswith(k)), 9))
    for pz in pipes:
        ets = por[pz]
        abertas = [(et, v) for et, v in ets.items() if not v[2]]
        tot = sum(v[0] for _, v in abertas)
        T.append("")
        T.append("*📊 Funil %s*" % pz)
        T.append("%d · Em andamento" % tot)
        prev = ant.get(pz) or {}
        for et, v in sorted(abertas, key=lambda x: x[1][1]):
            var = ""
            if prev:
                dlt = v[0] - int(prev.get(et) or 0)
                var = " (%s%d)" % ("+" if dlt > 0 else "", dlt) if dlt else " (0)"
            T.append("%d · %s · %s%s" % (v[0], et, _pct(v[0], tot), var))
        fech_d = Counter(o.get("estagio") for o in crm if (o.get("pipeline") or "?") == pz
                         and _dia_ce(o.get("atualizado_em_ce")) == d and (ets.get(o.get("estagio")) or [0, 0, False])[2])
        if fech_d:
            T.append("_Fechados no dia_")
            for et, n in fech_d.most_common():
                T.append("%d · %s" % (n, et))
    # mes
    LM = PM.get("leads") or []
    if LM:
        T.append("")
        T.append("*📅 %s até ontem (leads)*" % ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
                                                   "Setembro", "Outubro", "Novembro", "Dezembro"][dd.month - 1])
        com = sum(1 for l in LM if l.get("opp"))
        T.append("%d · Leads chegaram" % len(LM))
        T.append("%d · Com oportunidade · %s" % (com, _pct(com, len(LM))))
        T.append("%d · Sem oportunidade · %s" % (len(LM) - com, _pct(len(LM) - com, len(LM))))
        T.append("_Origem_")
        T += _origem_linhas(LM, len(LM))
        cm = [o for o in crm if str(o.get("pipeline") or "").upper().startswith("LEADS") and str(_dia_ce(o.get("criado_em_ce")) or "") >= mes_ini]
        if cm:
            cnt = Counter(o.get("estagio") for o in cm)
            g = sum(n for e, n in cnt.items() if "ganho" in str(e).lower())
            p_ = sum(n for e, n in cnt.items() if "perdido" in str(e).lower())
            q = sum(n for e, n in cnt.items() if "desqualif" in str(e).lower())
            T.append("_Oportunidades do mês (funil LEADS)_")
            T.append("%d · Criadas" % len(cm))
            T.append("%d · Ganhos · %s" % (g, _pct(g, len(cm))))
            T.append("%d · Perdidos · %s" % (p_, _pct(p_, len(cm))))
            T.append("%d · Desqualificados · %s" % (q, _pct(q, len(cm))))
            T.append("%d · Ainda abertas · %s" % (len(cm) - g - p_ - q, _pct(len(cm) - g - p_ - q, len(cm))))
    # furos que ja existiam
    _at = list(term.get("atencao") or [])
    if _at:
        T.append("")
        T.append("*🚨 Atenção agora (%d)*" % len(_at))
        for _a in _at[:8]:
            T.append("• %s · %s · %s" % ((_a.get("nome") or "?").strip(), _a.get("problema"), str(_a.get("dono") or "sem dono").split()[0]))
        if len(_at) > 8:
            T.append("• e mais %d no Painel de Leads" % (len(_at) - 8))
    T.append("")
    T.append("*⏱️ Atendimento*")
    T.append("%d · Leads esperando resposta (até 48h)" % len(sem_resp))
    for l in sem_resp[:6]:
        T.append("   %s · %dh" % ((l.get("nome") or l.get("fone") or "?").strip()[:28], int(l.get("horas_desde_ultima") or 0)))
    T.append("%d · Leads sem resposta há mais de 48h" % len(sem_resp_velhos))
    T.append("%d · Clientes esperando resposta" % len(sem_resp_cli))
    T.append("%d · Leads em andamento sem próximo passo" % len(sem_tarefa))
    if t1_med is not None:
        T.append("%d min · 1ª resposta (mediana ontem)" % int(t1_med))
        T.append("%d · 1ª resposta acima de 1h" % lentos)
    try:
        _hoje = _sp_now().date().isoformat()
        _cd = db.collection("escuta_cadencia").document(_hoje).get()
        if _cd.exists:
            _pd = (_cd.to_dict() or {}).get("por_dono") or {}
            T.append("")
            T.append("*📌 Cadência: leads sem próximo passo hoje*")
            for k, v in sorted(_pd.items(), key=lambda x: -x[1]):
                T.append("%d · %s" % (v, k.split()[0]))
    except Exception as _ex:
        print("[diario] cadencia:", _ex, flush=True)
    if notas:
        T.append("")
        T.append("*⭐ Nota de abordagem (0 a 6), conversas de ontem*")
        for a, n, q in notas[:6]:
            T.append("%s · %s (%d conversas)" % (str(n).replace(".", ","), a, q))
    c = (term.get("contagem") or {})
    if c:
        T.append("")
        T.append("*🌡️ Termômetro agora*")
        T.append("%d · Quentes" % c.get("quente", 0))
        T.append("%d · Mornos" % c.get("morno", 0))
        T.append("%d · Frios" % c.get("frio", 0))
        T.append("%d · Cross-sell" % c.get("cross_sell", 0))
    T.append("")
    T.append("Saúde: " + ("✅ todas as fontes OK" if not alertas else "⚠️ " + " | ".join(alertas[:3])))
    T.append("")
    T.append("_Responda *ok* para manter este canal aberto._")
    texto = "\n".join(T)

    por_origem = Counter(_BKT.get(l.get("bucket"), l.get("bucket")) for l in L)
    dados = {"dia": d, "versao": 2, "chegadas": len(L) + len(cli), "leads_novos": len(L), "clientes_voltaram": len(cli),
             "conferir": len(conferir), "outros": len(outros), "por_origem": dict(por_origem), "trafego_pago_1a_msg": trafego,
             "meta_gasto": gasto, "meta_conversas": conv_meta, "cpl": cpl, "orcamentos": len(orc_d), "vendas": len(ganhos_d),
             "avaliacoes_presencial": ag_pres, "avaliacoes_online": ag_on,
             "notas_atendente": [{"atendente": a, "nota": n, "conversas": q} for a, n, q in notas],
             "sem_resposta": len(sem_resp), "sem_resposta_48h_mais": len(sem_resp_velhos), "sem_oportunidade": len(sem_opp),
             "sem_tarefa": len(sem_tarefa), "t1_mediana_min": t1_med, "t1_acima_1h": lentos, "alertas": alertas, "aviso": aviso,
             "sem_tarefa_nomes": ", ".join((l.get("nome") or "?").strip() for l in sem_tarefa[:5]),
             "sem_resposta_nomes": ", ".join((l.get("nome") or "?").strip() for l in sem_resp[:3]),
             "sem_resposta_clientes": len(sem_resp_cli), "termometro": dict(c), "termometro_pendentes": dict(term.get("pendentes") or {}),
             "cadencia_hoje": {}, "atencao": [{"nome": a.get("nome"), "problema": a.get("problema")} for a in _at][:8],
             "leads_lista": [{"nome": (l.get("nome") or l.get("fone") or "?").strip()[:28], "origem": _BKT.get(l.get("bucket"), ""),
                              "campanha": (l.get("campanha") or "")[:34], "etapa": l.get("etapa") or ("sem oportunidade" if not l.get("opp") else "?")}
                             for l in L][:12],
             "por_campanha": dict(Counter((l.get("campanha") or "campanha não identificada") for l in L if l.get("bucket") in ("trafego", "reativacao"))),
             "nao_informada": sum(1 for l in L if l.get("bucket") == "nao"),
             "nao_informada_perguntou": sum(1 for l in L if l.get("bucket") == "nao" and "origem_nao_perguntada" not in (l.get("furos") or [])),
             "orcamentos_centavos": sum(int(v.get("valor_centavos") or 0) for v in orc_d),
             "vendas_centavos": sum(int(v.get("valor_centavos") or 0) for v in ganhos_d),
             "vendas_itens": ["%s x%d" % (i, n) for i, n in itens.most_common(4)]}
    if gravar:
        db.collection(COLL).document(d).set({"dia": d, "texto": texto, "dados": dados, "status": "previa",
                                             "gerado_em": _sp_now().isoformat(timespec="seconds")})
    print("[diario] v2 %s gerado (%d leads, %d sem origem)" % (d, len(L), len(so)), flush=True)
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
    if (r.get("dados") or {}).get("versao") == 2:     # 15/11: imagem desenhada a partir do texto v2 (mesmo padrao visual)
        return _di.render_v2(r.get("dados") or {}, r.get("texto") or "", r["dia"], "Gerado %s · dados do banco do IDM" % _sp_now().strftime("%d/%m às %H:%M"))
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
