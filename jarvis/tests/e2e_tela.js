/* Ponta a ponta da tela 800×480, com Playwright (Chromium já instalado no ambiente).
 *
 * Pré-requisito: o backend no ar com o mesmo token e o provedor mock.
 *   JARVIS_TOKEN=... JARVIS_LLM=mock JARVIS_MOCK_ATRASO_S=0.15 .venv/bin/python -m backend
 *   JARVIS_TOKEN=... node tests/e2e_tela.js
 *
 * Grava as capturas em docs/screens/ (ou em JARVIS_CAPTURAS). Sai com código 1 se algo falhar.
 */
"use strict";

const path = require("path");
const fs = require("fs");
const { chromium } = require("playwright");

const BASE = (process.env.JARVIS_URL || "http://localhost:8000").replace(/\/+$/, "");
const TOKEN = process.env.JARVIS_TOKEN || "";
const SAIDA = process.env.JARVIS_CAPTURAS || path.join(__dirname, "..", "docs", "screens");

if (TOKEN.length < 16) {
  console.error("defina JARVIS_TOKEN com o mesmo valor do backend");
  process.exit(2);
}

const falhas = [];
function checar(nome, condicao, detalhe) {
  console.log((condicao ? "ok     " : "FALHA  ") + nome + (detalhe ? "  (" + detalhe + ")" : ""));
  if (!condicao) falhas.push(nome);
}

async function api(caminho, metodo, corpo) {
  const r = await fetch(BASE + caminho, {
    method: metodo || "GET",
    headers: { "X-Jarvis-Token": TOKEN, "Content-Type": "application/json" },
    body: corpo ? JSON.stringify(corpo) : undefined,
  });
  if (!r.ok) throw new Error((metodo || "GET") + " " + caminho + " -> " + r.status);
  return r.status === 204 ? null : r.json();
}

// Dados de exemplo, só se a base estiver vazia (o teste pode rodar várias vezes).
async function semearDados() {
  if ((await api("/notes")).length === 0) {
    await api("/notes", "POST", { titulo: "Compras", texto: "Café\nLeite integral\nPão" });
    await api("/notes", "POST", { titulo: "Ideia para o vídeo", texto: "Cortar o trecho da entrevista" });
    await api("/notes", "POST", { titulo: "Reunião de terça", texto: "Levar o contrato revisado" });
  }
  if ((await api("/agenda")).length === 0) {
    const daqui = (h) => new Date(Date.now() + h * 3600e3).toISOString();
    await api("/agenda", "POST", { titulo: "Gravar episódio", quando: daqui(2), descricao: "Estúdio 2" });
    await api("/agenda", "POST", { titulo: "Entrevista com a equipe", quando: daqui(26), descricao: "Chamada de vídeo" });
    await api("/agenda", "POST", { titulo: "Revisar roteiro", quando: daqui(50) });
  }
  if ((await api("/reminders")).length === 0) {
    await api("/reminders", "POST", { texto: "Tomar água", quando: new Date(Date.now() + 3 * 3600e3).toISOString() });
  }
}

const esperar = (page, fn, arg, timeout) => page.waitForFunction(fn, arg, { timeout: timeout || 6000 });

(async () => {
  await semearDados();
  fs.mkdirSync(SAIDA, { recursive: true });

  const navegador = await chromium.launch();
  const contextoComToken = (opcoes) =>
    navegador.newContext(Object.assign({ viewport: { width: 800, height: 480 }, deviceScaleFactor: 2 }, opcoes || {}))
      .then(async (ctx) => {
        await ctx.addInitScript((t) => { try { localStorage.setItem("jarvis_token", t); } catch (e) { /* sem storage */ } }, TOKEN);
        return ctx;
      });

  const foto = (page, nome) => page.screenshot({ path: path.join(SAIDA, nome) });
  const tela = (page) => page.getAttribute("#tela", "data-tela");
  const mascote = (page) => page.getAttribute("#tela", "data-estado");

  // ---------- tela principal (800×480, como no Pi) ----------
  const ctx = await contextoComToken();
  const page = await ctx.newPage();
  const envios = [];
  const erros = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().endsWith("/input/key")) envios.push(JSON.parse(req.postData()));
  });
  page.on("pageerror", (e) => erros.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") erros.push(m.text()); });

  await page.goto(BASE + "/");
  await esperar(page, () => document.getElementById("m-cpu").textContent !== "--", null, 8000);

  const dims = await page.evaluate(() => ({
    rolagemW: document.documentElement.scrollWidth,
    rolagemH: document.documentElement.scrollHeight,
    telaW: document.getElementById("tela").offsetWidth,
    telaH: document.getElementById("tela").offsetHeight,
  }));
  checar("layout é 800×480 e não rola", dims.rolagemW === 800 && dims.rolagemH === 480 && dims.telaW === 800 && dims.telaH === 480, JSON.stringify(dims));
  const barra = await page.textContent("#barra");
  checar("barra superior: JARVIS, hora, temperatura, CPU e RAM", barra.includes("JARVIS") && barra.includes("TEMP") && barra.includes("CPU") && barra.includes("RAM") && (await page.textContent("#hora")) !== "--:--");
  checar("início: mascote descansando", (await mascote(page)) === "descansando");
  checar("início: próximas tarefas vindas de /status", (await page.locator("#proximas .item").count()) >= 1);
  await foto(page, "01-inicio.png");

  // Tecla 1 abre o campo; Esc (= tecla 3) fecha.
  await page.keyboard.press("1");
  await esperar(page, () => !document.getElementById("entrada").hidden);
  checar("tecla 1 abre o campo de texto em Resposta", (await tela(page)) === "resposta" && (await page.evaluate(() => document.activeElement.id)) === "campo");
  await page.keyboard.press("Escape");
  await esperar(page, () => document.getElementById("entrada").hidden);
  checar("Esc (tecla 3) fecha o campo sem sair da tela", (await tela(page)) === "resposta");

  // Cancelar uma resposta com a tecla 3.
  await page.keyboard.press("1");
  await esperar(page, () => !document.getElementById("entrada").hidden);
  await page.keyboard.type("Teste de cancelamento");
  await page.keyboard.press("Enter");
  await esperar(page, () => document.getElementById("resposta").textContent.length > 0, null, 8000);
  await page.keyboard.press("3");
  await esperar(page, () => document.getElementById("metricas-resposta").textContent === "cancelado", null, 4000);
  checar("tecla 3 cancela a resposta em andamento", true);

  // Conversa completa, com o mascote passando por falando.
  await page.keyboard.press("1");
  await esperar(page, () => !document.getElementById("entrada").hidden);
  await page.keyboard.type("Qual é a previsão para amanhã?");
  await page.keyboard.press("Enter");
  await esperar(page, () => document.getElementById("tela").dataset.estado === "falando", null, 8000);
  checar("mascote passa a falando durante o streaming", true);
  await page.waitForTimeout(1200);
  await foto(page, "02-resposta-falando.png");
  // "no total" só aparece nas métricas finais; durante o streaming só há tokens/s ao vivo.
  await esperar(page, () => /no total/.test(document.getElementById("metricas-resposta").textContent), null, 10000);
  const resposta = await page.textContent("#resposta");
  checar("resposta chega inteira, com tokens/s", resposta.includes("Você disse") && /tokens\/s/.test(await page.textContent("#metricas-resposta")), await page.textContent("#metricas-resposta"));
  await esperar(page, () => document.getElementById("tela").dataset.estado === "descansando", null, 6000);
  checar("mascote volta a descansando ao fim da resposta", true);
  await foto(page, "03-resposta.png");

  // Tecla 2 percorre as telas; tecla 3 volta à anterior.
  await page.keyboard.press("2");
  await esperar(page, () => document.getElementById("tela").dataset.tela === "agenda");
  await esperar(page, () => document.querySelectorAll("#lista-agenda .item").length > 0);
  checar("tecla 2 vai para Agenda, com compromissos de /agenda", true);
  await foto(page, "04-agenda.png");

  await page.keyboard.press("2");
  await esperar(page, () => document.getElementById("tela").dataset.tela === "notas");
  await esperar(page, () => document.querySelectorAll("#lista-notas .item").length > 0);
  checar("tecla 2 vai para Notas, com notas de /notes", true);
  await foto(page, "05-notas.png");

  await page.keyboard.press("2");
  await esperar(page, () => document.getElementById("tela").dataset.tela === "status");
  await esperar(page, () => document.getElementById("s-cpu").textContent !== "--");
  checar("tecla 2 vai para Status, com sensores de /status", true);
  await foto(page, "06-status.png");

  await page.keyboard.press("3");
  await esperar(page, () => document.getElementById("tela").dataset.tela === "notas");
  checar("tecla 3 volta à tela anterior", true);

  // Estados do mascote (o gancho window.jarvisTela só existe para isto).
  await page.evaluate(() => window.jarvisTela.tela("inicio"));
  for (const estado of ["ouvindo", "pensando", "falando", "erro"]) {
    await page.evaluate((e) => window.jarvisTela.mascote(e), estado);
    checar("mascote em " + estado, (await mascote(page)) === estado);
    await page.waitForTimeout(350);
    await foto(page, "07-mascote-" + estado + ".png");
  }
  await page.evaluate(() => window.jarvisTela.mascote("descansando"));

  const teclasEnviadas = envios.map((e) => e.key + ":" + e.action).join(" ");
  checar("teclas viram POST /input/key (down e up)", envios.some((e) => e.key === 1 && e.action === "down") && envios.some((e) => e.key === 2 && e.action === "up"), teclasEnviadas);
  checar("sem erros de JavaScript", erros.length === 0, erros.join(" | "));
  await ctx.close();

  // ---------- simulador (?sim=1) ----------
  const ctxSim = await contextoComToken({ viewport: { width: 1000, height: 720 } });
  const sim = await ctxSim.newPage();
  await sim.goto(BASE + "/?sim=1");
  await esperar(sim, () => document.getElementById("m-cpu").textContent !== "--", null, 8000);
  const botoes = await sim.locator("#controles button").count();
  checar("simulador: moldura centralizada com 3 botões", botoes === 3 && (await sim.isVisible("#controles")));
  await sim.click('#controles [data-tecla="2"]');
  await esperar(sim, () => document.getElementById("tela").dataset.tela === "resposta");
  checar("simulador: botão 2 troca de tela como a tecla", true);
  await foto(sim, "00-simulador.png");
  await ctxSim.close();

  // ---------- sem token: a tela pede o token ----------
  const ctxNovo = await navegador.newContext({ viewport: { width: 800, height: 480 }, deviceScaleFactor: 2 });
  const semToken = await ctxNovo.newPage();
  await semToken.goto(BASE + "/");
  await esperar(semToken, () => !document.getElementById("login").hidden, null, 5000);
  checar("sem token, a tela pede o token", true);
  await foto(semToken, "08-token.png");
  await ctxNovo.close();

  await navegador.close();
  console.log(falhas.length ? "\n" + falhas.length + " falha(s): " + falhas.join("; ") : "\nTodos os testes passaram.");
  process.exit(falhas.length ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
