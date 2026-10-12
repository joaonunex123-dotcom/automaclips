# ops/ — rodar 24/7 no mini PC

Arquivos de **exemplo**. Nada aqui é instalado pelo projeto; copie e ajuste.

| arquivo | para quê |
| --- | --- |
| `automaclips.service` | systemd, modo laço (recomendado): cada etapa no seu intervalo, reinicia se cair |
| `automaclips-ciclo.service` + `.timer` | systemd, um ciclo por hora (alternativa) |
| `com.automaclips.orquestrador.plist` | launchd (macOS), modo laço com `KeepAlive` |
| `pot_provider.sh` | servidor de PO token (download em 1080p) |
| `status.json` | **gerado** a cada ciclo: última execução, fila, quota, disco, token |

## Antes de ligar

1. O relógio do SO precisa estar em `America/Sao_Paulo` (`timedatectl set-timezone
   America/Sao_Paulo`): os horários de postagem são lidos no relógio local. O
   `status.json` e o log avisam se não estiver.
2. `.env` com os flags que o dono decidiu. Nada abaixo é ligado por padrão:
   `AUTO_PUBLISH`, `LIMPAR_FONTE_APOS_CLIPS`, `YOUTUBE_ENVIAR_CAPA`.
3. `YOUTUBE_TOKEN_VENCE_EM=AAAA-MM-DD` para o `status.json` contar os dias do
   token (app em modo Testing do Google tem refresh token de 7 dias).

## Monitorar

```bash
jq '{alertas, fila, quota: .quota_youtube.uploads_restantes, disco: .espaco_livre_gb, token: .token_youtube.dias_restantes}' ops/status.json
```

`alertas` vazio = tudo certo. Cada item é um texto (fila baixa, pouco disco,
token perto de vencer, fuso do SO errado, etapa que falhou, orçamento estourado).

## Garantias

* **Instância única:** `orquestrador.lock` (flock). Um segundo disparo sai com
  código 0 sem fazer nada; o SO solta a trava se o processo morrer.
* **Reinício seguro:** nenhum estado em memória. Vídeo em `baixado`/`transcrito`
  é retomado do ponto em que parou; um render interrompido é refeito (não há
  linha em `renders` até terminar).
* **Orçamento:** com `ORCAMENTO_USD` atingido o pipeline para e registra.
