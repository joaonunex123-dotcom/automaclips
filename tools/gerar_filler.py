"""Gera vídeos de preenchimento 100% procedurais para o layout split.

    python tools/gerar_filler.py [categoria ...] [--destino assets/filler]
                                 [--duracao 60] [--largura 1080 --altura 768]
                                 [--fps 30] [--semente 7] [--teste]

Categorias: slime, satisfatorio, parkour, asmr_geometrico (todas, por padrão).

Nada é baixado nem copiado de lugar nenhum: cada quadro nasce de numpy/OpenCV
e vai por pipe para o ffmpeg (libx264, sem trilha de áudio). A mesma semente
produz o mesmo vídeo. Todas as animações são periódicas na duração pedida, então
o arquivo entra em loop sem emenda visível.

O tamanho padrão (1080x768) é a faixa de baixo do split com proporcao_clip=0.6
(1920 - 1152 = 768); o render cobre e corta ao centro, então outro tamanho
também funciona. `--teste` gera 4 s por categoria, para checar rápido.

Dependências de desenvolvimento: numpy e opencv (cv2). O render do pipeline não
precisa delas — o que ele lê é só o .mp4 pronto.

Os .mp4 ficam em assets/filler/, ignorado pelo git; o que se versiona é este
gerador.
"""
import argparse
import colorsys
import math
import os
import subprocess
import sys

import numpy as np

TAU = 2.0 * math.pi
CATEGORIAS = ("slime", "satisfatorio", "parkour", "asmr_geometrico")


def _hsv(h, s, v):
    return np.array(colorsys.hsv_to_rgb(h % 1.0, s, v), dtype=np.float32)


def _suave(x, a, b):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# --- slime --------------------------------------------------------------------

class Slime:
    """Metaballs coloridas e brilhantes em movimento lento, em loop."""

    def __init__(self, w, h, duracao, rng):
        self.w, self.h, self.duracao = w, h, duracao
        # Campo calculado a 1/3 da resolução e ampliado: o slime é suave de
        # propósito, e isso triplica a velocidade.
        self.cw, self.ch = w // 3, h // 3
        n = 9
        self.cx = rng.uniform(0.2, 0.8, n) * self.cw
        self.cy = rng.uniform(0.2, 0.8, n) * self.ch
        self.ax = rng.uniform(0.10, 0.30, n) * self.cw
        self.ay = rng.uniform(0.10, 0.30, n) * self.ch
        self.kx = rng.integers(1, 4, n)          # ciclos inteiros por loop
        self.ky = rng.integers(1, 4, n)
        self.fx = rng.uniform(0, TAU, n)
        self.fy = rng.uniform(0, TAU, n)
        self.raio = rng.uniform(0.17, 0.30, n) * self.ch
        base = rng.uniform(0, 1)
        self.cor = np.stack([_hsv(base + i * 0.13, 0.85, 1.0) for i in range(n)])
        yy, xx = np.mgrid[0:self.ch, 0:self.cw].astype(np.float32)
        self.xx, self.yy = xx, yy

    def quadro(self, t):
        import cv2
        fase = TAU * t / self.duracao
        campo = np.zeros((self.ch, self.cw), np.float32)
        cor = np.zeros((self.ch, self.cw, 3), np.float32)
        for i in range(len(self.cx)):
            x = self.cx[i] + self.ax[i] * math.sin(self.kx[i] * fase + self.fx[i])
            y = self.cy[i] + self.ay[i] * math.sin(self.ky[i] * fase + self.fy[i])
            raio = self.raio[i] * (1.0 + 0.2 * math.sin(2 * fase + self.fx[i]))
            # Suporte compacto (Wyvill): (1 - d²/r²)³ vai a zero em d = r, sem
            # a singularidade 1/d² que estoura o brilho no centro.
            d2 = ((self.xx - x) ** 2 + (self.yy - y) ** 2) / (raio * raio)
            c = np.clip(1.0 - d2, 0.0, 1.0) ** 3
            campo += c
            cor += c[..., None] * self.cor[i]
        cor /= np.maximum(campo[..., None], 1e-6)

        # Altura em cúpula: a raiz do excesso sobre o limiar dá bordas
        # arredondadas, e o gradiente dela é a normal do brilho de gelatina.
        massa = _suave(campo, 0.20, 0.27)
        altura = np.sqrt(np.clip(campo - 0.2, 0.0, None))
        altura = cv2.GaussianBlur(altura, (0, 0), 1.0)
        gy, gx = np.gradient(altura)
        k = 5.0
        nx, ny = -gx * k, -gy * k
        inv = 1.0 / np.sqrt(nx * nx + ny * ny + 1.0)
        nx, ny, nz = nx * inv, ny * inv, inv
        luz = np.array([-0.45, -0.6, 0.66], np.float32)
        luz /= np.linalg.norm(luz)
        difusa = np.clip(nx * luz[0] + ny * luz[1] + nz * luz[2], 0, 1)
        meio = luz + np.array([0, 0, 1], np.float32)
        meio /= np.linalg.norm(meio)
        espec = np.clip(nx * meio[0] + ny * meio[1] + nz * meio[2], 0, 1) ** 28
        borda = (1.0 - nz) * 1.4                       # luz de contorno

        cor_slime = cor * (0.45 + 0.75 * difusa[..., None])
        cor_slime += 0.35 * borda[..., None] * cor
        cor_slime += 0.9 * espec[..., None]
        fundo = np.array([0.05, 0.04, 0.11], np.float32)
        fundo = fundo + 0.04 * np.sin(self.yy / self.ch * 3.0 + fase)[..., None]
        img = fundo * (1 - massa[..., None]) + cor_slime * massa[..., None]
        img = np.clip(img, 0, 1)
        # O cúbico passa de [0,1] nas bordas; sem clipar o uint8 dá a volta.
        return np.clip(cv2.resize(img, (self.w, self.h),
                                  interpolation=cv2.INTER_CUBIC), 0, 1)


# --- satisfatório: ladrilhos deslizantes --------------------------------------

class Ladrilhos:
    """Linhas de ladrilhos arredondados que deslizam e se encaixam, em loop."""

    def __init__(self, w, h, duracao, rng):
        self.w, self.h, self.duracao = w, h, duracao
        self.colunas = 8
        self.lado = w / self.colunas
        self.linhas = int(math.ceil(h / self.lado)) + 1
        self.matizes = rng.uniform(0, 1, 6)
        self.passos = max(1, int(duracao // 2))   # um encaixe a cada ~2 s
        self.fase_linha = rng.uniform(0, 1, self.linhas) * 0.35
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        self.xx, self.yy = xx, yy
        self.linha_idx = np.floor(yy / self.lado).astype(np.int32)
        self.v = (yy / self.lado) - self.linha_idx          # [0,1) dentro da célula

    def quadro(self, t):
        prog = (t / self.duracao) * self.passos             # ladrilhos percorridos
        inteiro = math.floor(prog)
        frac = prog - inteiro
        img = np.zeros((self.h, self.w, 3), np.float32)
        r = 0.22                                            # arredondamento
        # Uma linha por vez: o deslocamento tem easing e cada linha entra com
        # uma pequena defasagem, o que dá o "ripple" de encaixe.
        for L in range(self.linhas):
            local = np.clip((frac - self.fase_linha[L]) / 0.6, 0, 1)
            ease = local * local * (3 - 2 * local)
            sentido = 1 if L % 2 == 0 else -1
            desloc = (inteiro + ease) * sentido
            mascara = self.linha_idx == L
            if not mascara.any():
                continue
            x_cel = (self.xx[mascara] / self.lado) - desloc
            idx = np.floor(x_cel)
            u = x_cel - idx
            v = self.v[mascara]
            # SDF de caixa arredondada (célula centrada em 0.5, 0.5).
            qx = np.abs(u - 0.5) - (0.5 - 0.06 - r)
            qy = np.abs(v - 0.5) - (0.5 - 0.06 - r)
            d = (np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2)
                 + np.minimum(np.maximum(qx, qy), 0) - r)
            cobertura = 1.0 - _suave(d, -0.01, 0.015)
            matiz = (self.matizes[(idx.astype(np.int64) + 2 * L) % 6]
                     + 0.03 * L)
            # Cor por tabela de HSV vetorizada (cv2 faz o cálculo).
            h6 = (matiz % 1.0) * 6.0
            c = 0.85
            x = c * (1 - np.abs(h6 % 2 - 1))
            z = np.zeros_like(h6)
            conds = [h6 < 1, h6 < 2, h6 < 3, h6 < 4, h6 < 5, h6 >= 5]
            r_ = np.select(conds, [c, x, z, z, x, c])
            g_ = np.select(conds, [x, c, c, x, z, z])
            b_ = np.select(conds, [z, z, x, c, c, x])
            m = 0.15
            brilho = 0.75 + 0.35 * (1.0 - v) * (1.0 - 0.6 * np.abs(u - 0.5))
            rgb = np.stack([r_ + m, g_ + m, b_ + m], axis=-1) * brilho[:, None]
            fundo = np.array([0.05, 0.05, 0.09], np.float32)
            img[mascara] = fundo * (1 - cobertura[:, None]) + rgb * cobertura[:, None]
        return np.clip(img, 0, 1)


# --- parkour: corredor em perspectiva -----------------------------------------

class Parkour:
    """Corredor de blocos 3D estilizados vistos em primeira pessoa, em loop.

    Geometria própria (caixas planas coloridas, sem textura de jogo algum). A
    câmera avança em saltos por um caminho que se repete a cada `periodo`
    unidades, sincronizado com a duração do vídeo.
    """

    def __init__(self, w, h, duracao, rng):
        self.w, self.h, self.duracao = w, h, duracao
        self.periodo = 60                              # blocos de comprimento
        self.faixas = 5
        # grade[z][faixa] = altura do bloco (0 = vazio)
        grade = np.zeros((self.periodo, self.faixas), np.int32)
        z = 0
        while z < self.periodo:
            faixa_ok = rng.integers(0, self.faixas)
            for k in range(rng.integers(3, 6)):
                if z >= self.periodo:
                    break
                grade[z, :] = rng.choice([0, 1, 1, 1, 2], self.faixas)
                grade[z, faixa_ok] = 1
                z += 1
            z += rng.integers(1, 3)               # buraco: obriga a pular
        grade[0:4, :] = 1
        grade[-4:, :] = 1
        self.grade = grade
        self.matiz = rng.uniform(0, 1)
        self.cor_z = [_hsv(self.matiz + (z % 12) / 12.0 * 0.35, 0.75, 1.0)
                      for z in range(self.periodo)]
        self.foco = h * 1.05
        self.velocidade = self.periodo / duracao       # blocos/s

    def _proj(self, x, y, z):
        s = self.foco / np.maximum(z, 0.05)
        return (self.w * 0.5 + x * s, self.h * 0.52 - y * s)

    def quadro(self, t):
        import cv2
        w, h = self.w, self.h
        ceu = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
        topo = _hsv(self.matiz + 0.55, 0.55, 0.35)
        base = _hsv(self.matiz + 0.62, 0.35, 0.95)
        img = (topo * (1 - ceu) + base * ceu) * np.ones((1, w, 1), np.float32)
        img = np.ascontiguousarray(img)

        avanco = (t * self.velocidade) % self.periodo
        # Salto: parábola a cada 0.6 s, e leve balanço lateral.
        ciclo = (t / 0.6) % 1.0
        altura_cam = 1.9 + 0.55 * 4 * ciclo * (1 - ciclo)
        balanco = 0.35 * math.sin(TAU * t / self.duracao * 3)
        cam_x = (self.faixas - 1) / 2.0 + balanco

        quads = []
        alcance = 26
        for dz in range(alcance, -2, -1):
            zi = int(math.floor(avanco)) + dz
            linha = self.grade[zi % self.periodo]
            for f in range(self.faixas):
                alt = linha[f]
                if alt <= 0:
                    continue
                z0 = zi - avanco + 1.0
                z1 = z0 + 1.0
                if z1 <= 0.1:
                    continue
                z0 = max(z0, 0.1)
                x0, x1 = f - cam_x - 0.5, f - cam_x + 0.5
                topo_y = alt - altura_cam
                base_y = -altura_cam
                quads.append((z0, z1, x0, x1, base_y, topo_y, self.cor_z[zi % self.periodo]))
        # Mais longe primeiro (algoritmo do pintor).
        quads.sort(key=lambda q: -q[0])
        for z0, z1, x0, x1, by, ty, cor in quads:
            nevoa = float(np.clip(z0 / alcance, 0, 1)) ** 1.4
            fundo_nevoa = base
            def tom(fator):
                c = np.clip(cor * fator, 0, 1)
                return c * (1 - nevoa) + fundo_nevoa * nevoa
            # frente (face voltada para a câmera, em z0)
            pts = [self._proj(x0, by, z0), self._proj(x1, by, z0),
                   self._proj(x1, ty, z0), self._proj(x0, ty, z0)]
            cv2.fillConvexPoly(img, np.array(pts, np.int32), tuple(float(v) for v in tom(0.62)[::-1]))
            # topo
            pts = [self._proj(x0, ty, z0), self._proj(x1, ty, z0),
                   self._proj(x1, ty, z1), self._proj(x0, ty, z1)]
            cv2.fillConvexPoly(img, np.array(pts, np.int32), tuple(float(v) for v in tom(1.0)[::-1]))
            # lateral visível
            xs = x0 if (x0 + x1) / 2 > 0 else x1
            if x0 > 0 or x1 < 0:
                xv = x0 if x0 > 0 else x1
                pts = [self._proj(xv, by, z0), self._proj(xv, by, z1),
                       self._proj(xv, ty, z1), self._proj(xv, ty, z0)]
                cv2.fillConvexPoly(img, np.array(pts, np.int32),
                                   tuple(float(v) for v in tom(0.45)[::-1]))
        # img está em BGR por causa do cv2; converte de volta para RGB.
        return np.clip(img[..., ::-1], 0, 1)


# --- asmr geométrico ----------------------------------------------------------

class Geometrico:
    """Grade de formas cujo tamanho ondula em ondas concêntricas suaves."""

    def __init__(self, w, h, duracao, rng):
        self.w, self.h, self.duracao = w, h, duracao
        self.celula = 54.0
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        self.u = (xx / self.celula) % 1.0 - 0.5
        self.v = (yy / self.celula) % 1.0 - 0.5
        self.ix = np.floor(xx / self.celula)
        self.iy = np.floor(yy / self.celula)
        cx, cy = w / self.celula / 2.0, h / self.celula / 2.0
        self.dist = np.sqrt((self.ix - cx) ** 2 + (self.iy - cy) ** 2)
        self.ang = np.arctan2(self.iy - cy, self.ix - cx)
        self.matiz0 = rng.uniform(0, 1)

    def quadro(self, t):
        fase = TAU * t / self.duracao
        # 4 ciclos inteiros por loop: a onda sai do centro e nunca emenda.
        onda = 0.5 + 0.5 * np.sin(self.dist * 0.55 - 4 * fase)
        onda2 = 0.5 + 0.5 * np.sin(self.ang * 3 + 2 * fase + self.dist * 0.2)
        tam = 0.12 + 0.34 * onda
        # Forma: mistura de círculo e losango conforme a segunda onda.
        circ = np.sqrt(self.u ** 2 + self.v ** 2)
        losango = (np.abs(self.u) + np.abs(self.v)) * 0.8
        forma = circ * (1 - onda2) + losango * onda2
        cobertura = 1.0 - _suave(forma, tam - 0.015, tam + 0.015)
        matiz = (self.matiz0 + 0.0035 * self.dist * 6 + 0.3 * onda) % 1.0
        h6 = matiz * 6.0
        c = 0.7
        x = c * (1 - np.abs(h6 % 2 - 1))
        z = np.zeros_like(h6)
        conds = [h6 < 1, h6 < 2, h6 < 3, h6 < 4, h6 < 5, h6 >= 5]
        r = np.select(conds, [c, x, z, z, x, c]) + 0.3
        g = np.select(conds, [x, c, c, x, z, z]) + 0.3
        b = np.select(conds, [z, z, x, c, c, x]) + 0.3
        rgb = np.stack([r, g, b], axis=-1)
        fundo = np.array([0.07, 0.08, 0.13], np.float32)
        img = fundo * (1 - cobertura[..., None]) + rgb * cobertura[..., None]
        return np.clip(img, 0, 1)


GERADORES = {
    "slime": Slime,
    "satisfatorio": Ladrilhos,
    "parkour": Parkour,
    "asmr_geometrico": Geometrico,
}


def _abrir_ffmpeg(caminho, w, h, fps):
    return subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
         "-r", str(fps), "-i", "-", "-an",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", caminho],
        stdin=subprocess.PIPE,
    )


def gerar_categoria(categoria, destino, duracao, w, h, fps, semente):
    pasta = os.path.join(destino, categoria)
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, "filler.mp4")
    rng = np.random.default_rng(
        [semente, sum(ord(c) for c in categoria)]
    )
    gerador = GERADORES[categoria](w, h, duracao, rng)
    proc = _abrir_ffmpeg(caminho, w, h, fps)
    total = int(round(duracao * fps))
    try:
        for i in range(total):
            quadro = gerador.quadro(i / fps)
            proc.stdin.write((quadro * 255.0 + 0.5).astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        codigo = proc.wait()
    if codigo != 0:
        raise RuntimeError(f"ffmpeg falhou ao gerar {categoria} (código {codigo})")
    with open(os.path.join(pasta, "LICENCA.txt"), "w", encoding="utf-8") as f:
        f.write("Gerado por código nesta sessão (tools/gerar_filler.py, "
                f"semente {semente}), uso livre.\n"
                "Nenhum material de terceiros: formas, cores e movimento "
                "calculados com numpy/OpenCV e codificados pelo ffmpeg.\n")
    return caminho


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("categorias", nargs="*", choices=CATEGORIAS)
    p.add_argument("--destino", default=os.path.join("assets", "filler"))
    p.add_argument("--duracao", type=float, default=60.0)
    p.add_argument("--largura", type=int, default=1080)
    p.add_argument("--altura", type=int, default=768)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--semente", type=int, default=7)
    p.add_argument("--teste", action="store_true", help="4 s por categoria")
    args = p.parse_args(argv)

    duracao = 4.0 if args.teste else args.duracao
    for cat in args.categorias or CATEGORIAS:
        caminho = gerar_categoria(cat, args.destino, duracao, args.largura,
                                  args.altura, args.fps, args.semente)
        print(f"{cat:<16} {duracao:5.1f} s  {caminho}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
