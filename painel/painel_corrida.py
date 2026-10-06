#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel de Controle · Corrida para a Presidência
===============================================

Programa de mesa (Tkinter, só biblioteca padrão do Python) para acompanhar,
no dia da apuração, se tudo está funcionando:

  * lê DIRETAMENTE o arquivo oficial do TSE (presidente, 2º turno, 2026);
  * consulta o site publicado (Worker na Cloudflare) e confere se ele está
    respondendo e lendo o TSE;
  * mostra a última atualização dos dados, o andamento da apuração e os
    totais (eleitorado, comparecimento, válidos, brancos, nulos);
  * mostra a classificação dos 2 candidatos, que se reordena sozinha, com
    animação, à medida que os votos chegam (setas indicam quem subiu/desceu);
  * luzes de estado: VERDE = funcionando, AMARELO = atenção, VERMELHO = erro;
  * botões para abrir o site e a página oficial do TSE no navegador.

Uso:
    python painel_corrida.py                 # modo normal (TSE + site)
    python painel_corrida.py --intervalo 20  # a partir das 17h, consulta a cada 20 s (mínimo 10)
    (antes das 16h30 só faz uma checagem ao abrir; das 16h30 às 17h checa a cada 60 s)
    python painel_corrida.py --arquivo x.json  # lê um JSON local no lugar do TSE (só para teste do painel)

Requisitos: Python 3.8+ com Tkinter (já vem no instalador oficial do Windows e
do macOS; no Linux: sudo apt install python3-tk).
"""

import argparse
import json
import math
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone, timedelta

import tkinter as tk
from tkinter import font as tkfont

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
URL_TSE = "https://resultados.tse.jus.br/oficial/ele2026/6258/dados/br/br-c0001-e006258-u.json"  # 2º turno (eleição 6258)
URL_TSE_PAGINA = "https://resultados.tse.jus.br"
URL_SITE = "https://corridaparapresidencia.eleicoes.workers.dev"
WORKER_NOME = "corridaparapresidencia"     # nome do Worker na Cloudflare
COTA_DIA = 100_000                         # requisições/dia do plano grátis (zera 00h UTC = 21h de Brasília)
CONSULTAS_POR_MINUTO = 2                   # cada página aberta consulta o servidor a cada 30 s
GQL_URL = "https://api.cloudflare.com/client/v4/graphql"
GQL_INTERVALO = 60                         # segundos entre consultas às estatísticas da Cloudflare
# Token e Account ID da Cloudflare: arquivo painel_config.json (ao lado deste programa) ou
# variáveis de ambiente CF_API_TOKEN e CF_ACCOUNT_ID. O token precisa só de "Account Analytics: Read".
ARQ_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "painel_config.json")


def ler_config():
    cfg = {}
    try:
        with open(ARQ_CONFIG, encoding="utf-8-sig") as f:  # utf-8-sig: aceita arquivo salvo "com BOM" (Bloco de Notas)
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(cfg, dict):  # ex.: arquivo com [] ou null
        cfg = {}
    tok = (os.environ.get("CF_API_TOKEN") or str(cfg.get("cf_api_token", ""))).strip()
    acc = (os.environ.get("CF_ACCOUNT_ID") or str(cfg.get("cf_account_id", ""))).strip()
    # valores de exemplo ("COLE_...") contam como não preenchidos
    if tok.upper().startswith("COLE"):
        tok = ""
    if acc.upper().startswith("COLE"):
        acc = ""
    return tok, acc


# Requisições que o PRÓPRIO painel faz ao site: descontadas da estimativa de público
_proprias = []
_proprias_lock = threading.Lock()


def marcar_propria():
    with _proprias_lock:
        _proprias.append(time.time())
        corte = time.time() - 3 * 3600
        while _proprias and _proprias[0] < corte:
            _proprias.pop(0)


def proprias_entre(t0, t1):
    with _proprias_lock:
        return sum(1 for t in _proprias if t0 <= t <= t1)


def consultar_publico(token, conta):
    """Lê da Cloudflare quantas requisições o Worker recebeu e estima o público.

    Pessoas online ≈ requisições por minuto ÷ 2 (cada página aberta consulta 2×/min),
    numa janela de 5 min que termina 2 min atrás (as estatísticas chegam com ~1–2 min de atraso).
    """
    agora = datetime.now(timezone.utc)
    fim = agora - timedelta(minutes=2)
    ini = fim - timedelta(minutes=5)
    dia = agora.replace(hour=0, minute=0, second=0, microsecond=0)  # a cota grátis zera às 00h UTC
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    q = """query($acc:String!,$w:String!,$a:Time!,$b:Time!,$d:Time!,$z:Time!){viewer{accounts(filter:{accountTag:$acc}){
      janela:workersInvocationsAdaptive(limit:100,filter:{scriptName:$w,datetime_geq:$a,datetime_leq:$b}){sum{requests errors}}
      dia:workersInvocationsAdaptive(limit:100,filter:{scriptName:$w,datetime_geq:$d,datetime_leq:$z}){sum{requests errors}}}}}"""
    corpo = json.dumps({"query": q, "variables": {"acc": conta, "w": WORKER_NOME, "a": iso(ini), "b": iso(fim), "d": iso(dia), "z": iso(agora)}}).encode()
    req = urllib.request.Request(GQL_URL, data=corpo, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
            dados = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        return {"ok": False, "erro": f"Cloudflare HTTP {ex.code}" + (" (token sem permissão?)" if ex.code in (401, 403) else "")}
    except Exception as ex:
        return {"ok": False, "erro": "sem conexão com a Cloudflare: " + str(getattr(ex, "reason", ex))[:80]}
    if dados.get("errors"):
        return {"ok": False, "erro": "Cloudflare: " + str(dados["errors"][0].get("message", ""))[:120]}
    try:
        contas = dados["data"]["viewer"]["accounts"]
        if not contas:
            return {"ok": False, "erro": "Account ID não encontrado para este token"}
        soma = lambda linhas, k: sum((l.get("sum") or {}).get(k, 0) or 0 for l in (linhas or []))
        jan, d = contas[0].get("janela"), contas[0].get("dia")
        req_jan = soma(jan, "requests") - proprias_entre(ini.timestamp(), fim.timestamp())
        online = max(0, req_jan) / 5 / CONSULTAS_POR_MINUTO
        return {"ok": True, "online": online, "req_dia": soma(d, "requests"), "erros_dia": soma(d, "errors"),
                "jan_ini": ini.astimezone(BRT).strftime("%H:%M"), "jan_fim": fim.astimezone(BRT).strftime("%H:%M")}
    except (KeyError, TypeError) as ex:
        return {"ok": False, "erro": "resposta inesperada da Cloudflare: " + str(ex)[:80]}
INICIO_UTC = datetime(2026, 10, 25, 20, 0, 0, tzinfo=timezone.utc)  # 2º turno: 25/10, 17h de Brasília
BRT = timezone(timedelta(hours=-3))
INTERVALO_PADRAO = 30          # segundos entre consultas, a partir das 17h
AQUECIMENTO_MIN = 30           # minutos antes das 17h em que começam as checagens de "está tudo respondendo"
INTERVALO_AQUECIMENTO = 60     # segundos entre checagens nesse período (16h30 às 17h)
# Fases (pelo relógio do computador):
#   antes das 16h30 → uma checagem ao abrir o programa e depois só pelo botão "Atualizar agora";
#   16h30 às 17h    → checagem a cada 60 s (TSE e site respondendo);
#   a partir das 17h → consulta do TSE a cada --intervalo segundos.
TIMEOUT = 15                   # segundos por requisição
SEM_ATUALIZAR_ALERTA = 10 * 60  # após o início: sem dado novo por 10 min → amarelo

# Candidatos (mesmas cores do site): número, nome, partido, cor da camisa, cor do número
CANDS = [  # 2º turno
    (13, "Lula", "PT", "#E11D2E", "#FFFFFF"),
    (22, "Flávio Bolsonaro", "PL", "#2A5BE8", "#FFFFFF"),
]
INFO = {c[0]: c for c in CANDS}

# Paleta da interface (tema escuro, como o site)
COR = {
    "fundo": "#0a1122", "card": "#111a30", "card2": "#16213b", "linha": "#24304d",
    "texto": "#eef2f8", "fraco": "#9fb0c8", "ouro": "#ffd23f",
    "ok": "#2ecc71", "atencao": "#f5b829", "erro": "#ff4d4f", "neutro": "#5b6b88",
    "barra": "#2f6fe0",
}


# --------------------------------------------------------------------------
# Leitura e normalização dos dados do TSE (mesma lógica do Worker do site)
# --------------------------------------------------------------------------
def num(v):
    """Converte texto do TSE em número: '41,23' → 41.23; '9075260' → 9075260."""
    if v is None or v == "":
        return float("nan")
    s = str(v).strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def ok_num(x):
    """Número de verdade (descarta None, texto, NaN e infinito)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def coletar_candidatos(no, saida):
    """Percorre carg > agr > par > cand, qualquer que seja o aninhamento."""
    if isinstance(no, list):
        for item in no:
            coletar_candidatos(item, saida)
    elif isinstance(no, dict):
        if isinstance(no.get("cand"), list):
            saida.extend(no["cand"])
        for k, v in no.items():
            if k != "cand":
                coletar_candidatos(v, saida)


def normalizar(bruto):
    """Transforma o JSON bruto (-u.json) em um dicionário simples para o painel."""
    cargos = bruto.get("carg") or []
    cargo = next((c for c in cargos if str(c.get("cd")) == "1"), cargos[0] if cargos else None)
    lista = []
    coletar_candidatos([cargo] if cargo else [], lista)

    s, e, v = bruto.get("s") or {}, bruto.get("e") or {}, bruto.get("v") or {}

    def pct(a, b):
        x = num(a)
        if ok_num(x) and 0 <= x <= 100:
            return x
        y = num(b)
        return y if ok_num(y) and 0 <= y <= 100 else float("nan")

    cands = []
    for c in lista:
        try:
            n = int(str(c.get("n")))
        except ValueError:
            continue
        vap = num(c.get("vap"))
        if not ok_num(vap):
            continue
        cands.append({
            "n": n, "nm": c.get("nmu") or c.get("nm") or "", "vap": vap,
            "pvap": pct(c.get("pvap"), c.get("pvapn")),
            "st": c.get("st") or ("Eleito" if c.get("e") == "s" else ""),
        })
    base = num(v.get("vvc")) if ok_num(num(v.get("vvc"))) and num(v.get("vvc")) > 0 else sum(c["vap"] for c in cands)
    for c in cands:  # percentual ausente → calcula pelos votos válidos
        if not ok_num(c["pvap"]):
            c["pvap"] = 100 * c["vap"] / base if base > 0 else 0.0

    pst = pct(s.get("pst"), s.get("pstn"))
    if not ok_num(pst) and num(s.get("ts")) > 0:
        pst = 100 * num(s.get("st")) / num(s.get("ts"))

    gerado = " ".join(x for x in (bruto.get("dt"), bruto.get("ht")) if x)
    gerado_dg = " ".join(x for x in (bruto.get("dg"), bruto.get("hg")) if x)
    return {
        "pst": pst, "ts": num(s.get("ts")), "st": num(s.get("st")),
        "te": num(e.get("te")), "est": num(e.get("est")),
        "comp": num(e.get("c")), "abst": num(e.get("a")),
        "vv": num(v.get("vv")), "vvc": num(v.get("vvc")), "vb": num(v.get("vb")),
        "vn": num(v.get("vn")), "tv": num(v.get("tv")),
        "gerado": gerado or gerado_dg, "gerado_tot": gerado,  # dt/ht = totalização; dg/hg = geração do arquivo
        "final": bruto.get("tf") == "s",
        "cands": cands,
    }


# --------------------------------------------------------------------------
# Rede: requisições em uma thread separada (a janela nunca congela)
# --------------------------------------------------------------------------
CTX = ssl.create_default_context()


def baixar_json(url):
    """Retorna (dados, ms, codigo_http, erro_texto)."""
    t0 = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": "PainelCorrida/1.0 (+acompanhamento)", "Accept": "application/json",
        "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
            corpo = r.read()
            ms = int((time.time() - t0) * 1000)
            try:
                return json.loads(corpo.decode("utf-8")), ms, r.status, None
            except ValueError:
                return None, ms, r.status, "resposta não é JSON"
    except urllib.error.HTTPError as ex:
        return None, int((time.time() - t0) * 1000), ex.code, f"HTTP {ex.code}"
    except Exception as ex:  # sem internet, DNS, timeout, certificado...
        msg = str(getattr(ex, "reason", ex))
        if "timed out" in msg or isinstance(ex, TimeoutError):
            msg = "tempo esgotado (%d s)" % TIMEOUT
        return None, int((time.time() - t0) * 1000), None, "sem conexão: " + msg[:110]


def ciclo_de_consulta(fila, arquivo_local):
    """Faz uma rodada completa: TSE + site. Coloca o resultado na fila da interface (sempre, mesmo com erro)."""
    try:
        _ciclo(fila, arquivo_local)
    except Exception as ex:
        fila.put({"quando": time.time(),
                  "tse": {"ok": False, "ms": 0, "http": None, "dados": None, "erro": "falha interna: " + repr(ex)[:100]},
                  "site": {"status": None, "ms": 0, "http": None, "erro": "não consultado", "verificar": None, "ms_ver": 0,
                           "http_ver": None, "erro_ver": "não consultado", "resultado": None, "ms_res": 0, "erro_res": "não consultado"}})


def _ciclo(fila, arquivo_local):
    res = {"quando": time.time()}

    # 1) TSE (ou arquivo local, no modo de teste do painel)
    if arquivo_local:
        try:
            with open(arquivo_local, encoding="utf-8") as f:
                bruto = json.load(f)
            res["tse"] = {"ok": True, "ms": 0, "http": 200, "dados": normalizar(bruto), "erro": None}
        except Exception as ex:
            res["tse"] = {"ok": False, "ms": 0, "http": None, "dados": None, "erro": str(ex)}
    else:
        bruto, ms, http, erro = baixar_json(URL_TSE)
        dados = None
        if bruto is not None:
            try:
                dados = normalizar(bruto)
                if not dados["cands"]:
                    erro, dados = "formato inesperado (sem candidatos)", None
            except Exception as ex:
                erro = "falha ao interpretar: " + str(ex)[:100]
        res["tse"] = {"ok": dados is not None, "ms": ms, "http": http, "dados": dados, "erro": erro}

    # 2) Site: /api/status (o Worker está no ar?) e /api/verificar (o Worker consegue ler o TSE?)
    marcar_propria(); st, ms1, http1, e1 = baixar_json(URL_SITE + "/api/status")
    marcar_propria(); ver, ms2, http2, e2 = baixar_json(URL_SITE + "/api/verificar")
    res["site"] = {
        "status": st, "ms": ms1, "http": http1, "erro": e1,
        "verificar": ver, "ms_ver": ms2, "http_ver": http2, "erro_ver": e2,
    }
    # 3) O que a página do site está recebendo de fato (depois das 17h)
    marcar_propria(); rs, ms3, http3, e3 = baixar_json(URL_SITE + "/api/resultado")
    res["site"].update({"resultado": rs, "ms_res": ms3, "erro_res": e3})
    fila.put(res)


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
def escolher_fonte(raiz, preferidas):
    disponiveis = set(tkfont.families(raiz))
    for f in preferidas:
        if f in disponiveis:
            return f
    return "TkDefaultFont"


def fmt_int(x):
    return "—" if not ok_num(x) else f"{int(round(x)):,}".replace(",", ".")


def compacto(v):
    """1234567 → '1,2 mi'; 850000 → '850 mil' (rótulos curtos de eixo)."""
    v = abs(v)
    if v >= 1e6:
        return (f"{v/1e6:.0f}" if v >= 1e7 else f"{v/1e6:.1f}").replace(".", ",") + " mi"
    return f"{v/1e3:.0f} mil" if v >= 1e3 else f"{v:.0f}"


def fmt_pct(x, casas=2):
    return "—" if not ok_num(x) else f"{x:.{casas}f}".replace(".", ",") + "%"


class Luz:
    """Indicador redondo de estado (verde/amarelo/vermelho/cinza) com título e detalhe."""

    def __init__(self, pai, titulo, fontes):
        self.frame = tk.Frame(pai, bg=COR["card"], highlightthickness=1, highlightbackground=COR["linha"])
        self.cv = tk.Canvas(self.frame, width=34, height=34, bg=COR["card"], highlightthickness=0)
        self.cv.grid(row=0, column=0, rowspan=2, padx=(12, 8), pady=10)
        self.bola = self.cv.create_oval(4, 4, 30, 30, fill=COR["neutro"], outline="")
        self.brilho = self.cv.create_oval(10, 9, 17, 15, fill="#ffffff", outline="", stipple="gray50")
        tk.Label(self.frame, text=titulo, bg=COR["card"], fg=COR["fraco"], font=fontes["peq_b"]).grid(row=0, column=1, sticky="sw", pady=(12, 0))
        self.txt = tk.Label(self.frame, text="aguardando…", bg=COR["card"], fg=COR["texto"], font=fontes["linha_nome"], anchor="w")
        self.txt.grid(row=1, column=1, sticky="nw")
        self.det = tk.Label(self.frame, text="", bg=COR["card"], fg=COR["fraco"], font=fontes["peq"], anchor="w", justify="left", wraplength=300)
        self.det.grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 12))
        self.frame.grid_columnconfigure(1, weight=1)
        self.frame.bind("<Configure>", lambda e: self.det.configure(wraplength=max(120, e.width - 28)))

    def definir(self, nivel, texto, detalhe=""):
        cor = {"ok": COR["ok"], "atencao": COR["atencao"], "erro": COR["erro"]}.get(nivel, COR["neutro"])
        self.cv.itemconfigure(self.bola, fill=cor)
        self.txt.configure(text=texto, fg=cor if nivel in ("ok", "atencao", "erro") else COR["texto"])
        self.det.configure(text=detalhe)


class Painel:
    def __init__(self, raiz, intervalo, arquivo_local):
        self.raiz, self.intervalo, self.arquivo_local = raiz, intervalo, arquivo_local
        self.fila = queue.Queue()
        self.ocupado = False
        self.prox = time.time()
        self.ultimo_gerado, self.quando_mudou = None, None
        self.pos_anterior = {}   # posição de cada candidato na rodada anterior (setas ↑↓)
        self.tendencia = {}      # última mudança de posição de cada candidato
        self.y_atual = {}        # posição vertical animada de cada linha da classificação
        self.dados = None
        # estatísticas de público (Cloudflare): opcional, só se houver token configurado
        self.cf_token, self.cf_conta = ler_config()
        self.pub_fila = queue.Queue()
        self.pub_ocupado = False
        self.pub_prox = time.time() + 1

        fam = escolher_fonte(raiz, ["Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Cantarell", "DejaVu Sans", "Arial"])
        mono = escolher_fonte(raiz, ["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Courier New"])
        self.f = {
            "titulo": (fam, 19, "bold"), "sub": (fam, 11), "peq": (fam, 10), "peq_b": (fam, 10, "bold"),
            "med": (fam, 12), "med_b": (fam, 13, "bold"), "grande": (fam, 30, "bold"), "num": (fam, 16, "bold"),
            "linha_nome": (fam, 12, "bold"), "linha_num": (fam, 12), "mono": (mono, 9),
        }
        raiz.title("Painel de Controle · Corrida para a Presidência")
        raiz.configure(bg=COR["fundo"])
        sw, sh = raiz.winfo_screenwidth(), raiz.winfo_screenheight()
        raiz.geometry(f"{min(1360, sw - 20)}x{min(840, sh - 60)}+10+10")  # cabe em telas de notebook (1366x768)
        raiz.minsize(1000, 640)
        self._montar()
        self.raiz.after(200, self._loop)
        self.raiz.after(30, self._animar)
        self._registrar("Painel iniciado. Checagem inicial agora; checagens automáticas a cada %d s entre 16h30 e 17h; a cada %d s a partir das 17h." % (INTERVALO_AQUECIMENTO, self.intervalo))

    # ---------------- montagem da tela ----------------
    def _card(self, pai, titulo):
        fr = tk.Frame(pai, bg=COR["card"], highlightthickness=1, highlightbackground=COR["linha"])
        tk.Label(fr, text=titulo.upper(), bg=COR["card"], fg=COR["fraco"], font=self.f["peq_b"]).pack(anchor="w", padx=16, pady=(12, 4))
        return fr

    def _montar(self):
        C = COR
        # Cabeçalho
        topo = tk.Frame(self.raiz, bg=C["fundo"])
        topo.pack(fill="x", padx=18, pady=(14, 8))
        esq = tk.Frame(topo, bg=C["fundo"]); esq.pack(side="left")
        tk.Label(esq, text="PAINEL DE CONTROLE", bg=C["fundo"], fg=C["ouro"], font=self.f["peq_b"]).pack(anchor="w")
        tk.Label(esq, text="Corrida para a Presidência · Eleições 2026, 2º turno", bg=C["fundo"], fg=C["texto"], font=self.f["titulo"]).pack(anchor="w")
        self.lbl_inicio = tk.Label(esq, text="", bg=C["fundo"], fg=C["fraco"], font=self.f["sub"]); self.lbl_inicio.pack(anchor="w")
        dir_ = tk.Frame(topo, bg=C["fundo"]); dir_.pack(side="right", anchor="n")
        self.lbl_relogio = tk.Label(dir_, text="", bg=C["fundo"], fg=C["texto"], font=self.f["num"]); self.lbl_relogio.pack(anchor="e")
        self.lbl_prox = tk.Label(dir_, text="", bg=C["fundo"], fg=C["fraco"], font=self.f["peq"]); self.lbl_prox.pack(anchor="e", pady=(2, 0))

        # Faixa de estado geral
        linha = tk.Frame(self.raiz, bg=C["fundo"]); linha.pack(fill="x", padx=18, pady=(0, 10))
        self.faixa = tk.Label(linha, text="Verificando…", bg=C["neutro"], fg="#0b1020", font=self.f["med_b"], pady=7, anchor="w", padx=14)
        bts = tk.Frame(linha, bg=C["fundo"]); bts.pack(side="right")
        self._botao(bts, "↗ Abrir o site", lambda: webbrowser.open(URL_SITE), C["ouro"], "#1a1300").pack(side="left", padx=(0, 6))
        self._botao(bts, "↗ Resultados do TSE", lambda: webbrowser.open(URL_TSE_PAGINA), C["card2"], C["texto"]).pack(side="left", padx=(0, 6))
        self._botao(bts, "↗ /api/verificar", lambda: webbrowser.open(URL_SITE + "/api/verificar"), C["card2"], C["texto"]).pack(side="left", padx=(0, 6))
        self._botao(bts, "▤ Gráfico", self.abrir_grafico, C["card2"], C["texto"]).pack(side="left", padx=(0, 6))
        self._botao(bts, "⟳ Atualizar agora", self.atualizar_agora, C["card2"], C["texto"]).pack(side="left")
        self.faixa.pack(side="left", fill="x", expand=True, padx=(0, 10))


        # Luzes
        luzes = tk.Frame(self.raiz, bg=C["fundo"]); luzes.pack(fill="x", padx=18)
        self.luz_tse = Luz(luzes, "TSE · ARQUIVO OFICIAL", self.f)
        self.luz_site = Luz(luzes, "SITE NO AR", self.f)
        self.luz_leitura = Luz(luzes, "SITE LENDO O TSE", self.f)
        self.luz_dados = Luz(luzes, "DADOS CHEGANDO", self.f)
        self.luz_publico = Luz(luzes, "PÚBLICO NO SITE", self.f)
        for i, l in enumerate((self.luz_tse, self.luz_site, self.luz_leitura, self.luz_dados, self.luz_publico)):
            l.frame.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            luzes.grid_columnconfigure(i, weight=1, uniform="luz")

        # Corpo: apuração (esquerda) e classificação (direita)
        corpo = tk.Frame(self.raiz, bg=C["fundo"]); corpo.pack(fill="both", expand=True, padx=18, pady=10)
        corpo.grid_columnconfigure(0, weight=0, minsize=360)
        corpo.grid_columnconfigure(1, weight=1)
        corpo.grid_rowconfigure(0, weight=1)

        col_esq = tk.Frame(corpo, bg=C["fundo"]); col_esq.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ap = self._card(col_esq, "Apuração"); ap.pack(fill="x")
        self.lbl_pct = tk.Label(ap, text="—", bg=C["card"], fg=C["ouro"], font=self.f["grande"]); self.lbl_pct.pack(anchor="w", padx=16)
        tk.Label(ap, text="das seções (urnas) totalizadas", bg=C["card"], fg=C["fraco"], font=self.f["peq"]).pack(anchor="w", padx=16)
        self.cv_prog = tk.Canvas(ap, height=14, bg=C["card"], highlightthickness=0); self.cv_prog.pack(fill="x", padx=16, pady=(10, 4))
        self.lbl_gap = tk.Label(ap, text="", bg=C["card"], fg=C["ouro"], font=self.f["linha_nome"]); self.lbl_gap.pack(anchor="w", padx=16, pady=(4, 0))
        self.lbl_secoes = tk.Label(ap, text="", bg=C["card"], fg=C["texto"], font=self.f["peq"]); self.lbl_secoes.pack(anchor="w", padx=16)
        self.lbl_gerado = tk.Label(ap, text="", bg=C["card"], fg=C["fraco"], font=self.f["peq"], justify="left"); self.lbl_gerado.pack(anchor="w", padx=16, pady=(6, 14))

        tot = self._card(col_esq, "Totais"); tot.pack(fill="x", pady=(10, 0))
        grade = tk.Frame(tot, bg=C["card"]); grade.pack(fill="x", padx=16, pady=(0, 12))
        self.totais = {}
        itens = [("te", "Eleitorado"), ("comp", "Comparecimento"), ("abst", "Abstenção"),
                 ("vv", "Votos válidos"), ("vb", "Brancos"), ("vn", "Nulos")]
        for i, (k, rot) in enumerate(itens):
            lin_, col_ = divmod(i, 2)
            cel = tk.Frame(grade, bg=C["card"]); cel.grid(row=lin_, column=col_, sticky="w", pady=2, padx=(0, 10))
            tk.Label(cel, text=rot, bg=C["card"], fg=C["fraco"], font=self.f["peq"]).pack(anchor="w")
            lb = tk.Label(cel, text="—", bg=C["card"], fg=C["texto"], font=self.f["linha_nome"]); lb.pack(anchor="w")
            self.totais[k] = lb
        grade.grid_columnconfigure(0, weight=1, uniform="tot"); grade.grid_columnconfigure(1, weight=1, uniform="tot")

        reg = self._card(col_esq, "Registro"); reg.pack(fill="both", expand=True, pady=(10, 0))
        self.txt_log = tk.Text(reg, height=3, width=40, bg=C["card"], fg=C["fraco"], font=self.f["mono"], relief="flat", highlightthickness=0, wrap="word")
        self.txt_log.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        for nivel, cor in (("ok", C["ok"]), ("atencao", C["atencao"]), ("erro", C["erro"])):
            self.txt_log.tag_configure(nivel, foreground=cor)
        self.txt_log.configure(state="disabled")

        # Classificação
        col_dir = tk.Frame(corpo, bg=C["fundo"]); col_dir.grid(row=0, column=1, sticky="nsew")
        cl = self._card(col_dir, "Classificação ao vivo (votos válidos)"); cl.pack(fill="both", expand=True)
        self.cv = tk.Canvas(cl, bg=C["card"], highlightthickness=0); self.cv.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.cv.bind("<Configure>", lambda e: self._desenhar_classificacao(forcar=True))


    def _botao(self, pai, texto, cmd, bg, fg):
        b = tk.Label(pai, text=texto, bg=bg, fg=fg, font=self.f["med_b"], cursor="hand2", padx=10, pady=6, anchor="w")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=self._clarear(bg)))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    @staticmethod
    def _clarear(hexcor, k=0.12):
        r, g, b = (int(hexcor[i:i + 2], 16) for i in (1, 3, 5))
        r, g, b = (int(c + (255 - c) * k) for c in (r, g, b))
        return f"#{r:02x}{g:02x}{b:02x}"

    # ---------------- registro ----------------
    def _registrar(self, msg, nivel=None):
        hora = datetime.now(BRT).strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("1.0", f"{hora}  {msg}\n", nivel or ())
        linhas = int(self.txt_log.index("end-1c").split(".")[0])
        if linhas > 300:
            self.txt_log.delete("300.0", "end")
        self.txt_log.configure(state="disabled")

    # ---------------- ciclo ----------------
    def atualizar_agora(self):
        self.prox = 0
        self.pedido_manual = True  # se houver consulta em andamento, faz outra logo depois dela

    def _fase(self):
        """'espera' (antes das 16h30), 'aquecimento' (16h30–17h) ou 'apuracao' (17h em diante)."""
        falta = (INICIO_UTC - datetime.now(timezone.utc)).total_seconds()
        if falta <= 0:
            return "apuracao"
        return "aquecimento" if falta <= AQUECIMENTO_MIN * 60 else "espera"

    def _agendar_proxima(self):
        """Define quando será a próxima consulta automática, conforme a fase."""
        agora = time.time()
        if getattr(self, "pedido_manual", False):  # clicaram em "Atualizar agora" durante a consulta
            self.pedido_manual = False
            self.prox = agora
            return
        fase = self._fase()
        if fase == "apuracao":
            self.prox = agora + self.intervalo
        elif fase == "aquecimento":
            self.prox = min(agora + INTERVALO_AQUECIMENTO, INICIO_UTC.timestamp())
        else:  # em espera: só volta a consultar às 16h30
            self.prox = INICIO_UTC.timestamp() - AQUECIMENTO_MIN * 60

    def _loop(self):
        try:
            self._passo()
        except Exception as ex:  # nunca deixa o painel congelar por causa de um dado estranho
            try:
                self._registrar("Erro interno do painel: " + repr(ex)[:150], "erro")
            except Exception:
                pass
        finally:
            self.raiz.after(250, self._loop)

    def _passo(self):
        agora = time.time()
        # relógio e contagem para o início
        self.lbl_relogio.configure(text=datetime.now(BRT).strftime("%d/%m/%Y  %H:%M:%S") + "  (Brasília)")
        falta = (INICIO_UTC - datetime.now(timezone.utc)).total_seconds()
        if falta > 0:
            d, r = divmod(int(falta), 86400); h, r = divmod(r, 3600); m, _ = divmod(r, 60)
            dia_ini, hoje = INICIO_UTC.astimezone(BRT).date(), datetime.now(BRT).date()
            quando = "hoje, 17h" if dia_ini == hoje else "amanhã, 17h" if (dia_ini - hoje).days == 1 else "domingo, 25/10, 17h"
            self.lbl_inicio.configure(text=f"Divulgação começa em {d}d {h}h {m}min ({quando})" if d else f"Divulgação começa em {h}h {m}min ({quando})")
        else:
            self.lbl_inicio.configure(text="Divulgação em andamento desde as 17h de 25/10")
        # dispara nova consulta
        if not self.ocupado and agora >= self.prox:
            self.ocupado = True
            self.pedido_manual = False
            threading.Thread(target=ciclo_de_consulta, args=(self.fila, self.arquivo_local), daemon=True).start()
        if self.ocupado:
            self.lbl_prox.configure(text="Consultando…")
        else:
            falta_c = max(0, int(self.prox - agora))
            fase = self._fase()
            if fase == "espera":
                h, r = divmod(falta_c, 3600); m, _ = divmod(r, 60)
                self.lbl_prox.configure(text=f"Em espera até 16h30 (faltam {h}h {m:02d}min) · ⟳ checa agora")
            elif fase == "aquecimento":
                self.lbl_prox.configure(text=f"Pré-apuração: a cada {INTERVALO_AQUECIMENTO} s · próxima em {falta_c} s")
            else:
                self.lbl_prox.configure(text=f"Apuração: a cada {self.intervalo} s · próxima em {falta_c} s")
        # público no site (estatísticas da Cloudflare)
        if self.cf_token and self.cf_conta:
            if not self.pub_ocupado and agora >= self.pub_prox:
                self.pub_ocupado = True
                def tarefa_pub():
                    try:
                        r = consultar_publico(self.cf_token, self.cf_conta)
                    except Exception as ex:
                        r = {"ok": False, "erro": "falha ao ler estatísticas: " + repr(ex)[:80]}
                    self.pub_fila.put(r)
                threading.Thread(target=tarefa_pub, daemon=True).start()
            try:
                while True:
                    p = self.pub_fila.get_nowait()
                    try:
                        self._aplicar_publico(p)
                    except Exception as ex:
                        self.pub_ocupado = False
                        self.pub_prox = time.time() + GQL_INTERVALO
                        self._registrar("Público: erro ao mostrar: " + repr(ex)[:120], "atencao")
            except queue.Empty:
                pass
        elif not getattr(self, "_aviso_cfg", False):
            self._aviso_cfg = True
            if self.cf_token and not self.cf_conta:
                falta = "falta o ID da conta (cf_account_id) no painel_config.json"
            elif self.cf_conta and not self.cf_token:
                falta = "falta o token (cf_api_token) no painel_config.json"
            else:
                falta = "sem painel_config.json: o resto do painel funciona normalmente"
            self.luz_publico.definir("neutro", "Não configurado", falta)
        # resultados prontos
        try:
            while True:
                res = self.fila.get_nowait()
                try:
                    self._aplicar(res)
                except Exception as ex:
                    self.ocupado = False
                    self._agendar_proxima()
                    self._registrar("Erro ao mostrar a consulta: " + repr(ex)[:150], "erro")
        except queue.Empty:
            pass

    def _aplicar(self, res):
        self.ocupado = False
        self._agendar_proxima()
        comecou = datetime.now(timezone.utc) >= INICIO_UTC
        niveis = []

        # --- TSE
        t = res["tse"]
        if t["ok"]:
            d = t["dados"]
            self.luz_tse.definir("ok", "Respondendo", f"HTTP {t['http']} · {t['ms']} ms · {len(d['cands'])} candidatos no arquivo")
            niveis.append("ok")
            self.dados = d
            self._atualizar_numeros(d)
            self._atualizar_classificacao(d)
        else:
            self.luz_tse.definir("erro", "Fora do ar ou com erro", f"{t['erro'] or 'sem resposta'} · {t['ms']} ms")
            niveis.append("erro")
            self._registrar("TSE: " + (t["erro"] or "sem resposta"), "erro")

        # --- Site: servidor no ar
        s = res["site"]
        if s["status"] and "upstream" in s["status"]:
            ini = str(s["status"].get("start", ""))
            try:
                ini_site = datetime.fromisoformat(ini.replace("Z", "+00:00"))
            except ValueError:
                ini_site = None
            if ini_site is not None and ini_site != INICIO_UTC:
                self.luz_site.definir("atencao", "No ar, início diferente",
                                      f"o site começa em {ini_site.astimezone(BRT):%d/%m %H:%M} (Brasília); o painel espera {INICIO_UTC.astimezone(BRT):%d/%m %H:%M} · confira TSE_START")
                niveis.append("atencao")
            else:
                self.luz_site.definir("ok", "No ar", f"HTTP {s['http']} · {s['ms']} ms · início configurado: {ini.replace('T', ' ')[:16]} UTC")
                niveis.append("ok")
        else:
            self.luz_site.definir("erro", "Sem resposta", s["erro"] or "resposta inesperada")
            niveis.append("erro")
            self._registrar("Site: " + (s["erro"] or "resposta inesperada em /api/status"), "erro")

        # --- Site: consegue ler o TSE? (/api/verificar) e o que a página recebe (/api/resultado)
        ver, rs = s["verificar"], s["resultado"]
        if ver and ver.get("ok"):
            det = f"/api/verificar: {fmt_pct(ver.get('pst'))} · {len(ver.get('cands') or [])} candidatos"
            nivel = "ok"
            if ver.get("stale"):
                nivel, det = "atencao", det + " · usando último dado (TSE falhou para o servidor)"
            if t["ok"] and ok_num(ver.get("pst", float('nan'))) and t["dados"]["pst"] - ver["pst"] > 1.0:
                nivel, det = "atencao", det + f" · atrás do TSE ({fmt_pct(t['dados']['pst'])})"
            if rs is not None:
                if rs.get("error") == "not_started":
                    if comecou:
                        nivel = "erro"
                        det += "\n/api/resultado: o site ainda acha que não começou (confira TSE_START no Worker)"
                    else:
                        det += "\n/api/resultado: aguardando 17h (correto antes do início)"
                elif rs.get("ok"):
                    det += f"\n/api/resultado: {fmt_pct(rs.get('pst'))} (o que a página recebe)"
                else:
                    det += f"\n/api/resultado: {rs.get('error')}"
                    if comecou:
                        nivel = "atencao"
            elif s.get("erro_res"):
                det += f"\n/api/resultado: {s['erro_res']}"
                if comecou:
                    nivel = "atencao"
            self.luz_leitura.definir(nivel, "Lendo normalmente" if nivel == "ok" else "Erro" if nivel == "erro" else "Atenção", det)
            niveis.append(nivel)
        else:
            erro = (ver or {}).get("error") or s["erro_ver"] or "sem resposta"
            self.luz_leitura.definir("erro", "Não consegue ler", f"/api/verificar: {erro}")
            niveis.append("erro")
            self._registrar("Site não conseguiu ler o TSE: " + str(erro), "erro")

        # --- Atualização dos dados (o TSE está publicando coisas novas?)
        if t["ok"]:
            d = t["dados"]
            marca = (d["gerado"], d["pst"] if ok_num(d["pst"]) else None)
            if marca != self.ultimo_gerado:
                if self.ultimo_gerado is not None:
                    self._registrar(f"Novo dado do TSE: {fmt_pct(d['pst'])} das seções · gerado {d['gerado']}", "ok")
                self.ultimo_gerado, self.quando_mudou = marca, time.time()
            parado = time.time() - max(self.quando_mudou or time.time(), INICIO_UTC.timestamp())
            if not comecou:
                self.luz_dados.definir("ok", "Pronto (17h)", f"Arquivo gerado em {d['gerado'] or '—'} · {fmt_pct(d['pst'])} apurado")
                niveis.append("ok")
            elif d["final"] or (ok_num(d["pst"]) and d["pst"] >= 100):
                self.luz_dados.definir("ok", "Apuração concluída", f"Última totalização: {d['gerado']}")
                niveis.append("ok")
            elif parado > SEM_ATUALIZAR_ALERTA:
                self.luz_dados.definir("atencao", f"Sem novidade há {int(parado // 60)} min", f"Último dado: {d['gerado']} · {fmt_pct(d['pst'])}")
                niveis.append("atencao")
            else:
                self.luz_dados.definir("ok", "Chegando", f"Último dado: {d['gerado']} · há {int(parado)} s sem mudança")
                niveis.append("ok")
        else:
            self.luz_dados.definir("erro", "Sem dados", "Não foi possível ler o arquivo do TSE")
            niveis.append("erro")

        # --- Faixa geral
        if "erro" in niveis:
            self.faixa.configure(text="●  ERRO: veja os indicadores em vermelho", bg=COR["erro"], fg="#ffffff")
        elif "atencao" in niveis:
            self.faixa.configure(text="●  ATENÇÃO: funcionando, mas algo merece olhar", bg=COR["atencao"], fg="#1a1300")
        else:
            self.faixa.configure(text="●  TUDO OK: TSE respondendo, site no ar e lendo os dados", bg=COR["ok"], fg="#06210f")

    # ---------------- números ----------------
    def _aplicar_publico(self, p):
        self.pub_ocupado = False
        # mesma lógica de fases: em espera (antes das 16h30) não fica consultando
        self.pub_prox = time.time() + GQL_INTERVALO if self._fase() != "espera" else INICIO_UTC.timestamp() - AQUECIMENTO_MIN * 60
        if not p["ok"]:
            self.luz_publico.definir("atencao", "Sem estatísticas", p["erro"])
            self._registrar("Público: " + p["erro"], "atencao")
            return
        uso = 100 * p["req_dia"] / COTA_DIA
        nivel = "erro" if uso >= 100 else "atencao" if uso >= 80 else "ok"
        on = p["online"]
        txt = "≈ 0 online" if on < 1 else f"≈ {int(round(on)):,} online".replace(",", ".")
        det = (f"média {p['jan_ini']}–{p['jan_fim']} · hoje {fmt_int(p['req_dia'])} consultas\n"
               f"cota grátis: {uso:.0f}% usada (zera às 21h)")
        if p["erros_dia"]:
            det += f" · {fmt_int(p['erros_dia'])} com erro"
        if nivel == "erro":
            det += "\nCOTA ESGOTADA: o site mostra o último dado até as 21h."
        self.luz_publico.definir(nivel, txt, det)

    # ---------------- gráfico minuto a minuto (histórico gravado pelo site) ----------------
    def abrir_grafico(self):
        if getattr(self, "jan_graf", None) and self.jan_graf.winfo_exists():
            self.jan_graf.lift(); return
        j = tk.Toplevel(self.raiz); j.title("Evolução minuto a minuto · 2º turno"); j.configure(bg=COR["fundo"]); j.geometry("980x620")
        self.jan_graf = j
        self.lbl_graf = tk.Label(j, text="Carregando histórico do site…", bg=COR["fundo"], fg=COR["fraco"], font=self.f["peq"], anchor="w")
        self.lbl_graf.pack(fill="x", padx=16, pady=(12, 0))
        self.cv_graf = tk.Canvas(j, bg=COR["card"], highlightthickness=0); self.cv_graf.pack(fill="both", expand=True, padx=16, pady=12)
        self.cv_graf.bind("<Configure>", lambda e: self._desenhar_grafico())
        self.hist_pts = []
        self.graf_geracao = getattr(self, "graf_geracao", 0) + 1  # janela nova: a busca da janela antiga para sozinha
        self._buscar_hist(self.graf_geracao)

    def _buscar_hist(self, geracao):
        if geracao != getattr(self, "graf_geracao", 0) or not (getattr(self, "jan_graf", None) and self.jan_graf.winfo_exists()):
            return
        def tarefa():
            marcar_propria(); dados, ms, http, erro = baixar_json(URL_SITE + "/api/historico")
            try:
                self.raiz.after(0, lambda: self._receber_hist(dados, erro))
            except RuntimeError:  # painel fechado durante a busca
                pass
        threading.Thread(target=tarefa, daemon=True).start()
        self.raiz.after(60000, lambda: self._buscar_hist(geracao))  # a cada minuto, enquanto ESTA janela estiver aberta

    def _receber_hist(self, dados, erro):
        if not (getattr(self, "jan_graf", None) and self.jan_graf.winfo_exists()):
            return
        if isinstance(dados, dict) and dados.get("ok"):
            self.hist_pts = [p for p in (dados.get("pontos") or []) if isinstance(p, dict)]
            n = sum(1 for p in self.hist_pts if ok_num(p.get("t")) and p["t"] / 1000 >= INICIO_UTC.timestamp() - 60)
            self.lbl_graf.configure(text=f"{n} pontos gravados pelo site (um por minuto, desde as 17h). Atualiza a cada minuto.")
        else:
            self.lbl_graf.configure(text="Não foi possível ler o histórico do site: " + str((dados.get("error") if isinstance(dados, dict) else None) or erro or "resposta inesperada"))
        self._desenhar_grafico()

    def _desenhar_grafico(self):
        cv = getattr(self, "cv_graf", None)
        if not cv:
            return
        cv.delete("all")
        W, H = max(400, cv.winfo_width()), max(300, cv.winfo_height())
        pts = []
        for p in getattr(self, "hist_pts", []):
            if not ok_num(p.get("t")) or p["t"] / 1000 < INICIO_UTC.timestamp() - 60:
                continue  # ponto de ensaio, anterior ao início da apuração
            v = {int(k): float(x) for k, x in (p.get("votos") or {}).items() if ok_num(x)}
            tot = p.get("vvc") or sum(v.values())
            if tot:
                pts.append((p["t"] / 1000, p["pst"], v, tot))
        if len(pts) < 2:
            cv.create_text(W / 2, H / 2, text="O gráfico aparece depois das primeiras atualizações do TSE.", fill=COR["fraco"], font=self.f["med"])
            return
        ml, mr, mt, gap = 60, 190, 20, 40
        h1 = (H - mt - gap - 60) * 0.62
        h2 = (H - mt - gap - 60) - h1
        t0, t1 = pts[0][0], pts[-1][0] or pts[0][0] + 60
        X = lambda t: ml + (W - ml - mr) * (t - t0) / max(1, t1 - t0)
        nums = [c[0] for c in CANDS]
        sh = [[100 * p[2].get(n, 0) / p[3] for n in nums] for p in pts]
        lo = max(0, int(min(min(r) for r in sh)) - 1); hi = min(100, int(max(max(r) for r in sh)) + 2)
        Y1 = lambda v: mt + h1 * (1 - (v - lo) / max(1e-9, hi - lo))
        gaps = [p[2].get(nums[0], 0) - p[2].get(nums[1], 0) for p in pts]
        gmax = max(abs(g) for g in gaps) * 1.1 or 1
        top2 = mt + h1 + gap
        Y2 = lambda v: top2 + h2 * (1 - v / gmax)
        f = self.f
        cv.create_text(ml, mt - 8, text="VOTOS VÁLIDOS (%)", fill=COR["fraco"], font=f["peq_b"], anchor="w")
        for k in range(5):
            v = lo + (hi - lo) * k / 4; y = Y1(v)
            cv.create_line(ml, y, W - mr, y, fill="#1d2944")
            cv.create_text(ml - 8, y, text=f"{v:.1f}%".replace(".", ","), fill=COR["fraco"], font=f["peq"], anchor="e")
        for i, n in enumerate(nums):
            info = INFO[n]
            coords = []
            for j, p in enumerate(pts):
                coords += [X(p[0]), Y1(sh[j][i])]
            cv.create_line(*coords, fill=info[3], width=2, smooth=False)
            yl = Y1(sh[-1][i])
            cv.create_oval(X(t1) - 4, yl - 4, X(t1) + 4, yl + 4, fill=info[3], outline=COR["card"], width=2)
            cv.create_text(X(t1) + 10, yl, text=f"{info[1]} {sh[-1][i]:.1f}%".replace(".", ","), fill=COR["texto"], font=f["peq_b"], anchor="w")
        cv.create_text(ml, top2 - 8, text="DIFERENÇA ENTRE 1º E 2º (VOTOS)", fill=COR["fraco"], font=f["peq_b"], anchor="w")
        for k in range(4):
            v = gmax / 1.1 * k / 3; y = Y2(v)
            cv.create_line(ml, y, W - mr, y, fill="#1d2944")
            cv.create_text(ml - 8, y, text=compacto(v), fill=COR["fraco"], font=f["peq"], anchor="e")
        for j in range(1, len(pts)):
            cor = INFO[nums[0]][3] if gaps[j] >= 0 else INFO[nums[1]][3]
            cv.create_line(X(pts[j - 1][0]), Y2(abs(gaps[j - 1])), X(pts[j][0]), Y2(abs(gaps[j])), fill=cor, width=2)
        cv.create_text(X(t1) + 10, Y2(abs(gaps[-1])), text=f"{fmt_int(abs(gaps[-1]))} votos", fill=COR["texto"], font=f["peq_b"], anchor="w")
        n_t = max(2, min(6, len(pts), int((W - ml - mr) / 90)))
        for k in range(n_t):
            t = t0 + (t1 - t0) * k / (n_t - 1)
            cv.create_text(X(t), H - 16, text=datetime.fromtimestamp(t, BRT).strftime("%H:%M"), fill=COR["fraco"], font=f["peq"])

    def _atualizar_numeros(self, d):
        self.lbl_pct.configure(text=fmt_pct(d["pst"]))
        ordem = sorted(d["cands"], key=lambda c: -c["vap"])
        if len(ordem) >= 2 and ordem[0]["vap"] > 0:
            dv, dp = ordem[0]["vap"] - ordem[1]["vap"], ordem[0]["pvap"] - ordem[1]["pvap"]
            self.lbl_gap.configure(text=f"Diferença 1º–2º: {fmt_int(dv)} votos ({fmt_pct(dp, 1).replace('%', ' p.p.')})")
        else:
            self.lbl_gap.configure(text="")
        self.lbl_secoes.configure(text=f"{fmt_int(d['st'])} de {fmt_int(d['ts'])} seções totalizadas")
        self.lbl_gerado.configure(text=f"Totalização: {d['gerado_tot'] or 'ainda não iniciada'}\nArquivo gerado: {d['gerado'] or '—'}")
        self.cv_prog.update_idletasks()
        w = max(1, self.cv_prog.winfo_width())
        self.cv_prog.delete("all")
        self.cv_prog.create_rectangle(0, 0, w, 14, fill=COR["card2"], outline="")
        if ok_num(d["pst"]):
            self.cv_prog.create_rectangle(0, 0, w * d["pst"] / 100, 14, fill=COR["ouro"], outline="")
        for k, lb in self.totais.items():
            val = d[k]
            base_el = d["est"] if ok_num(d["est"]) and d["est"] > 0 else d["te"]  # eleitorado das seções já apuradas
            ref = d["tv"] if k in ("vv", "vb", "vn") else base_el if k in ("comp", "abst") else float("nan")
            extra = f"  ({fmt_pct(100 * val / ref, 1)})" if ok_num(val) and ok_num(ref) and ref > 0 else ""
            lb.configure(text=fmt_int(val) + extra)

    # ---------------- classificação animada ----------------
    def _atualizar_classificacao(self, d):
        por_num = {c["n"]: c for c in d["cands"]}
        ordem = sorted(por_num.values(), key=lambda c: (-c["vap"], c["n"]))
        nova = {c["n"]: i for i, c in enumerate(ordem)}
        if any(v > 0 for v in (c["vap"] for c in ordem)):
            for n, p in nova.items():
                ant = self.pos_anterior.get(n)
                if ant is not None and ant != p:
                    self.tendencia[n] = ant - p  # >0 subiu, <0 desceu
                    if p == 0 and ant != 0:
                        self._registrar(f"{INFO.get(n, (n, str(n)))[1]} passou a liderar", "atencao")
        self.pos_anterior = nova
        self.ordem = [c["n"] for c in ordem]
        self.votos = por_num
        self._desenhar_classificacao(forcar=True)

    def _geometria(self):
        h = max(200, self.cv.winfo_height())
        lin = max(24, min(46, (h - 30) // 13))
        return lin

    def _animar(self):
        """Move suavemente cada linha até sua nova posição."""
        if getattr(self, "ordem", None):
            lin = self._geometria()
            mexeu = False
            for i, n in enumerate(self.ordem):
                alvo = 26 + i * lin
                y = self.y_atual.get(n, alvo)
                if abs(alvo - y) > 0.5:
                    self.y_atual[n] = y + (alvo - y) * 0.18
                    mexeu = True
                else:
                    self.y_atual[n] = alvo
            if mexeu:
                self._desenhar_classificacao()
        self.raiz.after(30, self._animar)

    def _desenhar_classificacao(self, forcar=False):
        cv = self.cv
        cv.delete("all")
        w = max(400, cv.winfo_width())
        lin = self._geometria()
        f = self.f
        # cabeçalho
        x_pos, x_chip, x_nome, x_bar, x_votos, x_pct = 10, 70, 112, int(w * 0.50), w - 100, w - 12
        cv.create_text(x_pos, 12, text="#", fill=COR["fraco"], font=f["peq_b"], anchor="w")
        cv.create_text(x_nome, 12, text="CANDIDATO", fill=COR["fraco"], font=f["peq_b"], anchor="w")
        cv.create_text(x_votos - 14, 12, text="VOTOS", fill=COR["fraco"], font=f["peq_b"], anchor="e")
        cv.create_text(x_bar, 12, text="PROPORÇÃO AO LÍDER", fill=COR["fraco"], font=f["peq_b"], anchor="w")
        cv.create_text(x_pct, 12, text="%", fill=COR["fraco"], font=f["peq_b"], anchor="e")
        ordem = getattr(self, "ordem", None) or [c[0] for c in CANDS]
        votos = getattr(self, "votos", {})
        maior = max([votos[n]["vap"] for n in ordem if n in votos] + [1])
        tem_votos = any(votos.get(n, {}).get("vap", 0) > 0 for n in ordem)
        for i, n in enumerate(ordem):
            y = self.y_atual.get(n, 26 + i * lin)
            if forcar and n not in self.y_atual:
                self.y_atual[n] = y
            info = INFO.get(n, (n, f"Nº {n}", "?", "#888888", "#000000"))
            c = votos.get(n, {"vap": 0, "pvap": 0, "st": ""})
            fundo = COR["card2"] if i % 2 == 0 else COR["card"]
            if i < 2 and tem_votos:
                fundo = "#1c2a4a"
            cv.create_rectangle(4, y, w - 4, y + lin - 4, fill=fundo, outline="")
            # posição e tendência
            cv.create_text(x_pos + 8, y + (lin - 4) / 2, text=f"{i + 1}º", fill=COR["ouro"] if i < 2 and tem_votos else COR["texto"], font=f["linha_nome"], anchor="w")
            tnd = self.tendencia.get(n, 0)
            if tnd:
                cv.create_text(x_pos + 44, y + (lin - 4) / 2, text="▲" if tnd > 0 else "▼", fill=COR["ok"] if tnd > 0 else COR["erro"], font=f["peq_b"])
            # chip com número e cor do partido
            r = max(9, (lin - 10) / 2)
            compacta = lin < 38
            cx, cy = x_chip + r, y + (lin - 4) / 2
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=info[3], outline="#ffffff" if info[3] in ("#0B0D10", "#17226B", "#7A1F2B") else "")
            cv.create_text(cx, cy, text=str(n), fill=info[4], font=f["peq_b"])
            # nome e partido
            sub_txt = info[2] + (f" · {c['st']}" if c.get("st") else "")
            if compacta:
                idn = cv.create_text(x_nome, cy, text=info[1], fill=COR["texto"], font=f["linha_nome"], anchor="w")
                x2 = cv.bbox(idn)[2] + 8
                cv.create_text(x2, cy + 1, text=sub_txt, fill=COR["fraco"], font=f["peq"], anchor="w")
            else:
                cv.create_text(x_nome, cy - 7, text=info[1], fill=COR["texto"], font=f["linha_nome"], anchor="w")
                cv.create_text(x_nome, cy + 9, text=sub_txt, fill=COR["fraco"], font=f["peq"], anchor="w")
            # barra proporcional ao líder
            larg = (x_votos - 120 - x_bar)
            cv.create_rectangle(x_bar, cy - 6, x_bar + larg, cy + 6, fill="#0e1628", outline="")
            if c["vap"] > 0:
                cv.create_rectangle(x_bar, cy - 6, x_bar + larg * c["vap"] / maior, cy + 6, fill=info[3] if info[3] not in ("#0B0D10", "#F5F5F5") else COR["barra"], outline="")
            cv.create_text(x_votos - 14, cy, text=fmt_int(c["vap"]), fill=COR["texto"], font=f["linha_num"], anchor="e")
            cv.create_text(x_pct, cy, text=fmt_pct(c["pvap"]), fill=COR["ouro"] if i < 2 and tem_votos else COR["texto"], font=f["linha_nome"], anchor="e")
        if not tem_votos:
            cv.create_text(w / 2, 26 + len(ordem) * lin + 14, text="Ainda sem votos apurados: a ordem acima é pelo número de urna.", fill=COR["fraco"], font=f["peq"])


def main():
    ap = argparse.ArgumentParser(description="Painel de controle da Corrida para a Presidência")
    ap.add_argument("--intervalo", type=int, default=INTERVALO_PADRAO, help="segundos entre consultas (mínimo 10)")
    ap.add_argument("--arquivo", help="ler um JSON local no lugar do TSE (só para testar o painel)")
    a = ap.parse_args()
    raiz = tk.Tk()
    Painel(raiz, max(10, a.intervalo), a.arquivo)
    raiz.mainloop()


if __name__ == "__main__":
    main()
