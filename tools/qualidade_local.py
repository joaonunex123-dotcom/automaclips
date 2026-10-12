"""Bancada local da validação de qualidade (fases 1 a 4). NÃO publica nada.

    python -m tools.qualidade_local [--saida teste_local/qualidade]
                                    [--fonte caminho.mp4] [--inicio 75 --duracao 30]

Gera, a partir de um trecho local, os vídeos de comparação:

    01_base.mp4                       nada novo
    02_capa_titulo.mp4 + capa_*.png   quadro de título nos primeiros 0,3 s
    03_grafico_desfoque.mp4           modo gráfico, fundo desfocado
    04_grafico_solido.mp4             modo gráfico, fundo sólido
    05_efeitos_visuais.mp4            shutter, flash, glitch, whip pan
    06_efeitos_visuais_sonoros.mp4    tudo ligado (efeitos + SFX + mix)

mais um frame de cada um em frames/, a medição de LUFS e o comparativo.mp4.

Nenhuma chamada paga: o texto do hook é fixo e o trecho é local. A fonte é o
material interno já usado nas bancadas anteriores; como não tem gráfico, o
script insere um gráfico sintético (1920x1080) no meio, para a fase 2.
"""
import argparse
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time

log = logging.getLogger("qualidade_local")

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = "ele nunca contou isso"


# --- fonte sintética ------------------------------------------------------------

def desenhar_grafico(caminho_png, largura=1920, altura=1080):
    """Gráfico de barras com grade, eixos e rótulos (sem ruído de câmera)."""
    import cv2
    import numpy as np

    img = np.full((altura, largura, 3), 252, np.uint8)
    fonte = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(img, "Alunos aprovados por regiao (%)", (120, 110), fonte, 2.2,
                (30, 30, 30), 5, cv2.LINE_AA)
    x0, y0, x1, y1 = 180, 200, 1780, 940
    for i in range(6):
        y = y1 - (y1 - y0) * i // 5
        cv2.line(img, (x0, y), (x1, y), (205, 205, 205), 2)
        cv2.putText(img, str(i * 20), (70, y + 12), fonte, 1.1, (60, 60, 60), 2,
                    cv2.LINE_AA)
    valores = [62, 95, 48, 81, 70, 36]
    cores = [(200, 120, 40), (60, 160, 230), (80, 180, 90), (60, 60, 200),
             (160, 90, 160), (120, 120, 120)]
    nomes = ["Norte", "Sul", "Leste", "Oeste", "Centro", "Ilhas"]
    largura_barra = 170
    for i, v in enumerate(valores):
        bx = x0 + 60 + i * (largura_barra + 90)
        by = y1 - int((y1 - y0) * v / 100)
        cv2.rectangle(img, (bx, by), (bx + largura_barra, y1), cores[i], -1)
        cv2.putText(img, str(v), (bx + 40, by - 14), fonte, 1.3, (30, 30, 30), 3,
                    cv2.LINE_AA)
        cv2.putText(img, nomes[i], (bx + 10, y1 + 55), fonte, 1.1, (50, 50, 50),
                    2, cv2.LINE_AA)
    cv2.line(img, (x0, y0), (x0, y1), (40, 40, 40), 4)
    cv2.line(img, (x0, y1), (x1, y1), (40, 40, 40), 4)
    cv2.imwrite(caminho_png, img)
    return caminho_png


def gerar_fonte(fonte, saida_mp4, inicio_s, duracao_s, ffmpeg, pasta_tmp):
    """30 s: [0-10) real, [10-20) gráfico 1920x1080, [20-30) real. Áudio contínuo."""
    png = desenhar_grafico(os.path.join(pasta_tmp, "grafico.png"))
    terco = duracao_s / 3.0
    filtro = (
        f"[0:v]fps=30,scale=1920:1080:force_original_aspect_ratio=increase,"
        f"crop=1920:1080,setsar=1,format=yuv420p,split=2[a][b];"
        f"[a]trim=0:{terco:.3f},setpts=PTS-STARTPTS[v0];"
        f"[b]trim={2 * terco:.3f}:{duracao_s:.3f},setpts=PTS-STARTPTS[v2];"
        f"[1:v]fps=30,format=yuv420p,setsar=1,trim=0:{terco:.3f},"
        "setpts=PTS-STARTPTS[v1];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[v]"
    )
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{inicio_s}", "-t", f"{duracao_s}", "-i", fonte,
           "-loop", "1", "-t", f"{terco:.3f}", "-i", png,
           "-filter_complex", filtro, "-map", "[v]", "-map", "0:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "aac", "-b:a", "160k", saida_mp4]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:])
    return saida_mp4


# --- transcrição e picos -----------------------------------------------------------

def transcricao_fixa(duracao_s):
    """Transcrição no formato do .json do pipeline, com a legenda fixa."""
    from tools.comparativo_local import palavras_fixas
    palavras = palavras_fixas(duracao_s)
    segmentos = []
    for i in range(0, len(palavras), 6):
        grupo = palavras[i:i + 6]
        segmentos.append({
            "inicio": grupo[0]["inicio"], "fim": grupo[-1]["fim"],
            "texto": " ".join(p["palavra"] for p in grupo),
            "palavras": grupo,
        })
    return {"idioma": "pt", "duracao_s": duracao_s, "segmentos": segmentos}


# --- as seis variantes ---------------------------------------------------------------

VARIANTES = (
    ("01_base", "nada novo", {}),
    ("02_capa_titulo", "quadro de título nos primeiros 0,3 s", {
        "capa": {"ativa": True, "frame_inicial": {"ativo": True}}}),
    ("03_grafico_desfoque", "modo gráfico, fundo desfocado", {
        "grafico": {"ativo": True, "fundo": "desfoque"}}),
    ("04_grafico_solido", "modo gráfico, fundo sólido", {
        "grafico": {"ativo": True, "fundo": "solido"}}),
    ("05_efeitos_visuais", "shutter, flash, glitch, whip pan", {
        "efeitos": {n: {"ativo": True}
                    for n in ("shutter", "flash", "glitch", "whip_pan")}}),
    ("06_efeitos_visuais_sonoros", "tudo ligado: capa, gráfico, 6 efeitos, "
     "SFX, voz -16, ducking, limiter, pad", {
         "capa": {"ativa": True, "frame_inicial": {"ativo": True}},
         "grafico": {"ativo": True, "fundo": "desfoque"},
         "efeitos": {
             **{n: {"ativo": True} for n in (
                 "shutter", "iris", "whip_pan", "glitch", "flash", "zoom",
                 "voz", "ducking", "limiter", "pad", "sfx_visuais")}}}),
)


def _mesclar(base, mods):
    for chave, valor in mods.items():
        if isinstance(valor, dict) and isinstance(base.get(chave), dict):
            _mesclar(base[chave], valor)
        else:
            base[chave] = valor
    return base


def config_da_variante(base, mods, pasta_sfx):
    from editing import template as template_mod
    config = _mesclar(copy.deepcopy(base), mods)
    config["sfx"]["diretorio"] = pasta_sfx
    # Efeito de zoom por picos precisa dos picos; sem eles cai em intervalo.
    return template_mod.validar(config)


def _linha(conn, clip_id):
    from db import repositorio
    return next(c for c in repositorio.clips_para_renderizar(conn)
                if c["id"] == clip_id)


def renderizar_variantes(fonte, saida, duracao, ffmpeg, repeticoes=1):
    """Renderiza as seis pelo CAMINHO REAL do pipeline (editar.renderizar_clip)."""
    import settings
    from db import repositorio
    from editing import editar, gerar_sfx, template as template_mod
    from tools.comparativo_local import picos_locais

    pasta_sfx = os.path.join(saida, "sfx")
    gerar_sfx.gerar(pasta_sfx)
    gerar_sfx.gerar_pads(pasta_sfx)
    render_dir = os.path.join(saida, "render")
    os.makedirs(render_dir, exist_ok=True)

    base = template_mod.carregar()
    base["legenda"]["tamanho"] = 92
    picos = picos_locais(fonte, 0.0, duracao, saida) or []
    log.info("picos de energia: %d", len(picos))
    transcricao = transcricao_fixa(duracao)

    bd = os.path.join(saida, "tmp", "qualidade.db")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(bd + ext):
            os.remove(bd + ext)
    conn = repositorio.conectar(bd)
    relatorio = {}
    try:
        for idx, (nome, descricao, mods) in enumerate(VARIANTES, start=1):
            video_id = f"q{idx:02d}"
            fila_id = repositorio.registrar_observacao(
                conn, {"plataforma": "youtube", "video_id": video_id,
                       "canal_id": "UC_local", "canal_nome": "Local",
                       "titulo": "Teste de qualidade",
                       "url": f"https://example.invalid/{video_id}",
                       "publicado_em": "2026-10-01T00:00:00Z",
                       "duracao_s": int(duracao), "views": 0},
                views=0, ganho=0, score=1.0,
                status=repositorio.STATUS_ANALISADO)
            repositorio.registrar_midia(
                conn, fila_id, video_path=fonte, audio_path=fonte,
                transcricao_path=os.path.join(saida, "tmp", "transcricao.json"),
                duracao_real_s=duracao)
            repositorio.registrar_clips(conn, fila_id, [{
                "inicio_s": 0.0, "fim_s": duracao, "score_claude": 9.0,
                "motivo": descricao, "hook_text": HOOK, "picos_energia": len(picos),
                "score_final": 9.0, "picos_instantes": picos}])
            clip_id = repositorio.clips_do_video(conn, fila_id)[0]["id"]
            config = config_da_variante(base, mods, pasta_sfx)
            tempos = []
            caminho = None
            for _ in range(repeticoes):
                t0 = time.perf_counter()
                caminho = editar.renderizar_clip(
                    conn, _linha(conn, clip_id) if _ == 0 else linha, config,
                    carregar_transcricao=lambda c: transcricao,
                    destino_dir=render_dir)
                tempos.append(time.perf_counter() - t0)
                linha = _linha_de(conn, clip_id)
            destino = os.path.join(saida, f"{nome}.mp4")
            shutil.copyfile(caminho, destino)
            relatorio[nome] = {
                "descricao": descricao, "caminho": destino,
                "template_versao": config["versao"],
                "tempo_render_s": round(min(tempos), 1),
                "tempos_s": [round(t, 1) for t in tempos],
                "efeitos": _efeitos_ligados(config),
                "render_dir": render_dir,
            }
            log.info("%s  %.1fs", nome, min(tempos))
    finally:
        conn.close()
    return relatorio


def _linha_de(conn, clip_id):
    return conn.execute(
        "SELECT c.*, f.video_id, f.titulo, f.canal_nome, m.video_path,"
        "       m.transcricao_path FROM clips c"
        " JOIN fila_clips f ON f.id = c.fila_clip_id"
        " LEFT JOIN midia m ON m.fila_clip_id = c.fila_clip_id"
        " WHERE c.id = ?", (clip_id,)).fetchone()


def _efeitos_ligados(config):
    ligados = []
    if config["capa"]["ativa"]:
        ligados.append("capa")
    if config["capa"]["frame_inicial"]["ativo"]:
        ligados.append("capa.frame_inicial")
    if config["grafico"]["ativo"]:
        ligados.append(f"grafico({config['grafico']['fundo']})")
    for nome, secao in config["efeitos"].items():
        if isinstance(secao, dict) and secao.get("ativo") is True:
            ligados.append(f"efeitos.{nome}")
    return ligados


# --- medição e comparativo -----------------------------------------------------------

def frame_de_cada(relatorio, saida):
    """1 frame por vídeo: no instante em que o recurso aparece."""
    from tools.comparativo_local import frame_em
    instantes = {"01_base": 15.0, "02_capa_titulo": 0.15,
                 "03_grafico_desfoque": 15.0, "04_grafico_solido": 15.0,
                 "05_efeitos_visuais": 7.0, "06_efeitos_visuais_sonoros": 10.0}
    pasta = os.path.join(saida, "frames")
    os.makedirs(pasta, exist_ok=True)
    for nome, info in relatorio.items():
        t = instantes.get(nome, 15.0)
        destino = os.path.join(pasta, f"{nome}.png")
        frame_em(info["caminho"], t, destino)
        info["frame"] = destino
        info["frame_t_s"] = t


def medir(relatorio):
    from tools.comparativo_local import lufs, metadados
    for info in relatorio.values():
        info.update(metadados(info["caminho"]))
        info["lufs"], info["pico_dbfs"] = lufs(info["caminho"])


# --- teste da limpeza (diretório temporário, ffmpeg real) ----------------------------

def testar_limpeza(saida, ffmpeg):
    """Falso podcast com clips de verdade; simular, real, e caminho recusado."""
    import tempfile
    import settings
    from db import repositorio
    from pipeline import limpeza

    resultado = {"passos": [], "ok": True}

    def passo(nome, ok, detalhe=""):
        resultado["passos"].append({"passo": nome, "ok": bool(ok),
                                    "detalhe": detalhe})
        resultado["ok"] = resultado["ok"] and bool(ok)
        log.info("  [%s] %s %s", "ok" if ok else "FALHOU", nome, detalhe)

    with tempfile.TemporaryDirectory(prefix="limpeza_") as raiz:
        downloads = os.path.join(raiz, "downloads")
        render = os.path.join(raiz, "render")
        fora = os.path.join(raiz, "documentos_do_joao")
        for d in (downloads, render, fora, os.path.join(downloads, "transcricoes")):
            os.makedirs(d)
        vid = "fakePod0001"

        def clip(caminho, w=1080, h=1920):
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"testsrc=s={w}x{h}:r=30:d=2", "-f", "lavfi", "-i",
                 "sine=f=440:d=2", "-shortest", "-c:v", "libx264", "-preset",
                 "ultrafast", "-c:a", "aac", caminho], check=True)

        fonte = os.path.join(downloads, f"{vid}.mp4")
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc=s=1920x1080:r=30:d=4", "-f", "lavfi", "-i",
             "sine=f=300:d=4", "-shortest", "-c:v", "libx264", "-c:a", "aac",
             fonte], check=True)
        audio = os.path.join(downloads, f"{vid}.wav")
        open(audio, "wb").write(os.urandom(200_000))
        parte = os.path.join(downloads, f"{vid}.parte00.mp3")
        open(parte, "wb").write(os.urandom(50_000))
        transcricao = os.path.join(downloads, "transcricoes", f"{vid}.json")
        open(transcricao, "w").write("{}")
        outro = os.path.join(downloads, "OUTRO_PODCAST.mp4")
        open(outro, "wb").write(os.urandom(10_000))
        precioso = os.path.join(fora, "tese_final.mp4")
        open(precioso, "wb").write(b"nao apague")

        bd = os.path.join(raiz, "clips.db")
        conn = repositorio.conectar(bd)
        try:
            fila_id = repositorio.registrar_observacao(
                conn, {"plataforma": "youtube", "video_id": vid,
                       "canal_id": "UC", "canal_nome": "Falso",
                       "titulo": "Podcast falso", "url": "https://x.invalid/a",
                       "publicado_em": "2026-10-01T00:00:00Z",
                       "duracao_s": 4, "views": 0},
                views=0, ganho=0, score=1.0,
                status=repositorio.STATUS_ANALISADO)
            repositorio.registrar_midia(
                conn, fila_id, video_path=fonte, audio_path=audio,
                transcricao_path=transcricao, duracao_real_s=4.0)
            repositorio.registrar_clips(conn, fila_id, [
                {"inicio_s": 0.0, "fim_s": 2.0, "score_claude": 9.0,
                 "motivo": "a", "hook_text": "a", "picos_energia": 0,
                 "score_final": 9.0, "picos_instantes": []},
                {"inicio_s": 2.0, "fim_s": 4.0, "score_claude": 8.0,
                 "motivo": "b", "hook_text": "b", "picos_energia": 0,
                 "score_final": 8.0, "picos_instantes": []}])
            ids = [c["id"] for c in repositorio.clips_do_video(conn, fila_id)]
            clips = []
            for cid in ids:
                mp4 = os.path.join(render, f"{vid}_{cid}.mp4")
                clip(mp4)
                ass = os.path.join(render, f"{vid}_{cid}.ass")
                open(ass, "w").write("ass")
                capa = os.path.join(render, f"{vid}_{cid}_capa")
                os.makedirs(capa)
                png = os.path.join(capa, "capa_1080x1920.png")
                open(png, "wb").write(b"png")
                repositorio.registrar_render(conn, cid, mp4, duracao_s=2.0,
                                             capa_path=png)
                clips.append((mp4, ass, png))
            linha = conn.execute("SELECT * FROM fila_clips WHERE id = ?",
                                 (fila_id,)).fetchone()
            kw = dict(downloads_dir=downloads, render_dir=render, ffmpeg=ffmpeg)

            sim = limpeza.limpar_video(conn, linha, simular=True, **kw)
            passo("--simular lista sem apagar",
                  sim["estado"] == "simulado" and all(
                      os.path.exists(p) for p in (fonte, audio, parte)),
                  f"{sim['bytes']} bytes, {len(sim['arquivos'])} arquivos")

            # caminho fora do diretório registrado no banco: tem de ser recusado
            repositorio.registrar_midia(conn, fila_id, video_path=precioso)
            real = limpeza.limpar_video(conn, linha, simular=False, **kw)
            recusados = [c for c, _m in real.get("recusados", [])]
            passo("caminho fora do diretório é recusado",
                  precioso in recusados and os.path.exists(precioso),
                  f"recusados={[os.path.basename(c) for c in recusados]}")
            passo("fonte e intermediários apagados",
                  real["estado"] == "apagado" and not any(
                      os.path.exists(p) for p in (fonte, audio, parte,
                                                  clips[0][1], clips[1][1])),
                  f"{real['bytes']} bytes liberados")
            passo("clips, capas, transcrição, outro vídeo e banco intactos",
                  all(os.path.exists(p) for c in clips for p in (c[0], c[2]))
                  and os.path.exists(transcricao) and os.path.exists(outro)
                  and os.path.exists(bd))
            midia = repositorio.midia(conn, fila_id)
            passo("registrado no banco (fonte_apagada_em, bytes)",
                  bool(midia["fonte_apagada_em"])
                  and midia["fonte_bytes_liberados"] == real["bytes"],
                  str(midia["fonte_apagada_em"]))
            passo("idempotente: segunda rodada não candidata o vídeo",
                  limpeza.candidatos(conn) == [])
        finally:
            conn.close()
    return resultado


# --- principal -----------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--saida", default=os.path.join("teste_local", "qualidade"))
    p.add_argument("--fonte", default=None,
                   help="vídeo local de origem (padrão: o YTDown da bancada)")
    p.add_argument("--inicio", type=float, default=75.0)
    p.add_argument("--duracao", type=float, default=30.0)
    p.add_argument("--repeticoes", type=int, default=1,
                   help="renders por variante para o tempo (usa o mínimo)")
    p.add_argument("--pular-render", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.path.insert(0, RAIZ)

    from tools.comparativo_local import grade
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    saida = os.path.abspath(args.saida)
    os.makedirs(os.path.join(saida, "tmp"), exist_ok=True)

    sintetica = os.path.join(saida, "fonte_sintetica.mp4")
    if args.fonte and not os.path.exists(sintetica):
        gerar_fonte(os.path.abspath(args.fonte), sintetica, args.inicio,
                    args.duracao, ffmpeg, os.path.join(saida, "tmp"))
    if not os.path.exists(sintetica):
        raise SystemExit("passe --fonte para gerar a fonte sintética")

    relatorio = {}
    if not args.pular_render:
        relatorio = renderizar_variantes(sintetica, saida, args.duracao, ffmpeg,
                                         args.repeticoes)
        medir(relatorio)
        frame_de_cada(relatorio, saida)
        grade([i["caminho"] for i in relatorio.values()],
              os.path.join(saida, "comparativo.mp4"))
    limpeza_res = testar_limpeza(saida, ffmpeg)
    with open(os.path.join(saida, "relatorio.json"), "w", encoding="utf-8") as f:
        json.dump({"videos": relatorio, "limpeza": limpeza_res}, f,
                  ensure_ascii=False, indent=2)
    for nome, i in relatorio.items():
        print(f"{nome:<28} {i['resolucao']:>10} {i['duracao_s']:>6.1f}s "
              f"{i['lufs']} LUFS  {i['tempo_render_s']}s")
    return 0 if limpeza_res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
