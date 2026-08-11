"""Upload para o YouTube — montagem do body e credenciais."""
import pytest

import settings
from publish import youtube as yt


class ClienteUploadFalso:
    def __init__(self, resposta=None, erro=None):
        self._resposta = resposta if resposta is not None else {"id": "abc123"}
        self._erro = erro
        self.chamadas = []

    def videos(self):
        return self

    def insert(self, part=None, body=None, media_body=None):
        self.chamadas.append({"part": part, "body": body, "media": media_body})
        return self

    def execute(self):
        if self._erro:
            raise self._erro
        return self._resposta


@pytest.fixture
def arquivo(tmp_path):
    caminho = tmp_path / "vid1_7.mp4"
    caminho.write_text("video", encoding="utf-8")
    return str(caminho)


# --- body ---------------------------------------------------------------------

def test_body_leva_snippet_e_status():
    corpo = yt.corpo_do_upload("Título", "Descrição", ["a", "b"], "22", "private")
    assert corpo["snippet"]["title"] == "Título"
    assert corpo["snippet"]["tags"] == ["a", "b"]
    assert corpo["snippet"]["categoryId"] == "22"
    assert corpo["status"]["privacyStatus"] == "private"


def test_declaracao_de_conteudo_infantil_e_obrigatoria():
    # Sem ela a API recusa o insert.
    corpo = yt.corpo_do_upload("t", "d")
    assert corpo["status"]["selfDeclaredMadeForKids"] is False


def test_body_cai_nos_padroes_do_settings():
    corpo = yt.corpo_do_upload("t", "d")
    assert corpo["snippet"]["categoryId"] == str(settings.YOUTUBE_CATEGORIA)
    assert corpo["status"]["privacyStatus"] == settings.YOUTUBE_PRIVACIDADE


def test_escopo_e_o_minimo_para_subir():
    # Pedir mais do que se usa é pedir uma permissão que o programa não precisa.
    assert yt.ESCOPOS == ["https://www.googleapis.com/auth/youtube.upload"]


# --- envio --------------------------------------------------------------------

def test_envia_e_devolve_id_e_url(arquivo):
    cliente = ClienteUploadFalso({"id": "abc123"})
    video_id, url = yt.enviar(
        cliente, arquivo, "Título", "Descrição", tags=["x"],
        criar_midia=lambda caminho: f"midia:{caminho}",
    )
    assert video_id == "abc123"
    assert url == "https://www.youtube.com/watch?v=abc123"
    assert cliente.chamadas[0]["part"] == "snippet,status"
    assert cliente.chamadas[0]["media"] == f"midia:{arquivo}"


def test_arquivo_ausente_falha_antes_da_api(tmp_path):
    cliente = ClienteUploadFalso()
    with pytest.raises(yt.ErroYouTubeUpload, match="não encontrado"):
        yt.enviar(cliente, str(tmp_path / "sumiu.mp4"), "t", "d",
                  criar_midia=lambda c: c)
    assert cliente.chamadas == []


def test_resposta_sem_id(arquivo):
    cliente = ClienteUploadFalso({})
    with pytest.raises(yt.ErroYouTubeUpload, match="sem id"):
        yt.enviar(cliente, arquivo, "t", "d", criar_midia=lambda c: c)


def test_erro_da_api_vira_erro_do_modulo(arquivo):
    cliente = ClienteUploadFalso(erro=RuntimeError("quota exceeded"))
    with pytest.raises(yt.ErroYouTubeUpload, match="quota exceeded"):
        yt.enviar(cliente, arquivo, "t", "d", criar_midia=lambda c: c)


# --- credenciais --------------------------------------------------------------

def test_sem_token_diz_qual_comando_rodar(tmp_path):
    # O fluxo OAuth é interativo e não pode rodar numa execução agendada às 3
    # da manhã; a publicação só consome o token já salvo.
    with pytest.raises(yt.ErroYouTubeUpload, match="--autorizar"):
        yt.carregar_credenciais(str(tmp_path / "nao_existe.json"))


def test_sem_token_pode_devolver_none(tmp_path):
    assert yt.carregar_credenciais(
        str(tmp_path / "nao_existe.json"), erro_se_ausente=False
    ) is None


def test_autorizar_sem_client_secrets_explica_onde_pegar(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_CLIENT_SECRETS",
                        str(tmp_path / "nao_existe.json"))
    with pytest.raises(yt.ErroYouTubeUpload, match="Google Cloud"):
        yt.autorizar()


def test_custo_de_upload_bate_com_o_settings():
    assert yt.CUSTO_UPLOAD == settings.YOUTUBE_CUSTO_UPLOAD
