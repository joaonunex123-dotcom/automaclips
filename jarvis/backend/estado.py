"""Estado do mascote e tarefa atual. Cada mudança vira evento 'status'."""

MASCOTE_ESTADOS = ("descansando", "ouvindo", "pensando", "falando", "erro")

_NAO_MUDA = object()  # sentinela: "este campo não foi passado"


class Estado:
    def __init__(self, barramento):
        self._barramento = barramento
        self.mascote = "descansando"
        self.tarefa_atual = None

    def snapshot(self) -> dict:
        return {"mascote": self.mascote, "tarefa_atual": self.tarefa_atual}

    def atualizar(self, mascote=_NAO_MUDA, tarefa=_NAO_MUDA) -> None:
        """Muda só os campos passados. Publica 'status' se algo realmente mudou."""
        mudou = False
        if mascote is not _NAO_MUDA:
            if mascote not in MASCOTE_ESTADOS:
                raise ValueError(f"estado de mascote desconhecido: {mascote!r}")
            if mascote != self.mascote:
                self.mascote = mascote
                mudou = True
        if tarefa is not _NAO_MUDA and tarefa != self.tarefa_atual:
            self.tarefa_atual = tarefa
            mudou = True
        if mudou:
            self._barramento.publicar("status", self.snapshot())
