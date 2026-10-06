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
import subprocess
import sys
import urllib.parse
import urllib.request
import time
from datetime import datetime, timezone

import painel_corrida as P

AQUI = os.path.dirname(os.path.abspath(__file__))
ARQ_JSON = os.path.join(AQUI, "estado_corrida.json")
ARQ_TXT = os.path.join(AQUI, "estado_corrida.txt")
ARQ_LOG = os.path.join(AQUI, "monitor_corrida.log")
ESPERA_ANTES = 3600  # antes das 16h30: uma checagem por hora
ARQ_ALERTAS = os.path.join(AQUI, "alertas.json")       # liga/desliga/silencia (o OpenClaw muda por comando)
ARQ_ALERTAS_LOG = os.path.join(AQUI, "alertas.log")    # todos os avisos enviados
ARQ_PENDENTES = os.path.join(AQUI, "alertas_pendentes.txt")  # fila para o Kermit (OpenClaw) entregar no WhatsApp
REPETIR_MIN = 30   # problema que continua: lembra no máximo a cada 30 min
CARENCIA_TSE_MIN = 5  # o arquivo do TSE pode demorar alguns minutos depois das 17h: só avisa depois disso


# ---------------------------------------------------------------------------
# Avisos automáticos
# Regras (só a partir das 16h30):
#   - TSE sem responder em 3 rodadas seguidas (a partir das 17h05);
#   - site fora do ar em 2 rodadas seguidas;
#   - site sem conseguir ler o TSE em 3 rodadas seguidas (a partir das 17h05);
#   - página ainda "não começou" depois das 17h, ou início configurado errado;
#   - nenhum dado novo do TSE há mais de 10 min durante a apuração.
# Cada problema avisa uma vez, lembra a cada 30 min se continuar e avisa quando volta ao normal.
# Controle (o OpenClaw usa estes comandos):
#   monitor_corrida.py --alertas desligar | ligar | status      e      --silenciar 60   (minutos)
# Envio: Telegram direto (telegram_token + telegram_chat_id no painel_config.json) e/ou
#        um comando qualquer (alerta_cmd), que recebe a mensagem na variável ALERTA_MSG.
#        Sempre fica registrado em alertas.log.
# ---------------------------------------------------------------------------
def ler_ctrl():
    try:
        with open(ARQ_ALERTAS, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def gravar_ctrl(c):
    gravar(ARQ_ALERTAS, json.dumps(c, ensure_ascii=False, indent=1))


def estado_alertas():
    c = ler_ctrl()
    if c.get("desligado"):
        return False, "desligados (ligue com: --alertas ligar)"
    ate = c.get("silencio_ate", 0)
    if ate > time.time():
        return False, f"silenciados até {datetime.fromtimestamp(ate, P.BRT):%H:%M}"
    return True, "ligados"


def config_envio():
    try:
        with open(P.ARQ_CONFIG, encoding="utf-8-sig") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def enviar(msg):
    """Envia o aviso pelos meios configurados; nunca derruba o monitor."""
    with open(ARQ_ALERTAS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{agora_brt():%d/%m %H:%M:%S}  {msg}\n")
    ligado, porque = estado_alertas()
    if not ligado:
        registrar(f"Aviso não enviado (alertas {porque}): {msg.splitlines()[0]}")
        return
    # fila para o Kermit: o heartbeat do OpenClaw roda --pendentes e entrega pelo WhatsApp
    with open(ARQ_PENDENTES, "a", encoding="utf-8") as f:
        f.write(f"[{agora_brt():%H:%M}] {msg}\n\n")
    cfg, enviado = config_envio(), False
    tok, chat = str(cfg.get("telegram_token", "")).strip(), str(cfg.get("telegram_chat_id", "")).strip()
    if tok and chat and not tok.upper().startswith("COLE"):
        try:
            dados = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=dados), timeout=15, context=P.CTX).read()
            enviado = True
        except Exception as ex:
            registrar("Falha ao enviar pelo Telegram: " + repr(ex)[:120])
    cmd = str(cfg.get("alerta_cmd", "")).strip()
    if cmd:
        try:
            subprocess.run(cmd, shell=True, timeout=60, env=dict(os.environ, ALERTA_MSG=msg))
            enviado = True
        except Exception as ex:
            registrar("Falha no alerta_cmd: " + repr(ex)[:120])
    registrar(("Aviso enviado: " if enviado else "Aviso só registrado (nenhum envio configurado): ") + msg.splitlines()[0])


def checar_alertas(itens, res, mem):
    agora = datetime.now(timezone.utc)
    falta = (P.INICIO_UTC - agora).total_seconds()
    if falta > P.AQUECIMENTO_MIN * 60:
        return  # antes das 16h30 não avisa nada
    passou = -falta / 60  # minutos desde as 17h (negativo antes)
    cont = mem.setdefault("seguidas", {})
    s = res["site"]
    def conta(chave, ruim):
        cont[chave] = cont.get(chave, 0) + 1 if ruim else 0
        return cont[chave]
    problemas = {}
    if conta("tse", not res["tse"]["ok"]) >= 3 and passou >= CARENCIA_TSE_MIN:
        problemas["tse"] = "o arquivo do TSE não responde: " + str(res["tse"]["erro"] or "sem resposta")
    if conta("site", "upstream" not in (s["status"] or {})) >= 2:
        problemas["site"] = "o site está fora do ar: " + str(s["erro"] or "resposta inesperada")
    if conta("leitura", not (s["verificar"] or {}).get("ok")) >= 3 and passou >= CARENCIA_TSE_MIN:
        problemas["leitura"] = "o site não consegue ler o TSE"
    n_le, det_le = itens["leitura"]
    if n_le == "erro" and "TSE_START" in det_le:
        problemas["inicio"] = "a página ainda acha que a apuração não começou (confira TSE_START na Cloudflare)"
    n_si, det_si = itens["site"]
    if n_si == "atencao" and "início configurado" in det_si:
        problemas["inicio_cfg"] = det_si
    n_da, det_da = itens["dados"]
    if passou >= 0 and n_da == "atencao" and "sem novidade" in det_da:
        problemas["parado"] = det_da
    ativos = mem.setdefault("alertas", {})
    agora_s = time.time()
    novos = [k for k in problemas if k not in ativos]
    lembrar = [k for k in problemas if k in ativos and agora_s - ativos[k][0] >= REPETIR_MIN * 60]
    resolvidos = [k for k in list(ativos) if k not in problemas]
    if novos or lembrar:
        titulo = "⚠️ Corrida para a Presidência: problema" if novos else "⏰ Corrida para a Presidência: o problema continua"
        msg = titulo + "\n" + "\n".join("• " + problemas[k] for k in problemas) + \
              "\n(para parar os avisos: --alertas desligar  ou  --silenciar 60)"
        enviar(msg)
        for k in novos + lembrar:
            ativos[k] = (agora_s, problemas[k])
    textos = [ativos.pop(k)[1] for k in resolvidos]
    if resolvidos and not problemas:
        enviar("✅ Corrida para a Presidência: voltou ao normal.")
    elif resolvidos:
        enviar("✅ Corrida para a Presidência: resolvido\n" + "\n".join("• " + t for t in textos))


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
    try:
        checar_alertas(itens, res, mem)
    except Exception as ex:
        registrar("Erro ao checar avisos: " + repr(ex)[:150])
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
    ap.add_argument("--alertas", choices=["ligar", "desligar", "status"], help="liga, desliga ou mostra os avisos automáticos")
    ap.add_argument("--silenciar", type=int, metavar="MIN", help="silencia os avisos por MIN minutos (0 = acaba o silêncio)")
    ap.add_argument("--testar-aviso", action="store_true", help="envia um aviso de teste pelos meios configurados")
    ap.add_argument("--pendentes", action="store_true", help="mostra e esvazia os avisos ainda não entregues (usado pelo OpenClaw); sem avisos, imprime NADA")
    a = ap.parse_args()
    if a.pendentes:
        try:
            tmp = ARQ_PENDENTES + ".lendo"
            os.replace(ARQ_PENDENTES, tmp)  # pega a fila de uma vez (o monitor pode estar gravando)
            txt = open(tmp, encoding="utf-8").read().strip()
            os.remove(tmp)
        except FileNotFoundError:
            txt = ""
        print(txt or "NADA")
        return
    if a.alertas or a.silenciar is not None:
        c = ler_ctrl()
        if a.alertas == "ligar":
            c["desligado"] = False; c["silencio_ate"] = 0
        elif a.alertas == "desligar":
            c["desligado"] = True
        if a.silenciar is not None:
            c["silencio_ate"] = time.time() + max(0, a.silenciar) * 60
        if a.alertas != "status" or a.silenciar is not None:
            gravar_ctrl(c)
        print("Avisos automáticos:", estado_alertas()[1])
        return
    if a.testar_aviso:
        enviar("🔔 Teste de aviso da Corrida para a Presidência.")
        return
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
