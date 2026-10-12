"""Bancada local do modo multi (2+ pessoas no quadro). NÃO publica nada.

    python -m tools.multi_local --fonte caminho.mp4 [--inicio 64 --duracao 28]
                                [--saida teste_local/multi]

Renderiza, pelo CAMINHO REAL do pipeline (editar.renderizar_clip), com o
detector Haar real do OpenCV:

    01_antes_crop_no_vao.mp4          multi desligado (como era)
    02_multi_contain_desfoque.mp4     multi ligado, fundo desfocado
    03_multi_contain_solido.mp4       multi ligado, fundo sólido
    04_multi_com_efeitos.mp4          multi + efeitos visuais e sonoros

A fonte é lida do caminho original (não é copiada). Sem LLM, sem rede: o hook
e a legenda são fixos. Grava frames/, relatorio.json e comparativo.mp4.
"""
import argparse
import json
import logging
import os
import subprocess
import sys

log = logging.getLogger("multi_local")

HOOK = "dois apresentadores"

VARIANTES = (
    ("01_antes_crop_no_vao", "multi desligado: crop 9:16 fixo no centro", {}),
    ("02_multi_contain_desfoque", "multi ligado, fundo desfocado",
     {"multi": {"ativo": True, "fundo": "desfoque"}}),
    ("03_multi_contain_solido", "multi ligado, fundo sólido",
     {"multi": {"ativo": True, "fundo": "solido"}}),
    ("04_multi_com_efeitos", "multi + efeitos visuais e sonoros (cadência 4 s)",
     {"multi": {"ativo": True, "fundo": "desfoque"},
      "sfx": {"ativo": False},
      "efeitos": {n: {"ativo": True} for n in (
          "shutter", "iris", "whip_pan", "glitch", "flash", "zoom", "voz",
          "ducking", "limiter", "pad", "sfx_visuais")}}),
)


def _mesclar(base, mods):
    for chave, valor in mods.items():
        if isinstance(valor, dict) and isinstance(base.get(chave), dict):
            _mesclar(base[chave], valor)
        else:
            base[chave] = valor
    return base


def _transcricao(duracao_s, inicio_s=0.0):
    """Legenda fixa, deslocada para o tempo absoluto da fonte (inicio_s)."""
    from tools.comparativo_local import palavras_fixas
    palavras = [{**p, "inicio": p["inicio"] + inicio_s, "fim": p["fim"] + inicio_s}
                for p in palavras_fixas(duracao_s)]
    segmentos = []
    for i in range(0, len(palavras), 6):
        g = palavras[i:i + 6]
        segmentos.append({"inicio": g[0]["inicio"], "fim": g[-1]["fim"],
                          "texto": " ".join(p["palavra"] for p in g),
                          "palavras": g})
    return {"idioma": "pt", "duracao_s": duracao_s, "segmentos": segmentos}


def renderizar(fonte, inicio, duracao, saida):
    import copy
    from db import repositorio
    from editing import editar, gerar_sfx, template as template_mod
    from tools.comparativo_local import picos_locais

    saida = os.path.abspath(saida)      # o ffmpeg roda com cwd no render_dir
    pasta_sfx = os.path.join(saida, "sfx")
    gerar_sfx.gerar(pasta_sfx)
    gerar_sfx.gerar_pads(pasta_sfx)
    render_dir = os.path.join(saida, "render")
    os.makedirs(os.path.join(saida, "tmp"), exist_ok=True)
    os.makedirs(render_dir, exist_ok=True)

    base = template_mod.carregar()
    base["legenda"]["tamanho"] = 92                      # legenda grande
    picos = picos_locais(fonte, inicio, duracao, saida) or []
    transcricao = _transcricao(duracao, inicio)

    bd = os.path.join(saida, "tmp", "multi.db")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(bd + ext):
            os.remove(bd + ext)
    conn = repositorio.conectar(bd)
    relatorio = {}
    try:
        for idx, (nome, descricao, mods) in enumerate(VARIANTES, start=1):
            vid = f"m{idx:02d}"
            fila_id = repositorio.registrar_observacao(
                conn, {"plataforma": "youtube", "video_id": vid,
                       "canal_id": "UC_local", "canal_nome": "Local",
                       "titulo": "Teste multi", "url": f"https://x.invalid/{vid}",
                       "publicado_em": "2026-10-01T00:00:00Z",
                       "duracao_s": int(duracao), "views": 0},
                views=0, ganho=0, score=1.0, status=repositorio.STATUS_ANALISADO)
            repositorio.registrar_midia(
                conn, fila_id, video_path=fonte, audio_path=fonte,
                transcricao_path=os.path.join(saida, "tmp", "t.json"),
                duracao_real_s=inicio + duracao + 5)
            repositorio.registrar_clips(conn, fila_id, [{
                "inicio_s": inicio, "fim_s": inicio + duracao,
                "score_claude": 9.0, "motivo": descricao, "hook_text": HOOK,
                "picos_energia": len(picos), "score_final": 9.0,
                "picos_instantes": list(picos)}])
            linha = conn.execute(
                "SELECT c.*, f.video_id, f.titulo, f.canal_nome, m.video_path,"
                " m.transcricao_path FROM clips c"
                " JOIN fila_clips f ON f.id = c.fila_clip_id"
                " LEFT JOIN midia m ON m.fila_clip_id = c.fila_clip_id"
                " WHERE f.video_id = ?", (vid,)).fetchone()
            config = _mesclar(copy.deepcopy(base), mods)
            config["sfx"]["diretorio"] = pasta_sfx
            config = template_mod.validar(config)
            caminho = editar.renderizar_clip(
                conn, linha, config, carregar_transcricao=lambda c: transcricao,
                destino_dir=render_dir)
            destino = os.path.join(saida, f"{nome}.mp4")
            os.replace(caminho, destino)
            depur = caminho[:-4] + "_multi.json"
            relatorio[nome] = {
                "descricao": descricao, "caminho": os.path.abspath(destino),
                "template_versao": config["versao"],
                "multi_ativo": config["multi"]["ativo"],
                "fundo": config["multi"]["fundo"] if config["multi"]["ativo"]
                else None,
                "depuracao": depur if os.path.exists(depur) else None,
            }
            log.info("%s ok", nome)
    finally:
        conn.close()
    return relatorio


def instantes(relatorio):
    """(single, multi, pos_transicao) a partir da depuração do vídeo 02."""
    dep = relatorio["02_multi_contain_desfoque"]["depuracao"]
    dados = json.load(open(dep, encoding="utf-8"))
    usados = dados["segmentos_usados"]
    if not usados:
        return None, dados
    p = usados[0]
    single = max(0.5, p["inicio_s"] / 2.0)
    multi = p["inicio_s"] + (p["fim_s"] - p["inicio_s"]) / 2.0
    pos = p["inicio_s"] + 0.4
    return {"single": round(single, 2), "multi": round(multi, 2),
            "pos_transicao": round(pos, 2)}, dados


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonte", required=True)
    ap.add_argument("--inicio", type=float, default=64.0)
    ap.add_argument("--duracao", type=float, default=28.0)
    ap.add_argument("--saida", default="teste_local/multi")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from tools.comparativo_local import frame_em, grade, lufs, metadados

    os.makedirs(a.saida, exist_ok=True)
    rel = renderizar(a.fonte, a.inicio, a.duracao, a.saida)
    marcas, depur = instantes(rel)
    pasta = os.path.join(a.saida, "frames")
    os.makedirs(pasta, exist_ok=True)
    for nome, info in rel.items():
        info.update(metadados(info["caminho"]))
        info["lufs"], info["pico_dbfs"] = lufs(info["caminho"])
        info["frames"] = {}
        for rotulo, t in (marcas or {}).items():
            destino = os.path.join(pasta, f"{nome}__{rotulo}_{t:.1f}s.png")
            frame_em(info["caminho"], t, destino)
            info["frames"][rotulo] = os.path.abspath(destino)
    comparativo = os.path.join(a.saida, "comparativo.mp4")
    grade([rel[n]["caminho"] for n, _, _ in VARIANTES], comparativo)
    resumo = {"fonte": a.fonte, "inicio_s": a.inicio, "duracao_s": a.duracao,
              "instantes_frames": marcas, "comparativo": os.path.abspath(comparativo),
              "trocas_de_modo": depur.get("trocas_de_modo"),
              "segmentos": depur.get("segmentos"),
              "amostras_com_rosto": sum(1 for x in depur.get("amostras", [])
                                        if x["n"] > 0),
              "amostras_total": len(depur.get("amostras", [])),
              "max_rostos": max((x["n"] for x in depur.get("amostras", [])),
                                default=0),
              "videos": rel}
    with open(os.path.join(a.saida, "relatorio.json"), "w",
              encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in resumo.items() if k != "videos"},
                     ensure_ascii=False, indent=1))
    for nome, info in rel.items():
        print(nome, info["resolucao"], info["duracao_s"], info["lufs"], info["audio"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
