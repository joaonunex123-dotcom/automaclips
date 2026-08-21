"""Verificação de prontidão antes de ligar a publicação real."""
import pytest

import settings
from db import repositorio
from publish import preflight, quota


@pytest.fixture
def pronto(monkeypatch, tmp_path, conn):
    """Ambiente com tudo configurado — a linha de base dos testes."""
    token_yt = tmp_path / "youtube_token.json"
    token_yt.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "YOUTUBE_OAUTH_TOKEN", str(token_yt))
    monkeypatch.setattr(settings, "YOUTUBE_PRIVACIDADE", "public")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or-xxx")
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "123")
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "tok")
    monkeypatch.setattr(settings, "CLIPS_BASE_URL", "https://cdn/clips")
    monkeypatch.setattr(settings, "ARQUIVO_PARAR_PUBLICACAO",
                        str(tmp_path / "PARAR_PUBLICACAO"))
    return tmp_path


def test_tudo_configurado_nao_tem_problema(conn, pronto):
    assert preflight.verificar(conn) == []
    assert "Pronto para publicar" in preflight.formatar([])


def test_lista_todos_os_problemas_de_uma_vez(conn, monkeypatch, pronto):
    # Quem está ligando a publicação quer resolver tudo numa sentada, não
    # descobrir mais um item a cada execução.
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "")
    monkeypatch.setattr(settings, "CLIPS_BASE_URL", "")

    problemas = preflight.verificar(conn)
    mensagens = " ".join(p["mensagem"] for p in problemas)
    assert "OPENROUTER_API_KEY" in mensagens
    assert "INSTAGRAM_USER_ID" in mensagens
    assert "CLIPS_BASE_URL" in mensagens


def test_cada_problema_diz_como_resolver(conn, monkeypatch, pronto):
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "")
    bloqueios = preflight.bloqueios(preflight.verificar(conn))
    assert all(p["como_resolver"] for p in bloqueios)


# --- YouTube ------------------------------------------------------------------

def test_sem_oauth_bloqueia_o_youtube(conn, monkeypatch, pronto, tmp_path):
    monkeypatch.setattr(settings, "YOUTUBE_OAUTH_TOKEN",
                        str(tmp_path / "nao_existe.json"))
    problemas = preflight.verificar(conn, plataformas=["youtube"])
    assert "youtube" in preflight.plataformas_bloqueadas(problemas)
    assert "--autorizar" in problemas[0]["como_resolver"]


def test_privacidade_privada_e_aviso_nao_bloqueio(conn, monkeypatch, pronto):
    # O vídeo sobe; só não fica visível. Bloquear seria impedir o teste que a
    # privacidade 'private' existe justamente para permitir.
    monkeypatch.setattr(settings, "YOUTUBE_PRIVACIDADE", "private")
    problemas = preflight.verificar(conn, plataformas=["youtube"])
    assert preflight.bloqueios(problemas) == []
    assert any(p["nivel"] == preflight.AVISO for p in problemas)


def test_quota_esgotada_e_aviso_porque_vira_sozinha(conn, pronto):
    quota.registrar(conn, 10000)
    problemas = preflight.verificar(conn, plataformas=["youtube"])
    assert preflight.bloqueios(problemas) == []
    assert any("quota" in p["mensagem"] for p in problemas)


# --- Instagram ----------------------------------------------------------------

def test_token_do_banco_conta_como_configurado(conn, monkeypatch, pronto):
    # Depois da primeira renovação o .env fica vazio de propósito.
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "")
    repositorio.salvar_token(conn, "instagram", "renovado")
    problemas = preflight.verificar(conn, plataformas=["instagram"])
    assert preflight.bloqueios(problemas) == []


def test_sem_token_nenhum_bloqueia(conn, monkeypatch, pronto):
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "")
    problemas = preflight.verificar(conn, plataformas=["instagram"])
    assert "instagram" in preflight.plataformas_bloqueadas(problemas)


def test_base_url_vazia_bloqueia_o_instagram(conn, monkeypatch, pronto):
    monkeypatch.setattr(settings, "CLIPS_BASE_URL", "")
    problemas = preflight.verificar(conn, plataformas=["instagram"])
    assert "não aceita" in " ".join(p["mensagem"] for p in problemas)


def test_plataforma_fora_da_lista_nao_e_checada(conn, monkeypatch, pronto):
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "")
    assert preflight.verificar(conn, plataformas=["youtube"]) == []


# --- parada de emergência -----------------------------------------------------

def test_parada_de_emergencia_bloqueia_tudo(conn, pronto, monkeypatch):
    (pronto / "PARAR_PUBLICACAO").write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "PLATAFORMAS", ["youtube", "instagram"])

    problemas = preflight.verificar(conn)
    bloqueadas = preflight.plataformas_bloqueadas(problemas)
    assert {"youtube", "instagram"} <= bloqueadas


def test_arquivo_ausente_e_o_estado_normal(pronto):
    assert preflight.parada_de_emergencia_ativa(
        str(pronto / "PARAR_PUBLICACAO")
    ) is False


# --- relatório ----------------------------------------------------------------

def test_relatorio_separa_bloqueio_de_aviso(conn, monkeypatch, pronto):
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "")
    monkeypatch.setattr(settings, "YOUTUBE_PRIVACIDADE", "private")
    texto = preflight.formatar(preflight.verificar(conn))
    assert "IMPEDE A PUBLICAÇÃO" in texto
    assert "avisos" in texto
