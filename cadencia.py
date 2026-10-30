# -*- coding: utf-8 -*-
"""
IDM - CADENCIA (30/10/2026). Regra do gestor: nenhuma conversa de lead termina sem TAREFA no CRM (proximo passo + data).

Todo dia 06h35 (depois da ficha viva completa das 06h25, antes do Diario das 06h45):
  1) CONFERE as tarefas de cadencia de ONTEM: o lead agora tem tarefa ABERTA no CRM *com descricao*? -> resolvido.
     Tarefa sem descricao NAO conta (a atendente tem que anotar o que foi feito / o proximo passo).
  2) GERA as de HOJE: uma tarefa no "Minhas Tarefas" do IDM (colecao tasks) para cada lead em andamento no funil LEADS
     sem tarefa aberta no CRM. Dono = responsavel da oportunidade no CRM; se vazio, quem mais falou com o lead.
     Sem dono identificado -> vai para a funcao 'comercial'.
  3) 07h00: manda a lista de cada atendente no WhatsApp dela pelo CliniChat.
Resumo do dia em escuta_cadencia/{AAAA-MM-DD} (o Diario le daqui).
"""
import os, json, unicodedata, datetime as dt
from collections import defaultdict

import firebase_writer as _fw

COLL = "escuta_cadencia"
FONES = {}
for _p in os.environ.get("ATENDENTES_FONES", "isadora:11948631848,julia:11988611885").split(","):
    if ":" in _p:
        k, v = _p.split(":", 1)
        FONES[k.strip().lower()] = v.strip()
ST = {"ultima": None, "ok": None, "erro": None, "geradas": 0, "conferidas": 0, "resolvidas": 0, "envios": {}}

def _sp_now():
    return dt.datetime.utcnow() - dt.timedelta(hours=3)

def _key(nome):
    s = unicodedata.normalize("NFKD", (nome or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.split()[0] if s.split() else ""

def _usuarios(db):
    out = {}
    for d in db.collection("users").stream():
        u = d.to_dict() or {}
        k = _key(u.get("name"))
        if k and u.get("role") in ("comercial", "recepcao", "gestao", "social_seller", "avaliadora"):
            out[k] = {"uid": d.id, "nome": u.get("name"), "role": u.get("role")}
    return out

def _tarefas_crm(db):
    """fone -> lista de tarefas ABERTAS (com flag de descricao) das oportunidades do funil LEADS."""
    out = defaultdict(list)
    for d in db.collection(os.environ.get("ESCUTA_CRM_COLL", "escuta_crm")).stream():
        if d.id == "mapa":
            continue
        o = d.to_dict() or {}
        if not o.get("fone"):
            continue
        try:
            raw = json.loads(o.get("raw") or "{}")
        except Exception:
            raw = {}
        for t in raw.get("tasks") or []:
            if isinstance(t, dict) and not (t.get("done") or t.get("status") == "completed"):
                out[o["fone"]].append({"tem_descricao": bool((t.get("description") or "").strip()),
                                        "resumo": t.get("summary"), "vence": t.get("ends_at")})
    return out

def conferir(db, dia):
    """Confere as tarefas de cadencia de 'dia' contra o CRM de agora."""
    ref = db.collection(COLL).document(dia)
    snap = ref.get()
    if not snap.exists:
        return None
    doc = snap.to_dict() or {}
    abertas = _tarefas_crm(db)
    res = {"total": 0, "resolvidos": 0, "sem_descricao": 0, "por_dono": {}}
    for it in doc.get("itens") or []:
        tk = abertas.get(it.get("fone")) or []
        ok = any(t["tem_descricao"] for t in tk)
        sem_desc = bool(tk) and not ok
        it["resolvido"] = ok
        it["tarefa_sem_descricao"] = sem_desc
        d = res["por_dono"].setdefault(it.get("dono") or "sem dono", {"total": 0, "resolvidos": 0, "sem_descricao": 0})
        d["total"] += 1; res["total"] += 1
        if ok:
            d["resolvidos"] += 1; res["resolvidos"] += 1
        if sem_desc:
            d["sem_descricao"] += 1; res["sem_descricao"] += 1
    ref.set({"itens": doc.get("itens") or [], "conferencia": res,
             "conferido_em": _sp_now().isoformat(timespec="seconds")}, merge=True)
    return res

def gerar(db, dia):
    usuarios = _usuarios(db)
    leads = [x.to_dict() or {} for x in db.collection("escuta_leads").stream()]
    itens = []
    for l in leads:
        if l.get("tipo_contato") not in (None, "lead"):
            continue
        if l.get("situacao_crm") != "em_andamento" or "oportunidade_sem_tarefa" not in (l.get("furos") or []):
            continue
        crm = l.get("crm") or {}
        dono_nome = crm.get("vendedor") or (l.get("conduta") or {}).get("atendente")
        u = usuarios.get(_key(dono_nome)) if dono_nome else None
        h = l.get("horas_desde_ultima")
        itens.append({"fone": l.get("fone"), "nome": l.get("nome") or l.get("fone"), "etapa": crm.get("estagio"),
                      "dono": (u or {}).get("nome") or None, "uid": (u or {}).get("uid"),
                      "dias_parado": int((h or 0) // 24), "esperando": l.get("esperando")})
    itens.sort(key=lambda i: -(i["dias_parado"]))
    b = db.batch(); n = 0
    for it in itens:
        tid = "cad_%s_%s" % (dia, it["fone"])
        quem = ("o lead está esperando resposta" if it.get("esperando") == "equipe" else
                "parado há %d dia(s)" % it["dias_parado"])
        t = {"title": "📌 Criar tarefa no CRM: %s" % it["nome"],
             "description": "Etapa %s · %s · sem próximo passo no CRM. Abra a oportunidade, anote o que foi feito e crie a "
                            "próxima tarefa COM DATA e DESCRIÇÃO." % (it.get("etapa") or "?", quem),
             "priority": "alta", "dueTime": "12:00", "date": dia, "createdBy": "jarbas-idm", "origem": "cadencia",
             "fone": it["fone"]}
        if it.get("uid"):
            t["targetUserId"] = it["uid"]
        else:
            t["targetRole"] = "comercial"
        b.set(db.collection("tasks").document(tid), t)
        n += 1
        if n % 400 == 0:
            b.commit(); b = db.batch()
    b.commit()
    por_dono = defaultdict(int)
    for it in itens:
        por_dono[it.get("dono") or "sem dono"] += 1
    db.collection(COLL).document(dia).set({"dia": dia, "itens": itens, "por_dono": dict(por_dono),
                                           "gerado_em": _sp_now().isoformat(timespec="seconds")})
    return itens

def rodar():
    db = _fw._get_db()
    if not db:
        ST.update({"ok": False, "erro": "sem banco"}); return ST
    hoje = _sp_now().date().isoformat()
    ontem = (_sp_now().date() - dt.timedelta(days=1)).isoformat()
    try:
        conf = conferir(db, ontem)
        itens = gerar(db, hoje)
        ST.update({"ultima": _sp_now().isoformat(timespec="seconds"), "ok": True, "erro": None, "geradas": len(itens),
                   "conferidas": (conf or {}).get("total", 0), "resolvidas": (conf or {}).get("resolvidos", 0)})
        print("[cadencia] %s geradas=%d conferidas=%s" % (hoje, len(itens), conf), flush=True)
    except Exception as e:
        import traceback
        ST.update({"ok": False, "erro": repr(e)[:200]})
        print("[cadencia] erro:", traceback.format_exc(), flush=True)
    return dict(ST)

def texto_atendente(nome, itens, conf_dela=None):
    L = ["*Bom dia, %s! ☀️ Cadência do dia — IDM*" % nome.split()[0], ""]
    if conf_dela and conf_dela.get("total"):
        L.append("Ontem: %d de %d resolvidas%s." % (conf_dela["resolvidos"], conf_dela["total"],
                 (" · %d tarefa(s) criada(s) SEM descrição (não contam)" % conf_dela["sem_descricao"]) if conf_dela.get("sem_descricao") else ""))
        L.append("")
    L.append("Leads seus *sem próximo passo no CRM* (%d):" % len(itens))
    for it in itens[:25]:
        extra = "esperando você" if it.get("esperando") == "equipe" else "%dd parado" % it["dias_parado"]
        L.append("• %s — %s (%s)" % (it["nome"], it.get("etapa") or "?", extra))
    if len(itens) > 25:
        L.append("… e mais %d no IDM (Minhas Tarefas)." % (len(itens) - 25))
    L.append("")
    L.append("Para cada um: abra a oportunidade, anote o que foi feito e crie a próxima tarefa *com data e descrição*.")
    L.append("_Responda *ok* para manter este canal aberto._")
    return "\n".join(L)

def enviar_atendentes():
    import diario as _d
    db = _fw._get_db()
    hoje = _sp_now().date().isoformat()
    ontem = (_sp_now().date() - dt.timedelta(days=1)).isoformat()
    s = db.collection(COLL).document(hoje).get()
    if not s.exists:
        return {}
    itens = (s.to_dict() or {}).get("itens") or []
    so = db.collection(COLL).document(ontem).get()
    conf = ((so.to_dict() or {}).get("conferencia") or {}).get("por_dono", {}) if so.exists else {}
    por = defaultdict(list)
    for it in itens:
        if it.get("dono"):
            por[it["dono"]].append(it)
    res = {}
    for dono, lst in por.items():
        fone = FONES.get(_key(dono))
        if not fone:
            res[dono] = {"ok": False, "erro": "sem telefone cadastrado"}
            continue
        res[dono] = _d.enviar(texto_atendente(dono, lst, conf.get(dono)), fone)
    ST["envios"] = {k: {"ok": v.get("ok"), "erro": v.get("erro")} for k, v in res.items()}
    db.collection(COLL).document(hoje).set({"envios": ST["envios"]}, merge=True)
    return res

def iniciar(sched):
    if sched is None or os.environ.get("CADENCIA_ON", "1") == "0":
        return
    sched.add_job(rodar, "cron", hour=6, minute=35)
    sched.add_job(enviar_atendentes, "cron", hour=7, minute=0)
    print("[cadencia] agendada 06h35 (gera/confere) + 07h00 (WhatsApp das atendentes)", flush=True)
