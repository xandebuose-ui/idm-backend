# -*- coding: utf-8 -*-
"""
IDM - Diario em IMAGEM (30/10/2026). Desenha um PNG 1080px de largura a partir de `dados` do diario.gerar().
Pillow puro (sem navegador). Fontes Inter (OFL) na pasta fonts/. Sem emoji (a fonte nao tem): icones = formas.
"""
import os, io

from PIL import Image, ImageDraw, ImageFont

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_F = {}

def _font(peso, tam):
    k = (peso, tam)
    if k not in _F:
        arq = {"r": "Inter-Regular.otf", "s": "Inter-SemiBold.otf", "b": "Inter-ExtraBold.otf"}[peso]
        _F[k] = ImageFont.truetype(os.path.join(_DIR, arq), tam)
    return _F[k]

BG = (238, 241, 238); CARD = (255, 255, 255); FG = (23, 32, 28); MUT = (94, 107, 100); LINE = (230, 235, 231)
BRAND = (15, 91, 78); OK = (46, 139, 87); MID = (184, 134, 28); BAD = (198, 71, 44); BLUE = (47, 111, 159)
SOFT = {"bad": (253, 232, 226), "mid": (251, 241, 220), "blue": (228, 238, 246), "ok": (227, 242, 234), "bar": (238, 242, 239)}
DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
W, PAD = 1080, 36


def _num(v, casas=1):
    if v is None:
        return "—"
    s = ("%." + str(casas) + "f") % float(v)
    return s.replace(".", ",")

def _brl(v):
    return "R$ " + ("{:,.0f}".format(v or 0)).replace(",", ".")

def _cor_nota(n):
    return OK if n >= 4.5 else (MID if n >= 3 else BAD)

def _wrap(d, txt, fonte, larg):
    linhas, cur = [], ""
    for w in str(txt or "").split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fonte) <= larg:
            cur = t
        else:
            if cur:
                linhas.append(cur)
            cur = w
    if cur:
        linhas.append(cur)
    return linhas or [""]


def render(dados, dia_iso, gerado_txt=""):
    import datetime as _dt
    dd = _dt.date.fromisoformat(dia_iso)
    img = Image.new("RGB", (W, 4000), BG)
    d = ImageDraw.Draw(img)
    y = PAD

    def card(y0, h, borda=None):
        d.rounded_rectangle([PAD, y0, W - PAD, y0 + h], radius=22, fill=CARD)
        if borda:
            d.rounded_rectangle([PAD, y0, PAD + 10, y0 + h], radius=5, fill=borda)

    # cabecalho
    d.rounded_rectangle([PAD, y, W - PAD, y + 190], radius=28, fill=BRAND)
    d.text((PAD + 40, y + 32), "IDM  ·  DIÁRIO COMERCIAL", font=_font("s", 22), fill=(190, 214, 207))
    d.text((PAD + 40, y + 66), "%s, %s" % (DIAS[dd.weekday()], dd.strftime("%d/%m")), font=_font("b", 54), fill=(255, 255, 255))
    d.text((PAD + 40, y + 140), gerado_txt or "dados do banco do IDM", font=_font("r", 21), fill=(190, 214, 207))
    y += 190 + 18

    # KPIs
    kw = (W - 2 * PAD - 3 * 16) // 4
    t1 = dados.get("t1_mediana_min")
    kpis = [("Leads novos", str(dados.get("leads_novos", 0)), FG, "%d conversas · %d cliente(s)" % (dados.get("chegadas", 0), dados.get("clientes_voltaram", 0))),
            ("Custo por lead", _brl(dados.get("cpl")) if dados.get("cpl") else "—", FG,
             "Meta: R$ %s · %d conv." % (_num(dados.get("meta_gasto"), 2), dados.get("meta_conversas", 0))),
            ("1ª resposta", ("%d min" % int(t1)) if t1 is not None else "—", OK if (t1 or 99) <= 15 else (MID if (t1 or 99) <= 60 else BAD),
             "mediana · %d acima de 1h" % dados.get("t1_acima_1h", 0)),
            ("Vendas", str(dados.get("vendas", 0)), OK if dados.get("vendas") else BAD, "%d orçamento(s)" % dados.get("orcamentos", 0))]
    for i, (lab, val, cor, sub) in enumerate(kpis):
        x = PAD + i * (kw + 16)
        d.rounded_rectangle([x, y, x + kw, y + 170], radius=22, fill=CARD)
        d.text((x + 22, y + 22), lab, font=_font("s", 21), fill=MUT)
        fv = _font("b", 50 if d.textlength(val, font=_font("b", 50)) < kw - 40 else 38)
        d.text((x + 22, y + 56), val, font=fv, fill=cor)
        for j, ln in enumerate(_wrap(d, sub, _font("r", 18), kw - 40)[:2]):
            d.text((x + 22, y + 118 + j * 22), ln, font=_font("r", 18), fill=MUT)
    y += 170 + 18

    # furos
    furos = [("Leads SEM próximo passo no CRM", dados.get("sem_tarefa", 0), dados.get("sem_tarefa_nomes") or "", BAD),
             ("Leads esperando resposta (agir hoje)", dados.get("sem_resposta", 0), dados.get("sem_resposta_nomes") or "", MID),
             ("Leads sem resposta há mais de 48h", dados.get("sem_resposta_48h_mais", 0), "esfriando", BAD),
             ("Clientes esperando resposta", dados.get("sem_resposta_clientes", 0), "", MID),
             ("Novos de ontem sem oportunidade no CRM", dados.get("sem_oportunidade", 0), "", BAD)]
    linhas = []
    for lab, n, sub, cor in furos:
        subl = _wrap(d, sub, _font("r", 19), W - 2 * PAD - 220)[:2] if sub else []
        linhas.append((lab, n, subl, cor if n else OK))
    h = 80 + sum(54 + 24 * len(s) for _, _, s, _ in linhas)
    card(y, h, BAD)
    d.text((PAD + 40, y + 26), "Furos para cobrar hoje", font=_font("b", 27), fill=FG)
    yy = y + 76
    for k, (lab, n, subl, cor) in enumerate(linhas):
        if k:
            d.line([PAD + 40, yy - 8, W - PAD - 30, yy - 8], fill=LINE, width=2)
        d.text((PAD + 40, yy + 4), lab, font=_font("r", 25), fill=FG)
        nt = str(n)
        d.text((W - PAD - 30 - d.textlength(nt, font=_font("b", 32)), yy), nt, font=_font("b", 32), fill=cor)
        for j, ln in enumerate(subl):
            d.text((PAD + 40, yy + 38 + j * 24), ln, font=_font("r", 19), fill=MUT)
        yy += 54 + 24 * len(subl)
    y += h + 18

    # notas
    notas = dados.get("notas_atendente") or []
    if notas:
        h = 90 + 50 * len(notas[:6]) + 10
        card(y, h)
        d.text((PAD + 30, y + 24), "Nota de abordagem", font=_font("b", 27), fill=FG)
        d.text((PAD + 30 + d.textlength("Nota de abordagem", font=_font("b", 27)) + 14, y + 31), "0 a 6 · conversas de ontem",
               font=_font("r", 20), fill=MUT)
        yy = y + 80
        for nt in notas[:6]:
            n = float(nt.get("nota") or 0)
            d.text((PAD + 30, yy), "%s (%d)" % (nt.get("atendente"), nt.get("conversas") or 0), font=_font("s", 24), fill=FG)
            bx0, bx1 = PAD + 250, W - PAD - 120
            d.rounded_rectangle([bx0, yy + 4, bx1, yy + 30], radius=13, fill=SOFT["bar"])
            d.rounded_rectangle([bx0, yy + 4, bx0 + max(26, int((bx1 - bx0) * n / 6.0)), yy + 30], radius=13, fill=_cor_nota(n))
            v = _num(n)
            d.text((W - PAD - 30 - d.textlength(v, font=_font("b", 28)), yy - 2), v, font=_font("b", 28), fill=_cor_nota(n))
            yy += 50
        y += h + 18

    # termometro
    c = dados.get("termometro") or {}
    pend = dados.get("termometro_pendentes") or {}
    if c:
        h = 230
        card(y, h)
        d.text((PAD + 30, y + 24), "Termômetro agora", font=_font("b", 27), fill=FG)
        tw = (W - 2 * PAD - 60 - 3 * 12) // 4
        tiles = [("Quentes", "quente", BAD, "bad"), ("Mornos", "morno", MID, "mid"), ("Frios", "frio", BLUE, "blue"), ("Agendados", "agendado", OK, "ok")]
        for i, (lab, k, cor, soft) in enumerate(tiles):
            x = PAD + 30 + i * (tw + 12)
            d.rounded_rectangle([x, y + 76, x + tw, y + 206], radius=18, fill=SOFT[soft])
            v = str(c.get(k, 0))
            d.text((x + (tw - d.textlength(v, font=_font("b", 46))) / 2, y + 86), v, font=_font("b", 46), fill=cor)
            d.text((x + (tw - d.textlength(lab, font=_font("s", 21))) / 2, y + 142), lab, font=_font("s", 21), fill=FG)
            if k in pend:
                p = "%d a agir" % pend.get(k, 0)
                d.text((x + (tw - d.textlength(p, font=_font("r", 17))) / 2, y + 172), p, font=_font("r", 17), fill=MUT)
        y += h + 18

    # cadencia
    cad = dados.get("cadencia_hoje") or {}
    if cad:
        txt = " · ".join("%s %d" % (k if str(k).startswith("sem") else str(k).split()[0], v) for k, v in sorted(cad.items(), key=lambda x: -x[1]))
        linhas = _wrap(d, txt, _font("r", 23), W - 2 * PAD - 60)[:3]
        h = 84 + 30 * len(linhas)
        card(y, h)
        d.text((PAD + 30, y + 24), "Cadência de hoje (por dono)", font=_font("b", 27), fill=FG)
        for j, ln in enumerate(linhas):
            d.text((PAD + 30, y + 70 + j * 30), ln, font=_font("r", 23), fill=FG)
        y += h + 18

    # rodape
    al = dados.get("alertas") or []
    saude = "Todas as fontes OK" if not al else ("Atenção: " + str(al[0])[:60])
    d.ellipse([PAD + 6, y + 10, PAD + 22, y + 26], fill=OK if not al else BAD)
    d.text((PAD + 32, y + 4), saude, font=_font("r", 20), fill=MUT)
    r = "Responda ok para manter o canal aberto"
    d.text((W - PAD - 6 - d.textlength(r, font=_font("r", 20)), y + 4), r, font=_font("r", 20), fill=MUT)
    y += 48

    img = img.crop((0, 0, W, y))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
