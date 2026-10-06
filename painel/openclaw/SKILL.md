---
name: corrida-presidencia
description: Situação da Corrida para a Presidência (site da apuração do 2º turno): TSE, site, dados e público.
---

# Corrida para a Presidência · monitor

O monitor `monitor_corrida.py` roda no Raspberry Pi e grava a situação do site a cada rodada.

Quando o usuário perguntar sobre a corrida, o site da apuração, a eleição, o TSE ou "como está o site",
use o `exec` para rodar:

```bash
python3 ~/corrida/painel/monitor_corrida.py --resumo
```

Se o resumo disser que tem mais de 5 minutos (monitor parado) ou o usuário pedir "checar agora", rode:

```bash
python3 ~/corrida/painel/monitor_corrida.py --uma-vez
```

Para ver o que mudou durante a noite (novos dados, mudanças de situação):

```bash
tail -n 20 ~/corrida/painel/monitor_corrida.log
```

Para saber se o serviço do monitor está rodando (e reiniciar, se o usuário pedir):

```bash
systemctl --user status corrida-monitor --no-pager | head -5
systemctl --user restart corrida-monitor
```

Regras:
- Responda em português, curto: situação (TUDO OK / ATENÇÃO / ERRO), % de urnas, 1º e 2º e o que estiver com problema.
- Não altere arquivos do site, do GitHub nem da Cloudflare. Este skill é só para acompanhar.
- O resultado oficial é sempre o do TSE (resultados.tse.jus.br); o site é independente.
