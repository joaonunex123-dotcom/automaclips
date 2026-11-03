"""Persistência em SQLite: notas, agenda, lembretes e histórico do chat.

Uma conexão por operação (abre, faz, fecha). Mais lento que manter uma conexão
aberta, mas sem estado escondido e seguro entre threads; para um banco local
com poucas centenas de linhas a diferença não aparece. WAL deixa a leitura da
tela não travar a escrita do chat.

Este módulo só grava e lê. Não sabe o que é um lembrete disparado nem um token.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS notas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo TEXT NOT NULL,
    texto TEXT NOT NULL DEFAULT '',
    criada_em TEXT NOT NULL,
    atualizada_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agenda (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo TEXT NOT NULL,
    descricao TEXT NOT NULL DEFAULT '',
    quando TEXT NOT NULL,            -- UTC, ISO 8601
    criada_em TEXT NOT NULL,
    atualizada_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agenda_quando ON agenda (quando);
CREATE TABLE IF NOT EXISTS lembretes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    texto TEXT NOT NULL,
    quando TEXT NOT NULL,            -- UTC, ISO 8601
    disparado_em TEXT,               -- NULL enquanto não disparou
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lembretes_quando ON lembretes (quando);
CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    papel TEXT NOT NULL CHECK (papel IN ('user', 'assistant')),
    texto TEXT NOT NULL,
    criada_em TEXT NOT NULL,
    provedor TEXT NOT NULL DEFAULT '',
    tokens INTEGER,
    tokens_por_s REAL,
    tempo_total_s REAL
);
"""

# Tabelas que têm CRUD genérico, com a ordem da listagem. Nome de tabela nunca
# vem do usuário: só destas chaves, que são constantes do código.
ORDEM_LISTAGEM = {
    "notas": "atualizada_em DESC, id DESC",
    "agenda": "quando ASC, id ASC",
    "lembretes": "quando ASC, id ASC",
}


class Banco:
    def __init__(self, caminho):
        self.caminho = str(caminho)
        Path(self.caminho).parent.mkdir(parents=True, exist_ok=True)
        with self._conexao() as conn:
            conn.executescript(ESQUEMA)

    @contextmanager
    def _conexao(self):
        conn = sqlite3.connect(self.caminho, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- CRUD genérico (notas, agenda, lembretes) --------------------------

    def listar(self, tabela):
        _checar_tabela(tabela)
        with self._conexao() as conn:
            linhas = conn.execute(
                f"SELECT * FROM {tabela} ORDER BY {ORDEM_LISTAGEM[tabela]}"
            ).fetchall()
        return [dict(linha) for linha in linhas]

    def obter(self, tabela, id_):
        _checar_tabela(tabela)
        with self._conexao() as conn:
            linha = conn.execute(f"SELECT * FROM {tabela} WHERE id = ?", (id_,)).fetchone()
        return dict(linha) if linha else None

    def inserir(self, tabela, dados: dict):
        """`dados` vem sempre do código (colunas fixas), nunca da entrada crua."""
        _checar_tabela(tabela)
        colunas = ", ".join(dados)
        marcas = ", ".join("?" for _ in dados)
        with self._conexao() as conn:
            cursor = conn.execute(
                f"INSERT INTO {tabela} ({colunas}) VALUES ({marcas})", list(dados.values())
            )
            linha = conn.execute(
                f"SELECT * FROM {tabela} WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(linha)

    def atualizar(self, tabela, id_, dados: dict):
        _checar_tabela(tabela)
        if not dados:
            return self.obter(tabela, id_)
        atribuicoes = ", ".join(f"{coluna} = ?" for coluna in dados)
        with self._conexao() as conn:
            cursor = conn.execute(
                f"UPDATE {tabela} SET {atribuicoes} WHERE id = ?",
                [*dados.values(), id_],
            )
            if cursor.rowcount == 0:
                return None
            linha = conn.execute(f"SELECT * FROM {tabela} WHERE id = ?", (id_,)).fetchone()
        return dict(linha)

    def apagar(self, tabela, id_) -> bool:
        _checar_tabela(tabela)
        with self._conexao() as conn:
            cursor = conn.execute(f"DELETE FROM {tabela} WHERE id = ?", (id_,))
            return cursor.rowcount > 0

    # --- consultas específicas ---------------------------------------------

    def proximos_agenda(self, a_partir_utc: str, limite: int):
        with self._conexao() as conn:
            linhas = conn.execute(
                "SELECT * FROM agenda WHERE quando >= ? ORDER BY quando ASC, id ASC LIMIT ?",
                (a_partir_utc, limite),
            ).fetchall()
        return [dict(linha) for linha in linhas]

    def lembretes_pendentes(self):
        """Lembretes que ainda não dispararam, do mais cedo ao mais tarde."""
        with self._conexao() as conn:
            linhas = conn.execute(
                "SELECT * FROM lembretes WHERE disparado_em IS NULL ORDER BY quando ASC, id ASC"
            ).fetchall()
        return [dict(linha) for linha in linhas]

    def proximos_lembretes(self, a_partir_utc: str, limite: int):
        with self._conexao() as conn:
            linhas = conn.execute(
                "SELECT * FROM lembretes WHERE disparado_em IS NULL AND quando >= ? "
                "ORDER BY quando ASC, id ASC LIMIT ?",
                (a_partir_utc, limite),
            ).fetchall()
        return [dict(linha) for linha in linhas]

    def marcar_disparado(self, id_, quando_disparo_utc: str):
        with self._conexao() as conn:
            cursor = conn.execute(
                "UPDATE lembretes SET disparado_em = ?, atualizado_em = ? WHERE id = ?",
                (quando_disparo_utc, quando_disparo_utc, id_),
            )
            if cursor.rowcount == 0:
                return None
            linha = conn.execute("SELECT * FROM lembretes WHERE id = ?", (id_,)).fetchone()
        return dict(linha)

    # --- histórico do chat -------------------------------------------------

    def salvar_mensagem(self, papel, texto, criada_em, provedor="", tokens=None,
                        tokens_por_s=None, tempo_total_s=None):
        with self._conexao() as conn:
            cursor = conn.execute(
                "INSERT INTO mensagens (papel, texto, criada_em, provedor, tokens, "
                "tokens_por_s, tempo_total_s) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (papel, texto, criada_em, provedor, tokens, tokens_por_s, tempo_total_s),
            )
            linha = conn.execute(
                "SELECT * FROM mensagens WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(linha)

    def ultimas_mensagens(self, limite: int):
        """As `limite` mensagens mais recentes, em ordem cronológica."""
        with self._conexao() as conn:
            linhas = conn.execute(
                "SELECT * FROM (SELECT * FROM mensagens ORDER BY id DESC LIMIT ?) "
                "ORDER BY id ASC",
                (limite,),
            ).fetchall()
        return [dict(linha) for linha in linhas]


def _checar_tabela(tabela):
    if tabela not in ORDEM_LISTAGEM:
        raise ValueError(f"tabela desconhecida: {tabela!r}")
