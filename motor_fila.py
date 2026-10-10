#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IDM Escuta — Motor da Fila de Prioridade (núcleo). SÓ LEITURA na API oficial do CE.
Sem dependência de 'curl': usa urllib com User-Agent de navegador (Cloudflare).
"""
import json, os, urllib.request, urllib.error, datetime as dt

BASE = "https://api.clinicaexperts.com.br/api/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")

def _load_key():
    k = os.environ.get("CE_API_KEY")
    if k:
        return k.strip()
    for p in (os.environ.get("CE_KEY_FILE"),
              os.path.expanduser("~/mnt/instituto-mafra/_credenciais/clinic-expert.json")):
        if p and os.path.isfile(p):
            return json.load(open(p))["api_key"]
    raise RuntimeError("Chave da API não encontrada (defina CE_API_KEY no ambiente).")

KEY = _load_key()

def api(path):
    url = path if path.startswith("http") else f"{BASE}/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}", "User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[api] erro", path, e)
        return {}

def api_all(path, cap_pages=40):
    out, pg = [], 1
    sep = "&" if "?" in path else "?"
    while pg <= cap_pages:
        d = api(f"{path}{sep}page={pg}")
        out += d.get("data", []) or []
        m = d.get("meta") or {}
        if not m or m.get("page", pg) >= m.get("last_page", pg):
            break
        pg += 1
    return out

STAGE_ORDER = {"Entrada":0,"Desqualificado":1,"Qualificação":2,"Agendamento":3,"No-Show":4,
    "Reagendamento":5,"Orçamento":6,"Recuperação N1":7,"Recuperação N2":8,"Recuperação N3":9,
    "Ganho - Novo Cliente":10,"Perdido":11}
ATIVO   = {"Entrada","Qualificação","Agendamento"}
RESGATE = {"No-Show","Reagendamento","Recuperação N1","Recuperação N2","Recuperação N3"}
FECHADO = {"Desqualificado","Ganho - Novo Cliente","Perdido"}

def _norm(p): return (p or "").translate(str.maketrans("", "", "+-() "))

def _classifica(L):
    st, flags, score = L["stage"], [], 0
    if st in ATIVO:
        score += 50
        if st in ("Entrada","Qualificação") and not L["tem_agenda_futura"]:
            flags.append("AGIR: lead ativo sem avaliação agendada"); score += 40
        if st == "Agendamento" and L["tem_agenda_futura"]:
            flags.append("OK: avaliação agendada")
    elif st in RESGATE:
        score += 35; flags.append(f"RESGATE: etapa {st} — reengajar")
    elif st in FECHADO:
        score -= 20
    if L["eh_paciente"] and not (L["tem_agenda_futura"] or L["tem_agenda_passada"]):
        flags.append("ERRO: consta como Paciente sem nenhum agendamento"); score += 25
    if L["tem_agenda_passada"] and not L["tem_agenda_futura"] and st in ("Qualificação","No-Show","Agendamento"):
        flags.append("AGIR: compareceu/faltou e não há novo agendamento"); score += 30
    if L["venda"] > 0:
        flags.append(f"FINANCEIRO: venda R$ {L['venda']/100:,.2f}")
    score += (L.get("priority") or 0) * 3
    L["flags"], L["score"] = flags, score
    return L

def run_motor(enrich_patients=True):
    hoje = dt.date.today()
    ini = hoje - dt.timedelta(days=14)
    fim = hoje + dt.timedelta(days=21)
    iso_ini, iso_fim = f"{ini}T00:00:00-03:00", f"{fim}T23:59:59-03:00"

    ops = [o for o in api_all("crm/opportunities?sort_column=created_at&sort_direction=desc")
           if ((o.get("pipeline") or {}).get("name") == "LEADS")]
    bookings = api_all(f"bookings?starts_at={iso_ini}&ends_at={iso_fim}")
    ag, ag_fut, ag_pas = {}, set(), set()
    for b in bookings:
        p = b.get("patient") or {}
        chave = _norm(p.get("phone")) or (p.get("name") or "").strip().lower()
        if not chave: continue
        w = (b.get("starts_at") or "")[:10]
        ag.setdefault(chave, []).append(w)
        (ag_fut if w >= str(hoje) else ag_pas).add(chave)
    sales = api_all(f"sales?starts_at={iso_ini}&ends_at={iso_fim}")
    venda = {}
    for s in sales:
        p = s.get("patient") or s.get("buyer") or {}
        chave = _norm(p.get("phone")) or (p.get("name") or "").strip().lower()
        if chave: venda[chave] = venda.get(chave, 0) + (s.get("final_amount") or 0)

    leads = []
    for o in ops:
        p = o.get("patient") or {}
        phone, tags = None, None
        if enrich_patients and p.get("uuid"):
            pat = api(f"patients/{p['uuid']}"); pd = pat.get("data") or pat
            if isinstance(pd, dict):
                phone, tags = pd.get("phone"), pd.get("tags")
        chave = _norm(phone) or (p.get("name") or "").strip().lower()
        L = {"nome": p.get("name"), "telefone": phone or "(só no WhatsApp)",
             "etapa": (o.get("stage") or {}).get("name") or "?",
             "stage": (o.get("stage") or {}).get("name") or "?",
             "vendedor": (o.get("seller") or {}).get("name"),
             "origem_crm": o.get("origin"),
             "tag_cadastro": ", ".join(t.get("name","") if isinstance(t,dict) else str(t) for t in (tags or [])) or "—",
             "priority": o.get("priority"), "obs_crm": o.get("observations"),
             "opp_uuid": o.get("uuid"),
             "eh_paciente": bool(phone),
             "tem_agenda_futura": chave in ag_fut, "tem_agenda_passada": chave in ag_pas,
             "agenda": sorted(ag.get(chave, [])), "venda": venda.get(chave, 0)}
        leads.append(_classifica(L))

    leads.sort(key=lambda x: (-x["score"], STAGE_ORDER.get(x["stage"], 99)))
    return {
        "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "total_leads_pipeline": len(leads),
        "resumo": {
            "ativos_sem_agenda": sum(1 for L in leads if "AGIR: lead ativo sem avaliação agendada" in L["flags"]),
            "erros_cadastro": sum(1 for L in leads if any("ERRO" in x for x in L["flags"])),
            "em_resgate": sum(1 for L in leads if any("RESGATE" in x for x in L["flags"])),
        },
        "fila": leads,
    }

if __name__ == "__main__":
    out = run_motor()
    print(f"[motor] {out['total_leads_pipeline']} leads | {out['resumo']}")
