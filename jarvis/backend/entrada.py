"""Entrada das teclas físicas.

Hoje a tecla chega por POST /input/key. Quando o GPIO estiver ligado, a leitura
dos pinos chama esta mesma função `registrar_tecla`, e o resto não muda.
"""
TECLAS = (1, 2, 3)
ACOES = ("down", "up")


def registrar_tecla(barramento, tecla: int, acao: str) -> dict:
    if tecla not in TECLAS:
        raise ValueError(f"tecla inválida: {tecla!r}")
    if acao not in ACOES:
        raise ValueError(f"ação inválida: {acao!r}")
    evento = {"tecla": tecla, "acao": acao}
    barramento.publicar("tecla", evento)
    return evento
