import pytest

from backend.config import ErroConfiguracao, carregar_config, ler_arquivo_env

from tests.constantes import TOKEN


def test_defaults_com_token_valido():
    cfg = carregar_config({"JARVIS_TOKEN": TOKEN})
    assert cfg.llm == "mock"
    assert cfg.tz == "America/Sao_Paulo"
    assert cfg.llama_url == "http://127.0.0.1:8080"
    assert cfg.historico_msgs == 10


def test_token_obrigatorio_e_minimo():
    with pytest.raises(ErroConfiguracao, match="JARVIS_TOKEN"):
        carregar_config({})
    with pytest.raises(ErroConfiguracao, match="JARVIS_TOKEN"):
        carregar_config({"JARVIS_TOKEN": "curto"})


def test_llm_invalido_recusado():
    with pytest.raises(ErroConfiguracao, match="JARVIS_LLM"):
        carregar_config({"JARVIS_TOKEN": TOKEN, "JARVIS_LLM": "gpt"})


def test_llamacpp_aceito_e_url_sem_barra_final():
    cfg = carregar_config({"JARVIS_TOKEN": TOKEN, "JARVIS_LLM": "llamacpp",
                           "LLAMA_URL": "http://10.0.0.5:8080/"})
    assert cfg.llm == "llamacpp"
    assert cfg.llama_url == "http://10.0.0.5:8080"


def test_url_sem_esquema_recusada():
    with pytest.raises(ErroConfiguracao, match="LLAMA_URL"):
        carregar_config({"JARVIS_TOKEN": TOKEN, "LLAMA_URL": "10.0.0.5:8080"})


def test_fuso_desconhecido_recusado():
    with pytest.raises(ErroConfiguracao, match="JARVIS_TZ"):
        carregar_config({"JARVIS_TOKEN": TOKEN, "JARVIS_TZ": "Marte/Olympus"})


def test_numero_invalido_recusado():
    with pytest.raises(ErroConfiguracao, match="LLAMA_TIMEOUT_S"):
        carregar_config({"JARVIS_TOKEN": TOKEN, "LLAMA_TIMEOUT_S": "rapido"})


def test_ler_arquivo_env_aceita_comentario_aspas_e_linhas_vazias(tmp_path):
    arquivo = tmp_path / ".env"
    arquivo.write_text(
        "# comentário\n\nJARVIS_TOKEN=\"abc def\"\nLLAMA_URL='http://x:1'\nSEM_IGUAL\n",
        encoding="utf-8",
    )
    valores = ler_arquivo_env(arquivo)
    assert valores == {"JARVIS_TOKEN": "abc def", "LLAMA_URL": "http://x:1"}


def test_arquivo_inexistente_vira_vazio(tmp_path):
    assert ler_arquivo_env(tmp_path / "nao_existe") == {}


def test_env_do_arquivo_e_vencido_pelo_ambiente(tmp_path, monkeypatch):
    arquivo = tmp_path / ".env"
    arquivo.write_text(f"JARVIS_TOKEN={TOKEN}\nJARVIS_LLM=mock\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_LLM", "llamacpp")
    cfg = carregar_config(arquivo_env=arquivo)
    assert cfg.token == TOKEN
    assert cfg.llm == "llamacpp"
