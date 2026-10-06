# Painel de controle (programa de mesa)

Programa em Python, **separado do site**, para acompanhar no computador se tudo está funcionando no dia da apuração. Não é publicado na Cloudflare: o site usa só `public/` e `src/`.

## O que mostra
- Luzes de estado: TSE respondendo, site no ar, site lendo o TSE e dados chegando (verde / amarelo / vermelho).
- Apuração: % de urnas, seções totalizadas e horário da última totalização.
- Totais: eleitorado, comparecimento, abstenção, válidos, brancos e nulos.
- Classificação dos 2 candidatos do 2º turno e a **diferença de votos** entre eles.
- Botão **▤ Gráfico**: janela com a evolução minuto a minuto (lida do histórico gravado pelo site), no horário de Brasília.
- **Público no site** (opcional): estimativa de pessoas online e consultas do dia, com o quanto da cota grátis da Cloudflare já foi usado.
- Botões para abrir o site, os resultados do TSE e o `/api/verificar`.

## Quando consulta (pelo relógio do computador)
| Horário | Consultas |
|---|---|
| antes das 16h30 | uma ao abrir; depois, só pelo botão ⟳ |
| 16h30 às 17h | a cada 60 s (TSE e site respondendo) |
| a partir das 17h | a cada `--intervalo` segundos (padrão 30) |

## Como rodar
```bash
python painel_corrida.py
python painel_corrida.py --intervalo 20
```
Só biblioteca padrão do Python (Tkinter). No Linux: `sudo apt install python3-tk`.

## Público no site (opcional)

O painel lê as estatísticas do Worker na Cloudflare e estima:
- **pessoas online** ≈ consultas por minuto ÷ 2 (cada página aberta consulta o servidor 2 vezes por minuto), média dos últimos 5 minutos, com ~2 min de atraso; as consultas do próprio painel são descontadas;
- **consultas do dia** e a porcentagem da cota grátis (100 mil por dia, zera às 21h de Brasília).

Só conta quem está com a página aberta **depois das 17h**: antes disso a página não consulta o servidor.

Para ativar:
1. No painel da Cloudflare, clique no ícone do seu perfil → **Tokens de API** → **Criar token** → **Criar token personalizado**.
2. Em *Permissões*, escolha **Conta → Account Analytics → Ler**. Em *Recursos da conta*, inclua a sua conta. Crie e copie o token.
3. Copie o **ID da conta** (aparece no endereço do painel, `dash.cloudflare.com/<ID da conta>/...`, ou na página do Worker, em *ID da conta*).
4. Na mesma pasta do `painel_corrida.py`, copie `painel_config.exemplo.json` para `painel_config.json` e preencha:
   ```json
   {"cf_api_token": "COLE_O_TOKEN_AQUI", "cf_account_id": "COLE_O_ID_DA_CONTA_AQUI"}
   ```
   Ou use as variáveis de ambiente `CF_API_TOKEN` e `CF_ACCOUNT_ID`.

O arquivo `painel_config.json` está no `.gitignore`: **nunca envie o token para o GitHub.**

## Sem tela (Raspberry Pi) e com o OpenClaw

`monitor_corrida.py` faz as mesmas checagens do painel, sem janela, e grava a situação em
`estado_corrida.txt` (resumo em português), `estado_corrida.json` e `monitor_corrida.log`.

```bash
git clone https://github.com/celibertojr/corrida-para-presidencia_2turno.git ~/corrida
cp painel_config.json ~/corrida/painel/          # opcional: público no site
python3 ~/corrida/painel/monitor_corrida.py --uma-vez   # teste
# deixar rodando sempre (reinicia sozinho):
mkdir -p ~/.config/systemd/user && cp ~/corrida/painel/corrida-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now corrida-monitor
sudo loginctl enable-linger $USER
# skill do OpenClaw:
mkdir -p ~/.openclaw/workspace/skills/corrida-presidencia && cp ~/corrida/painel/openclaw/SKILL.md ~/.openclaw/workspace/skills/corrida-presidencia/
```

### Avisos automáticos

O monitor avisa sozinho quando algo dá errado (a partir das 16h30): TSE sem responder, site fora do ar,
site sem ler o TSE, página ainda "não começou" depois das 17h, ou nenhum dado novo há mais de 10 min.
Cada problema avisa uma vez, lembra a cada 30 min se continuar e avisa quando volta ao normal.

- **Envio:** no `painel_config.json`, `telegram_token` + `telegram_chat_id` (bot do Telegram) e/ou
  `alerta_cmd` (qualquer comando; a mensagem vem na variável `ALERTA_MSG`). Tudo fica em `alertas.log`.
- **Controle** (o Kermit/OpenClaw usa pela skill): `--alertas desligar`, `--alertas ligar`,
  `--silenciar 60`, `--alertas status`. Teste: `--testar-aviso`.

### Avisos pelo próprio Kermit (WhatsApp)

Cada aviso também entra numa fila (`alertas_pendentes.txt`). O heartbeat do OpenClaw roda
`monitor_corrida.py --pendentes` a cada poucos minutos: sem avisos, a saída é `NADA` e o Kermit responde
`HEARTBEAT_OK` (nada é enviado); com avisos, ele manda o texto no WhatsApp. Instruções em `openclaw/HEARTBEAT.md`.
Avisos desligados ou silenciados não entram na fila.
