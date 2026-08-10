"""Carga e validação do template_config.json."""
import json

import pytest

from editing import template as template_mod


def _escrever(tmp_path, dados):
    caminho = tmp_path / "template.json"
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    return str(caminho)


# --- o arquivo que acompanha o repositório ------------------------------------

def test_template_versionado_e_valido(template):
    # Se o template que vem no repo não passar na própria validação, a etapa 3
    # nasce quebrada e nenhum teste com config sintético perceberia.
    assert template["versao"]
    assert template["saida"]["altura"] > template["saida"]["largura"]  # vertical


def test_template_versionado_traz_as_secoes_do_spec(template):
    for secao in ("saida", "reframe", "zoom", "legenda", "hook", "watermark"):
        assert secao in template


def test_zoom_vem_desligado(template):
    # zoompan é conhecido por micro-tremor e não foi verificado num render
    # real; entregar ligado seria embutir um defeito não medido no padrão.
    assert template["zoom"]["ativo"] is False


# --- carga --------------------------------------------------------------------

def test_chaves_de_comentario_sao_ignoradas(tmp_path):
    caminho = _escrever(tmp_path, {
        "_comentario": "isto some", "versao": "9",
        "legenda": {"_nota": "some também", "tamanho": 50},
    })
    config = template_mod.carregar(caminho)
    assert "_comentario" not in config
    assert "_nota" not in config["legenda"]
    assert config["legenda"]["tamanho"] == 50


def test_chaves_ausentes_caem_no_padrao(tmp_path):
    config = template_mod.carregar(_escrever(tmp_path, {"versao": "2"}))
    assert config["saida"]["largura"] == template_mod.PADRAO["saida"]["largura"]
    assert config["legenda"]["fonte"] == "Arial"


def test_mescla_e_profunda(tmp_path):
    # Informar uma chave de legenda não pode apagar as outras.
    config = template_mod.carregar(
        _escrever(tmp_path, {"legenda": {"tamanho": 120}})
    )
    assert config["legenda"]["tamanho"] == 120
    assert config["legenda"]["cor_destaque"] == "#FFD400"


def test_arquivo_ausente_explica_o_papel_dele(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="visual do clip"):
        template_mod.carregar(str(tmp_path / "nao_existe.json"))


def test_json_invalido(tmp_path):
    caminho = tmp_path / "template.json"
    caminho.write_text("{quebrado", encoding="utf-8")
    with pytest.raises(template_mod.TemplateInvalido, match="JSON válido"):
        template_mod.carregar(str(caminho))


def test_json_que_nao_e_objeto(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="objeto JSON"):
        template_mod.carregar(_escrever(tmp_path, ["lista"]))


# --- validação ----------------------------------------------------------------

def test_modo_de_reframe_desconhecido(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="reframe.modo"):
        template_mod.carregar(_escrever(tmp_path, {"reframe": {"modo": "girar"}}))


def test_tamanho_como_string_e_recusado(tmp_path):
    # Erro que só apareceria depois de baixar, transcrever e chamar o LLM.
    with pytest.raises(template_mod.TemplateInvalido, match="precisa ser número"):
        template_mod.carregar(_escrever(tmp_path, {"legenda": {"tamanho": "76"}}))


def test_booleano_nao_passa_por_numero(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="precisa ser número"):
        template_mod.carregar(_escrever(tmp_path, {"saida": {"crf": True}}))


@pytest.mark.parametrize("alinhamento", [0, 10, -1])
def test_alinhamento_fora_do_teclado_numerico(tmp_path, alinhamento):
    with pytest.raises(template_mod.TemplateInvalido, match="alinhamento"):
        template_mod.carregar(
            _escrever(tmp_path, {"legenda": {"alinhamento": alinhamento}})
        )


def test_ancora_fora_de_0_1(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="ancora_horizontal"):
        template_mod.carregar(
            _escrever(tmp_path, {"reframe": {"ancora_horizontal": 1.5}})
        )


def test_crf_fora_da_faixa(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="crf"):
        template_mod.carregar(_escrever(tmp_path, {"saida": {"crf": 99}}))


def test_cor_invalida(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="cor"):
        template_mod.carregar(_escrever(tmp_path, {"legenda": {"cor": "vermelho"}}))


def test_watermark_ligada_sem_texto(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="texto está vazio"):
        template_mod.carregar(
            _escrever(tmp_path, {"watermark": {"ativo": True, "texto": "  "}})
        )


def test_sfx_vem_desligado(template):
    # Depende de arquivos de áudio que não vêm no repositório.
    assert template["sfx"]["ativo"] is False


def test_sfx_declara_os_tres_gatilhos_do_spec(template):
    gatilhos = {e["gatilho"] for e in template["sfx"]["eventos"].values()}
    assert gatilhos == {"transicao", "pico", "palavra_chave"}


def test_gatilho_desconhecido_e_recusado(tmp_path):
    # Um som com gatilho inexistente nunca tocaria, em silêncio — o modo de
    # falha invisível que só apareceria ao assistir o clip.
    with pytest.raises(template_mod.TemplateInvalido, match="gatilho"):
        template_mod.carregar(_escrever(tmp_path, {
            "sfx": {"eventos": {"x": {"ativo": True, "arquivo": "x.wav",
                                      "gatilho": "quando_der"}}}
        }))


def test_evento_ativo_sem_arquivo(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="sem arquivo"):
        template_mod.carregar(_escrever(tmp_path, {
            "sfx": {"eventos": {"x": {"ativo": True, "arquivo": "",
                                      "gatilho": "pico"}}}
        }))


def test_sfx_ligado_sem_nenhum_evento_ativo(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="nada tocaria"):
        template_mod.carregar(_escrever(tmp_path, {
            "sfx": {"ativo": True,
                    "eventos": {"x": {"ativo": False, "arquivo": "x.wav",
                                      "gatilho": "pico"}}}
        }))


def test_volume_negativo(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="sfx.volume"):
        template_mod.carregar(_escrever(tmp_path, {"sfx": {"volume": -1}}))


def test_palavras_chave_precisa_ser_lista(tmp_path):
    with pytest.raises(template_mod.TemplateInvalido, match="palavras_chave"):
        template_mod.carregar(
            _escrever(tmp_path, {"sfx": {"palavras_chave": "caramba"}})
        )


# --- conversão de cor ---------------------------------------------------------

def test_cor_ass_inverte_os_canais():
    # ASS usa BGR. Escrever #FF0000 direto no arquivo pintaria AZUL.
    assert template_mod.cor_ass("#FF0000") == "&H000000FF"
    assert template_mod.cor_ass("#0000FF") == "&H00FF0000"
    assert template_mod.cor_ass("#FFD400") == "&H0000D4FF"


def test_cor_ass_inverte_o_alfa():
    # No JSON o alfa é o intuitivo (FF = opaco); no ASS 00 é que é opaco.
    assert template_mod.cor_ass("#FFFFFFFF") == "&H00FFFFFF"
    assert template_mod.cor_ass("#FFFFFF00") == "&HFFFFFFFF"
    assert template_mod.cor_ass("#FFFFFFB0") == "&H4FFFFFFF"


def test_cor_ass_sem_alfa_e_opaca():
    assert template_mod.cor_ass("#FFFFFF") == "&H00FFFFFF"


def test_cor_ass_aceita_sem_cerquilha():
    assert template_mod.cor_ass("FFFFFF") == "&H00FFFFFF"


@pytest.mark.parametrize("ruim", ["#FFF", "#GGGGGG", "", 123, None])
def test_cor_ass_recusa_lixo(ruim):
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.cor_ass(ruim)
