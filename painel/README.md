# Painel de controle (programa de mesa)

Programa em Python, **separado do site**, para acompanhar no computador se tudo está funcionando no dia da apuração. Não é publicado na Cloudflare: o site usa só `public/` e `src/`.

## O que mostra
- Luzes de estado: TSE respondendo, site no ar, site lendo o TSE e dados chegando (verde / amarelo / vermelho).
- Apuração: % de urnas, seções totalizadas e horário da última totalização.
- Totais: eleitorado, comparecimento, abstenção, válidos, brancos e nulos.
- Classificação dos 2 candidatos do 2º turno e a **diferença de votos** entre eles.
- Botão **📈 Gráfico**: janela com a evolução minuto a minuto (lida do histórico gravado pelo site).
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
