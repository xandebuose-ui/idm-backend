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
    leads = [x.to_dict() or {} for x in db.collection("escuta_leads").stream()]
    crm = [x.to_dict() or {} for x in db.collection(os.environ.get("ESCUTA_CRM_COLL", "escuta_crm")).stream() if x.id != "mapa"]
    vendas = [x.to_dict() or {} for x in db.collection("escuta_vendas").stream()]
    meta = (db.collection("escuta_meta").document(d).get().to_dict() or {}) if True else {}
    term = (db.collection(os.environ.get("TERM_COLL", "escuta_termometro")).document("atual").get().to_dict() or {})

    # ---- chegadas do dia
    # chegada = conversa nova em que a PESSOA mandou mensagem (conversa so com automatica nao conta)
    cheg = [l for l in leads if l.get("data_chegada_sp") == d and l.get("identidade") != "sem_telefone" and (l.get("msgs_lead") or 0) > 0]
    novos = [l for l in cheg if l.get("identidade") != "cliente"]
    clientes = [l for l in cheg if l.get("identidade") == "cliente"]
    por_origem = Counter(l.get("origem") or "Origem não informada" for l in novos)
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
    sem_tarefa = [l for l in ativos if "oportunidade_sem_tarefa" in (l.get("furos") or [])]
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

    c = (term.get("contagem") or {})
    L = []
    L.append("*IDM · Diário comercial — %s %s*" % (DIAS_PT[dd.weekday()], dd.strftime("%d/%m")))
    L.append("_prévia — gerado %s_" % _sp_now().strftime("%d/%m %H:%M"))
    L.append("")
    L.append("*📥 Chegadas:* %d conversas novas" % len(cheg))
    L.append("• Leads: *%d*" % len(novos) + ("" if not novos else " — " + " · ".join("%s: %d" % (o, n) for o, n in por_origem.most_common())))
    L.append("• Clientes que voltaram a falar: %d" % len(clientes))
    if meta:
        L.append("*📣 Meta Ads (%s):* gasto %s · conversas iniciadas %d" % (dd.strftime("%d/%m"), _brl_reais(gasto), conv_meta) +
                 (" · custo por lead (1ª msg de tráfego) %s" % _brl_reais(cpl) if cpl else (" · nenhum lead de tráfego pago identificado pela 1ª msg" if gasto else "")))
    else:
        L.append("*📣 Meta Ads:* sem dado do dia (coletor ainda não gravou)")
    L.append("*📊 CRM:* oportunidades abertas: %d" % len(criadas) +
             ("" if not criadas else " (" + ", ".join("%s %d" % (e, n) for e, n in et_criadas.most_common()) + ")") +
             " · movimentadas: %d" % len(mexidas))
    L.append("*🧾 Orçamentos:* %d gerados (%s) · *vendas: %d (%s)*" % (len(orc_d), _brl(sum(int(v.get("valor_centavos") or 0) for v in orc_d)),
             len(ganhos_d), _brl(sum(int(v.get("valor_centavos") or 0) for v in ganhos_d))) +
             ("" if not itens else " — " + ", ".join("%s x%d" % (i, n) for i, n in itens.most_common(4))))
    L.append("")
    L.append("*🚨 Furos para cobrar hoje*")
    L.append("• Leads esperando resposta (até 48h — agir hoje): *%d*" % len(sem_resp) + (" — " + nomes(sem_resp, 6, True) if sem_resp else ""))
    if sem_resp_velhos:
        L.append("• Leads sem resposta há mais de 48h (esfriando): %d" % len(sem_resp_velhos))
    if sem_resp_cli:
        L.append("• Clientes esperando resposta: %d" % len(sem_resp_cli))
    L.append("• Leads novos de ontem sem oportunidade no CRM: *%d*" % len(sem_opp) + (" — " + nomes(sem_opp) if sem_opp else ""))
    cid_d = [l for l in cidade if l.get("data_chegada_sp") == d]
    ori_d = [l for l in origem_np if l.get("data_chegada_sp") == d]
    L.append("• Nas chegadas de ontem: cidade/bairro não perguntados %d · origem não perguntada %d" % (len(cid_d), len(ori_d)))
    L.append("• Base ativa inteira: cidade não perguntada %d · origem não perguntada %d · oportunidade sem tarefa %d" % (len(cidade), len(origem_np), len(sem_tarefa)))
    if t1_med is not None:
        L.append("• 1ª resposta (mediana ontem): %d min · %d acima de 1h" % (int(t1_med), lentos))
    L.append("")
    if c:
        L.append("*🌡️ Termômetro agora:* 🔥 %d · 🌤️ %d · ❄️ %d · 💰 %d cross-sell" % (c.get("quente", 0), c.get("morno", 0), c.get("frio", 0), c.get("cross_sell", 0)))
    L.append("— Saúde: " + ("✅ todas as fontes OK" if not alertas else "⚠️ " + " | ".join(alertas[:3])))
    texto = "\n".join(L)

    dados = {"dia": d, "chegadas": len(cheg), "leads_novos": len(novos), "clientes_voltaram": len(clientes),
             "por_origem": dict(por_origem), "trafego_pago_1a_msg": trafego, "meta_gasto": gasto, "meta_conversas": conv_meta,
             "cpl": cpl, "opps_criadas": len(criadas), "opps_movimentadas": len(mexidas), "orcamentos": len(orc_d),
             "vendas": len(ganhos_d), "sem_resposta": len(sem_resp), "sem_resposta_48h_mais": len(sem_resp_velhos), "sem_oportunidade": len(sem_opp),
             "cidade_nao_perguntada": len(cidade), "origem_nao_perguntada": len(origem_np), "sem_tarefa": len(sem_tarefa),
             "t1_mediana_min": t1_med, "t1_acima_1h": lentos, "alertas": alertas}
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
    sched.add_job(lambda: gerar(), "cron", hour=6, minute=45)
    print("[diario] agendado 06h45 (previa, sem envio)", flush=True)
