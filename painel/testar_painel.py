#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Teste rápido do painel de controle (rode na mesma pasta do painel_corrida.py):

    python testar_painel.py

Confere, em ordem:
  1. versão do Python e Tkinter (necessário para a janela do painel);
  2. painel_config.json (existe? está bem escrito? tem token e ID da conta?);
  3. token da Cloudflare (é válido?);
  4. estatísticas do Worker (o token + ID da conta conseguem ler o público?);
  5. arquivo oficial do TSE e o site (respondem?).
Cada linha sai como OK, ATENÇÃO ou FALHA, com a explicação.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
CTX = ssl.create_default_context()
VERDE, AMARELO, VERMELHO, FIM = "\033[92m", "\033[93m", "\033[91m", "\033[0m"
if os.name == "nt":
    os.system("")  # ativa cores no terminal do Windows


def linha(nivel, texto):
    cor = {"OK": VERDE, "ATENÇÃO": AMARELO, "FALHA": VERMELHO}[nivel]
    print(f"  {cor}{nivel:<8}{FIM} {texto}")


def pedir(url, dados=None, cab=None, timeout=15):
    req = urllib.request.Request(url, data=dados, headers=cab or {"User-Agent": "TestePainel/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read().decode("utf-8", "replace")
    except Exception as ex:
        return None, str(getattr(ex, "reason", ex))


print("\n1) Python e Tkinter")
v = sys.version_info
linha("OK" if v >= (3, 8) else "FALHA", f"Python {v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 8) else " (precisa 3.8 ou mais novo)"))
try:
    import tkinter
    linha("OK", f"Tkinter {tkinter.TkVersion} disponível")
except ImportError:
    linha("FALHA", "Tkinter não encontrado (Linux: sudo apt install python3-tk)")

print("\n2) painel_config.json")
arq = os.path.join(AQUI, "painel_config.json")
tok = acc = ""
if not os.path.exists(arq):
    linha("ATENÇÃO", f"não encontrado em {AQUI} (o painel funciona, só sem o quadro de público)")
else:
    try:
        cfg = json.load(open(arq, encoding="utf-8"))
        tok, acc = str(cfg.get("cf_api_token", "")).strip(), str(cfg.get("cf_account_id", "")).strip()
        linha("OK", "arquivo bem escrito (JSON válido)")
        linha("OK" if tok and not tok.upper().startswith("COLE") else "FALHA", "token " + ("preenchido" if tok and not tok.upper().startswith("COLE") else "não preenchido"))
        ok_id = len(acc) == 32 and all(c in "0123456789abcdef" for c in acc.lower())
        linha("OK" if ok_id else "FALHA", "ID da conta " + ("preenchido" if ok_id else f"inválido ou não preenchido ('{acc[:12]}…' deveria ter 32 letras/números)"))
    except ValueError as ex:
        linha("FALHA", f"arquivo com erro de escrita: {ex}")

print("\n3) Token da Cloudflare")
if tok and not tok.upper().startswith("COLE"):
    st, corpo = pedir("https://api.cloudflare.com/client/v4/user/tokens/verify", cab={"Authorization": "Bearer " + tok})
    try:
        j = json.loads(corpo)
        if j.get("success") and (j.get("result") or {}).get("status") == "active":
            linha("OK", "token válido e ativo")
        else:
            linha("FALHA", "token recusado: " + str((j.get("errors") or [{}])[0].get("message", corpo[:120])))
    except ValueError:
        linha("FALHA", f"sem resposta da Cloudflare ({st}): {corpo[:120]}")
else:
    linha("ATENÇÃO", "pulado (sem token)")

print("\n4) Estatísticas do site (público)")
if tok and acc and not acc.upper().startswith("COLE"):
    q = '{"query":"query($a:String!){viewer{accounts(filter:{accountTag:$a}){workersInvocationsAdaptive(limit:10,filter:{scriptName:\\"corridaparapresidencia\\",datetime_geq:\\"%s\\"}){sum{requests}}}}}","variables":{"a":"%s"}}'
    from datetime import datetime, timezone
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    st, corpo = pedir("https://api.cloudflare.com/client/v4/graphql", dados=(q % (hoje, acc)).encode(),
                      cab={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        j = json.loads(corpo)
        if j.get("errors"):
            linha("FALHA", "Cloudflare: " + str(j["errors"][0].get("message", ""))[:150])
        elif not j["data"]["viewer"]["accounts"]:
            linha("FALHA", "ID da conta não encontrado para este token")
        else:
            n = sum((l.get("sum") or {}).get("requests", 0) for l in j["data"]["viewer"]["accounts"][0]["workersInvocationsAdaptive"])
            linha("OK", f"lendo estatísticas: {n} consultas ao site hoje (desde 21h de ontem, horário de Brasília)")
    except (ValueError, KeyError, TypeError):
        linha("FALHA", f"resposta inesperada ({st}): {corpo[:150]}")
else:
    linha("ATENÇÃO", "pulado (falta token ou ID da conta)")

print("\n5) TSE e site")
st, corpo = pedir("https://resultados.tse.jus.br/oficial/ele2026/6258/dados/br/br-c0001-e006258-u.json")
try:
    j = json.loads(corpo); linha("OK", f"arquivo oficial do TSE respondeu (HTTP {st}), apurado: {j.get('s', {}).get('pst', '?')}%")
except ValueError:
    linha("FALHA", f"TSE não respondeu ({st}): {corpo[:100]}")
st, corpo = pedir("https://corridaparapresidencia.eleicoes.workers.dev/api/status")
linha("OK" if st == 200 else "FALHA", f"site respondeu (HTTP {st})" if st == 200 else f"site não respondeu ({st}): {corpo[:100]}")
print("\nSe tudo estiver OK, abra o painel:  python painel_corrida.py\n")
