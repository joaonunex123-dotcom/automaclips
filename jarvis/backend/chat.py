"""Uma rodada de conversa: salva a pergunta, gera a resposta, transmite e salva.

Não conhece WebSocket. Recebe `enviar`, uma corrotina que manda um dict para o
cliente, e assim o mesmo código serve o WS e os testes.

Um lock global garante uma geração de cada vez. O llama-server atende um pedido
por vez de qualquer forma, e dois clientes intercalando tokens seria confuso.
"""
import logging
import time
from contextlib import aclosing

from .banco import Banco
from .estado import Estado
from .eventos import Barramento
from .llm import ErroLLM
from .tempo import agora_armazenado

log = logging.getLogger(__name__)

TAMANHO_MAX_TEXTO = 4000
TAMANHO_TAREFA = 60


async def _enviar_seguro(enviar, mensagem) -> bool:
    """Envia; False se o cliente já foi embora (não é erro do modelo)."""
    try:
        await enviar(mensagem)
        return True
    except Exception:
        return False


async def responder(texto: str, enviar, *, banco: Banco, provedor, estado: Estado,
                    barramento: Barramento, lock, prompt_sistema: str,
                    historico: int, relogio=time.monotonic):
    """Roda uma rodada completa. Devolve a mensagem final, ou None se falhou."""
    banco.salvar_mensagem("user", texto, criada_em=agora_armazenado())
    estado.atualizar(mascote="ouvindo")

    async with lock:
        return await _gerar(
            texto, enviar, banco=banco, provedor=provedor, estado=estado,
            barramento=barramento, prompt_sistema=prompt_sistema,
            historico=historico, relogio=relogio,
        )


async def _gerar(texto, enviar, *, banco, provedor, estado, barramento,
                 prompt_sistema, historico, relogio):
    resumo = texto if len(texto) <= TAMANHO_TAREFA else texto[:TAMANHO_TAREFA - 1] + "…"
    estado.atualizar(mascote="pensando", tarefa=f"respondendo: {resumo}")

    contexto = [{"role": "system", "content": prompt_sistema}]
    contexto += [
        {"role": m["papel"], "content": m["texto"]}
        for m in banco.ultimas_mensagens(historico)
    ]

    inicio = relogio()
    primeiro = None
    pedacos = []
    cliente_ativo = True
    try:
        async with aclosing(provedor.gerar(contexto)) as geracao:
            async for pedaco in geracao:
                if primeiro is None:
                    primeiro = relogio()
                    estado.atualizar(mascote="falando")
                pedacos.append(pedaco)
                barramento.publicar("token", {"texto": pedaco})
                cliente_ativo = await _enviar_seguro(
                    enviar, {"tipo": "token", "texto": pedaco}
                )
                if not cliente_ativo:
                    break
    except ErroLLM as e:
        return await _falhar(str(e), enviar, estado, barramento)
    except Exception:
        log.exception("falha inesperada ao gerar resposta")
        return await _falhar("erro interno ao gerar a resposta", enviar, estado, barramento)

    if not cliente_ativo:
        # O cliente caiu no meio. Não há a quem entregar a resposta parcial,
        # então ela não é salva: o histórico fica só com perguntas e respostas inteiras.
        log.info("cliente desconectou durante a resposta; geração descartada")
        estado.atualizar(mascote="descansando", tarefa=None)
        return None

    fim = relogio()
    primeiro = fim if primeiro is None else primeiro
    texto_final = "".join(pedacos)
    tokens = len(pedacos)
    tempo_total = round(fim - inicio, 3)
    tempo_geracao = fim - primeiro
    tokens_por_s = round(tokens / tempo_geracao, 2) if tempo_geracao > 0 and tokens else None

    salvo = banco.salvar_mensagem(
        "assistant", texto_final, criada_em=agora_armazenado(), provedor=provedor.nome,
        tokens=tokens, tokens_por_s=tokens_por_s, tempo_total_s=tempo_total,
    )
    final = {
        "tipo": "final",
        "mensagem_id": salvo["id"],
        "texto": texto_final,
        "provedor": provedor.nome,
        "tokens": tokens,
        "tokens_por_s": tokens_por_s,
        "tempo_total_s": tempo_total,
        "tempo_ate_primeiro_token_s": round(primeiro - inicio, 3),
    }
    estado.atualizar(mascote="descansando", tarefa=None)
    barramento.publicar("chat_final", final)
    await _enviar_seguro(enviar, final)
    return final


async def _falhar(mensagem, enviar, estado, barramento):
    estado.atualizar(mascote="erro", tarefa=None)
    barramento.publicar("chat_erro", {"erro": mensagem})
    await _enviar_seguro(enviar, {"tipo": "erro", "erro": mensagem})
    return None
