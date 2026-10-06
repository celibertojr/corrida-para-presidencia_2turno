# Corrida para a Presidência · avisos

A cada heartbeat, rode:

```bash
python3 ~/corrida/painel/monitor_corrida.py --pendentes
```

- Se a saída for exatamente `NADA`: responda apenas `HEARTBEAT_OK` (nada é enviado).
- Se houver avisos: envie o texto dos avisos ao usuário, como está.
