"""Configuração do Jarvis, lida do .env e do ambiente.

Sem dependência extra: o .env é um KEY=VALUE simples. Comentário só em linha
inteira (começando com #). Variável já presente no ambiente vence o .env, o que
deixa o systemd ajustar um valor sem editar o arquivo.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Pasta jarvis/ (um nível acima de backend/).
RAIZ = Path(__file__).resolve().parent.parent

LLM_PROVEDORES = ("mock", "llamacpp")
TAMANHO_MINIMO_TOKEN = 16

# Origens liberadas por CORS: localhost e a faixa de IP da Tailscale
# (100.64.0.0/10), além dos nomes *.ts.net do MagicDNS.
CORS_PADRAO = (
    r"^https?://(localhost|127\.0\.0\.1|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r"|[a-z0-9-]+\.[a-z0-9-]+\.ts\.net)(:\d+)?$"
)

PROMPT_PADRAO = (
    "Você é o Jarvis, um assistente de mesa. Responda em português, de forma curta e direta."
)


class ErroConfiguracao(Exception):
    """Variável ausente ou inválida. A mensagem diz qual."""


@dataclass(frozen=True)
class Config:
    token: str
    llm: str = "mock"
    llama_url: str = "http://127.0.0.1:8080"
    llama_timeout_s: float = 120.0
    llama_max_tokens: int = 512
    mock_atraso_s: float = 0.05
    tz: str = "America/Sao_Paulo"
    db_path: str = str(RAIZ / "dados" / "jarvis.db")
    tela_dir: str = str(RAIZ / "screen")
    thermal_dir: str = "/sys/class/thermal"
    cors_regex: str = CORS_PADRAO
    historico_msgs: int = 10
    system_prompt: str = PROMPT_PADRAO

    @property
    def zona(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


def ler_arquivo_env(caminho) -> dict:
    """Lê um .env simples. Arquivo inexistente devolve dicionário vazio."""
    valores = {}
    try:
        linhas = Path(caminho).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return valores
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        valores[chave.strip()] = valor
    return valores


def _texto(valores, nome, padrao):
    valor = valores.get(nome, "")
    return valor.strip() if valor and valor.strip() else padrao


def _numero(valores, nome, padrao, tipo):
    bruto = valores.get(nome, "")
    if not bruto or not bruto.strip():
        return padrao
    try:
        return tipo(bruto.strip())
    except ValueError as e:
        raise ErroConfiguracao(f"{nome} precisa ser número, veio {bruto!r}") from e


def carregar_config(ambiente=None, arquivo_env=None) -> Config:
    """Monta a Config.

    Sem `ambiente`: lê o .env (arquivo_env, ou RAIZ/.env) e depois o os.environ,
    que vence. Com um dict: usa só ele. Os testes passam o dict e não tocam no
    .env da máquina.
    """
    if ambiente is None:
        valores = ler_arquivo_env(arquivo_env or RAIZ / ".env")
        valores.update(os.environ)
    else:
        valores = dict(ambiente)

    token = _texto(valores, "JARVIS_TOKEN", "")
    if len(token) < TAMANHO_MINIMO_TOKEN:
        raise ErroConfiguracao(
            f"JARVIS_TOKEN ausente ou curto (mínimo {TAMANHO_MINIMO_TOKEN} caracteres). "
            "Gere um com: openssl rand -hex 32"
        )

    llm = _texto(valores, "JARVIS_LLM", "mock").lower()
    if llm not in LLM_PROVEDORES:
        raise ErroConfiguracao(f"JARVIS_LLM inválido: {llm!r} (use mock ou llamacpp)")

    tz = _texto(valores, "JARVIS_TZ", "America/Sao_Paulo")
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as e:
        raise ErroConfiguracao(f"JARVIS_TZ desconhecido: {tz!r}") from e

    llama_url = _texto(valores, "LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")
    if not llama_url.startswith(("http://", "https://")):
        raise ErroConfiguracao(f"LLAMA_URL precisa começar com http:// ou https://: {llama_url!r}")

    return Config(
        token=token,
        llm=llm,
        llama_url=llama_url,
        llama_timeout_s=_numero(valores, "LLAMA_TIMEOUT_S", 120.0, float),
        llama_max_tokens=_numero(valores, "LLAMA_MAX_TOKENS", 512, int),
        mock_atraso_s=_numero(valores, "JARVIS_MOCK_ATRASO_S", 0.05, float),
        tz=tz,
        db_path=_texto(valores, "JARVIS_DB_PATH", str(RAIZ / "dados" / "jarvis.db")),
        tela_dir=_texto(valores, "JARVIS_TELA_DIR", str(RAIZ / "screen")),
        thermal_dir=_texto(valores, "JARVIS_THERMAL_DIR", "/sys/class/thermal"),
        cors_regex=_texto(valores, "JARVIS_CORS_REGEX", CORS_PADRAO),
        historico_msgs=_numero(valores, "JARVIS_HISTORICO_MSGS", 10, int),
        system_prompt=_texto(valores, "JARVIS_SYSTEM_PROMPT", PROMPT_PADRAO),
    )
