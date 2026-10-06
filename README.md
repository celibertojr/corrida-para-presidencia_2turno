<div align="center">

# 🏃 Corrida para a Presidência · 2º turno

**A apuração do 2º turno das eleições presidenciais de 2026 como uma prova de atletismo em 3D.**

Dois corredores, uma volta na pista = 100% das urnas apuradas.
Todos os números vêm da divulgação oficial do TSE.

[![Cloudflare Workers](https://img.shields.io/badge/Cloudflare-Workers-F38020?logo=cloudflare&logoColor=white)](https://developers.cloudflare.com/workers/)
[![Three.js](https://img.shields.io/badge/Three.js-r128-000000?logo=threedotjs&logoColor=white)](https://threejs.org/)
[![Dados](https://img.shields.io/badge/dados-TSE%20oficial-1F6FEB)](https://resultados.tse.jus.br)

**🌐 [corridaparapresidencia.eleicoes.workers.dev](https://corridaparapresidencia.eleicoes.workers.dev)**

<img src="docs/chegada.png" alt="Os dois candidatos do 2º turno na largada, sob a faixa de saída" width="860">

</div>

---

> Versão do **2º turno (25/10/2026)**. A versão do 1º turno está preservada em
> [celibertojr/corrida-para-presidencia](https://github.com/celibertojr/corrida-para-presidencia).

## ✨ O que há de novo

| Recurso | O que faz |
|---|---|
| **2º turno** | Lula (13, PT) e Flávio Bolsonaro (22, PL) nas raias do meio de uma pista de 8 raias. A análise diz quando a vitória está *garantida* pelos números, quando há *tendência* e quando a disputa segue aberta. |
| **Diferença de votos** | O placar mostra a diferença entre o 1º e o 2º, em votos e em pontos percentuais. |
| **Estatísticas** | Botão com urnas apuradas, ritmo da apuração e previsão de 100% (estimativas), votos de cada candidato, diferença, trocas de liderança, comparecimento, abstenção, válidos, brancos e nulos. |
| **Gráfico minuto a minuto** | Botão com a evolução do % de votos válidos de cada candidato e da diferença entre eles, desde as 17h, com tabela e dica ao passar o mouse. |
| **Histórico no servidor** | A cada minuto o próprio servidor consulta o TSE e grava um ponto (o TSE só mantém o arquivo mais recente). Funciona mesmo sem ninguém com o site aberto. |
| **Modo leve** | Para computadores e celulares mais lentos: menos resolução, sem sombras, sem torcida, até 30 quadros/s. Liga sozinho se a página ficar lenta, pelo botão ou pelo endereço com `?leve=1`. |
| **Painel de controle** | O programa em Python (`painel/`) também foi atualizado: 2º turno, diferença de votos e janela com o gráfico. |

## 📏 Regras da corrida

| Regra | Como funciona |
|---|---|
| **Uma volta = 100% das urnas** | O líder fica na posição igual à porcentagem de urnas apuradas. |
| **Posição do 2º** | Proporcional aos votos: `posição = % apurada × votos do 2º ÷ votos do 1º`. |
| **Vitória garantida** | Quando a diferença supera todos os eleitores das seções ainda não apuradas. |
| **Tendência** | Quando a diferença supera o dobro dos votos válidos esperados no ritmo atual. |
| **Com 100% das urnas** | Mostra "mais votado" até o TSE marcar o candidato como eleito no arquivo oficial. |
| **Botão "Vencedores"** | Pódio 3D liberado acima de 99% das urnas, ou só com 99,99% se a diferença estiver dentro da margem de incerteza. |

## 🧱 Como funciona

```mermaid
flowchart LR
    TSE[("TSE<br/>JSON oficial")] -->|no máximo a cada 20 s| W["Worker<br/>/api/resultado"]
    TSE -->|1 vez por minuto, agendado| H["Histórico<br/>(Durable Object)"]
    H --> G["/api/historico"]
    W --> P["Página 3D"]
    G --> P
```

- **`public/index.html`**: o site inteiro (3D, interface, estatísticas, gráfico, modo leve).
- **`src/worker.js`**: lê o JSON do TSE com cache, grava o histórico minuto a minuto e responde `/api/resultado`, `/api/historico`, `/api/status` e `/api/verificar`.
- **`wrangler.jsonc`**: configuração do Worker, incluindo o agendador (`* * * * *`) e o armazenamento do histórico (Durable Object com SQLite, disponível no plano grátis).
- **Antes de 25/10 às 17h**, nem a página nem o servidor consultam o TSE; a página mostra a contagem regressiva.

## 🔧 Variáveis do Worker

| Variável | Para que serve |
|---|---|
| `TSE_URL` | URL do JSON do TSE, se o tribunal usar outro caminho. Padrão: eleição **6258** (2º turno). |
| `TSE_START` | Início da divulgação (ISO 8601). Padrão: `2026-10-25T20:00:00Z` (17h de Brasília). |

`/api/status` mostra a URL e o horário em uso; `/api/verificar` lê o TSE na hora.

## 💻 Rodar localmente

```bash
npx wrangler dev --test-scheduled
# disparar o agendador manualmente:  curl "http://localhost:8787/__scheduled?cron=*+*+*+*+*"
```

## ⚠️ Aviso

Projeto **independente, sem qualquer vínculo com o Tribunal Superior Eleitoral (TSE)**, com candidatos ou partidos. Os números vêm da divulgação do TSE, mas podem ter atraso ou falhas, e as indicações de tendência são apenas estimativas. **Para saber quem foi eleito, use somente as fontes oficiais do TSE:** [resultados.tse.jus.br](https://resultados.tse.jus.br).

## 🤖 Feito com IA

Todo o código foi escrito por **inteligência artificial** (Claude, da Anthropic), a partir de pedidos e revisões feitos em conversa, como experimento do que dá para construir assim.

## 📚 Referências

- [TSE — Divulgação de Resultados](https://resultados.tse.jus.br)
- [Cloudflare Workers — Static Assets](https://developers.cloudflare.com/workers/static-assets/)
- [Cloudflare Workers — Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Cloudflare Durable Objects — SQLite storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
- [Three.js](https://threejs.org/)
