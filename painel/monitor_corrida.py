#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor SEM JANELA · Corrida para a Presidência (2º turno)
==========================================================

Faz as mesmas checagens do painel_corrida.py (TSE, site, leitura do TSE pelo site,
dados chegando, público), mas sem Tkinter: ideal para um Raspberry Pi sem monitor.
A cada rodada grava, na mesma pasta:

  estado_corrida.json   → estado completo (para programas, como o OpenClaw)
  estado_corrida.txt    → resumo em português, curto (para ler no celular)
  monitor_corrida.log   → registro: só quando o nível muda ou chega dado novo

Uso:
  python3 monitor_corrida.py               # fica rodando (mesmas fases do painel)
  python3 monitor_corrida.py --uma-vez     # faz uma checagem agora, mostra o resumo e sai
  python3 monitor_corrida.py --resumo      # só mostra o último resumo gravado (não consulta nada)
  python3 monitor_corrida.py --intervalo 30

Fases (pelo relógio, em horário de Brasília):
  antes das 16h30 → uma checagem por hora (só para saber que está tudo no ar)
  16h30 às 17h    → a cada 60 s
  a partir das 17h → a cada --intervalo segundos (padrão 30)

Só biblioteca padrão. Usa as funções do painel_corrida.py (precisa estar na mesma pasta).
"""
import argparse
import json
import os
import queue
import sys
import time
from datetime import datetime, timezone

import painel_corrida as P

AQUI = os.path.dirname(os.path.abspath(__file__))
ARQ_JSON = os.path.join(AQUI, "estado_corrida.json")
ARQ_TXT = os.path.join(AQUI, "estado_corrida.txt")
ARQ_LOG = os.path.join(AQUI, "monitor_corrida.log")
ESPERA_ANTES = 3600  # antes das 16h30: uma checagem por hora


def agora_brt():
    return datetime.now(P.BRT)


def registrar(msg):
    linha = f"{agora_brt():%d/%m %H:%M:%S}  {msg}"
    print(linha, flush=True)
    with open(ARQ_LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def gravar(caminho, texto):
    """Grava de forma atômica (quem estiver lendo nunca pega o arquivo pela metade)."""
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, caminho)


def avaliar(res, mem):
    """Transforma uma rodada do painel em níveis ok/atencao/erro (mesmas regras do painel)."""
    comecou = datetime.now(timezone.utc) >= P.INICIO_UTC
    itens = {}
    # TSE
    t = res["tse"]
    d = t["dados"] if t["ok"] else None
    if d:
        itens["tse"] = ("ok", f"respondendo ({t['ms']} ms)")
    else:
        itens["tse"] = ("erro" if comecou else "atencao", f"sem resposta: {t['erro'] or '—'}"
                        + ("" if comecou else " (normal antes da publicação do arquivo)"))
    # Site no ar e horário de início
    s = res["site"]
    st = s["status"] or {}
    if "upstream" in st:
        ini = str(st.get("start", ""))
        try:
            ini_site = datetime.fromisoformat(ini.replace("Z", "+00:00"))
        except ValueError:
            ini_site = None
        if ini_site is not None and ini_site != P.INICIO_UTC:
            itens["site"] = ("atencao", f"no ar, mas o início configurado é {ini_site.astimezone(P.BRT):%d/%m %H:%M}")
        else:
            itens["site"] = ("ok", f"no ar ({s['ms']} ms)")
    else:
        itens["site"] = ("erro", f"sem resposta: {s['erro'] or 'resposta inesperada'}")
    # Site lendo o TSE e o que a página recebe
    ver, rs = s["verificar"] or {}, s["resultado"] or {}
    if ver.get("ok"):
        nivel = "atencao" if ver.get("stale") else "ok"
        det = f"site lê o TSE: {P.fmt_pct(ver.get('pst'))}" + (" (último dado)" if ver.get("stale") else "")
        if rs.get("error") == "not_started" and comecou:
            nivel, det = "erro", det + "; a página ainda acha que não começou (TSE_START)"
        elif rs and not rs.get("ok") and rs.get("error") != "not_started" and comecou:
            nivel, det = "atencao", det + f"; página recebe: {rs.get('error')}"
        itens["leitura"] = (nivel, det)
    else:
        erro = ver.get("error") or s["erro_ver"] or "sem resposta"
        itens["leitura"] = ("erro" if comecou else "atencao", f"site não lê o TSE: {erro}")
    # Dados chegando
    if d:
        marca = (d["gerado"], d["pst"] if P.ok_num(d["pst"]) else None)
        if marca != mem.get("marca"):
            if mem.get("marca") is not None:
                registrar(f"Novo dado do TSE: {P.fmt_pct(d['pst'])} das seções · gerado {d['gerado']}")
            mem["marca"], mem["mudou"] = marca, time.time()
        parado = time.time() - max(mem.get("mudou") or time.time(), P.INICIO_UTC.timestamp())
        if not comecou:
            itens["dados"] = ("ok", "aguardando 17h")
        elif d["final"] or (P.ok_num(d["pst"]) and d["pst"] >= 100):
            itens["dados"] = ("ok", "apuração concluída")
        elif parado > P.SEM_ATUALIZAR_ALERTA:
            itens["dados"] = ("atencao", f"sem novidade do TSE há {int(parado // 60)} min")
        else:
            itens["dados"] = ("ok", "chegando")
    else:
        itens["dados"] = ("erro" if comecou else "atencao", "sem dados do TSE")
    return itens


def resumo(itens, d, publico):
    nivel = "ERRO" if any(n == "erro" for n, _ in itens.values()) else \
            "ATENÇÃO" if any(n == "atencao" for n, _ in itens.values()) else "TUDO OK"
    simb = {"ok": "✅", "atencao": "⚠️", "erro": "❌"}
    nomes = {"tse": "TSE", "site": "Site", "leitura": "Leitura", "dados": "Dados"}
    linhas = [f"Corrida para a Presidência · {agora_brt():%d/%m %H:%M} (Brasília)", f"Situação: {nivel}"]
    if d and P.ok_num(d["pst"]):
        linhas.append(f"Urnas apuradas: {P.fmt_pct(d['pst'])}")
        ordem = sorted(d["cands"], key=lambda c: -c["vap"])
        for i, c in enumerate(ordem[:2]):
            nome = P.INFO.get(c["n"], (c["n"], c["nm"]))[1]
            linhas.append(f"{i + 1}º {nome}: {P.fmt_pct(c['pvap'])} ({P.fmt_int(c['vap'])} votos)")
        if len(ordem) >= 2:
            linhas.append(f"Diferença: {P.fmt_int(ordem[0]['vap'] - ordem[1]['vap'])} votos")
        linhas.append(f"Arquivo do TSE gerado em {d['gerado'] or '—'}")
    for k, (n, det) in itens.items():
        linhas.append(f"{simb[n]} {nomes[k]}: {det}")
    if publico:
        if publico.get("ok"):
            linhas.append(f"👥 Público: ≈ {int(round(publico['online']))} online · "
                          f"cota do dia {100 * publico['req_dia'] / P.COTA_DIA:.0f}% (zera às 21h)")
        else:
            linhas.append(f"👥 Público: {publico.get('erro')}")
    return nivel, "\n".join(linhas) + "\n"


def rodada(mem):
    fila = queue.Queue()
    P.ciclo_de_consulta(fila, None)  # mesma consulta do painel (TSE + /api/status + /api/verificar + /api/resultado)
    res = fila.get()
    itens = avaliar(res, mem)
    d = res["tse"]["dados"] if res["tse"]["ok"] else None
    publico = None
    tok, conta = P.ler_config()
    if tok and conta:
        try:
            publico = P.consultar_publico(tok, conta)
        except Exception as ex:
            publico = {"ok": False, "erro": repr(ex)[:80]}
    nivel, txt = resumo(itens, d, publico)
    estado = {"quando": agora_brt().isoformat(timespec="seconds"), "nivel": nivel,
              "itens": {k: {"nivel": n, "detalhe": det} for k, (n, det) in itens.items()},
              "apuracao": None if not d else {"pst": d["pst"], "gerado": d["gerado"],
                                              "candidatos": [{"n": c["n"], "votos": c["vap"], "pct": c["pvap"]} for c in d["cands"]]},
              "publico": publico}
    gravar(ARQ_JSON, json.dumps(estado, ensure_ascii=False, indent=1, default=str))
    gravar(ARQ_TXT, txt)
    if nivel != mem.get("nivel"):
        probs = " | ".join(f"{k}: {det}" for k, (n, det) in itens.items() if n != "ok")
        registrar(f"Situação: {nivel}" + (f" · {probs}" if probs else ""))
        mem["nivel"] = nivel
    return txt


def espera(intervalo):
    falta = (P.INICIO_UTC - datetime.now(timezone.utc)).total_seconds()
    if falta <= 0:
        return intervalo
    if falta <= P.AQUECIMENTO_MIN * 60:
        return min(P.INTERVALO_AQUECIMENTO, falta)
    return min(ESPERA_ANTES, max(5, falta - P.AQUECIMENTO_MIN * 60))  # acorda às 16h30


def main():
    ap = argparse.ArgumentParser(description="Monitor sem janela da Corrida para a Presidência")
    ap.add_argument("--intervalo", type=int, default=P.INTERVALO_PADRAO, help="segundos entre consultas a partir das 17h (mínimo 10)")
    ap.add_argument("--uma-vez", action="store_true", help="faz uma checagem agora, mostra o resumo e sai")
    ap.add_argument("--resumo", action="store_true", help="mostra o último resumo gravado, sem consultar nada")
    a = ap.parse_args()
    if a.resumo:
        if not os.path.exists(ARQ_TXT):
            sys.exit("Ainda não há resumo: o monitor não rodou nenhuma vez.")
        idade = time.time() - os.path.getmtime(ARQ_TXT)
        print(open(ARQ_TXT, encoding="utf-8").read().rstrip())
        if idade > 300:
            print(f"(atenção: resumo de {int(idade // 60)} min atrás — o monitor pode estar parado)")
        return
    mem = {}
    if a.uma_vez:
        print(rodada(mem).rstrip())
        return
    intervalo = max(10, a.intervalo)
    registrar(f"Monitor iniciado (a partir das 17h, a cada {intervalo} s).")
    while True:
        try:
            rodada(mem)
        except Exception as ex:  # nunca para por causa de um dado estranho
            registrar("Erro na rodada: " + repr(ex)[:150])
        time.sleep(espera(intervalo))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nParado.")
