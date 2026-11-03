// Tela 800×480 do Jarvis: HTML/CSS/JS puro, sem build.
// Teclas: o teclado e o simulador mandam POST /input/key. A tela reage ao evento
// "tecla" que volta pelo /events, o mesmo caminho do GPIO. Ver docs/decisoes.md, seção 19.
(function () {
  "use strict";

  var CFG = Object.assign({ backend: "", token: "", tz: "America/Sao_Paulo" }, window.JARVIS_CONFIG || {});
  var BASE = String(CFG.backend || "").replace(/\/+$/, "");
  var TZ = CFG.tz;
  var FMT_HORA = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  var FMT_DIA = new Intl.DateTimeFormat("sv-SE", { timeZone: TZ });
  var FMT_DATA = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, weekday: "long", day: "numeric", month: "long" });
  var PARAMS = new URLSearchParams(location.search);
  var SIM = PARAMS.get("sim") === "1";
  var TELAS = ["inicio", "resposta", "agenda", "notas", "status"];
  var NOMES = { inicio: "Início", resposta: "Resposta", agenda: "Agenda", notas: "Notas", status: "Status" };
  var MASCOTE_SVG =
    '<svg viewBox="-10 -10 220 220" aria-hidden="true">' +
      '<g class="m-antena"><line x1="100" y1="40" x2="100" y2="20"/><circle cx="100" cy="14" r="9"/></g>' +
      '<circle class="m-onda" cx="100" cy="100" r="96"/>' +
      '<g class="m-cabeca">' +
        '<rect class="m-corpo" x="34" y="40" width="132" height="128" rx="38"/>' +
        '<rect class="m-face" x="50" y="58" width="100" height="88" rx="26"/>' +
        '<g class="m-olhos">' +
          '<ellipse class="m-olho" cx="80" cy="98" rx="12" ry="15"/>' +
          '<ellipse class="m-olho" cx="120" cy="98" rx="12" ry="15"/>' +
          '<path class="m-fechado" d="M66 100 q14 12 28 0 M106 100 q14 12 28 0"/>' +
        '</g>' +
        '<rect class="m-boca" x="82" y="128" width="36" height="9" rx="4.5"/>' +
        '<path class="m-boca-erro" d="M80 134 l7 -7 l7 7 l7 -7 l7 7 l7 -7 l7 7"/>' +
      '</g>' +
      '<g class="m-pensando"><circle cx="172" cy="40" r="5"/><circle cx="186" cy="28" r="5"/><circle cx="200" cy="16" r="5"/></g>' +
      '<text class="m-zzz" x="170" y="60">z</text>' +
    '</svg>';

  var $ = function (id) { return document.getElementById(id); };
  var estado = {
    tela: "inicio",
    historico: [],
    token: "",
    sse: null,
    proximas: [],
    agenda: [],
    notas: [],
    status: null,
  };
  var chat = { ws: null, gerando: false, pergunta: "", texto: "", tokens: 0, primeiro: 0, metricas: null, erro: "" };
  var teclasPressionadas = {};

  // ---------- utilidades ----------

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function fmt(n, casas) {
    var c = casas === undefined ? 1 : casas;
    return Number(n).toFixed(c).replace(".", ",");
  }
  function limitar(n) { return Math.max(0, Math.min(100, n)); }
  function fmtMB(mb) { return mb >= 1024 ? fmt(mb / 1024, 1) + " GB" : Math.round(mb) + " MB"; }
  function formatarUptime(s) {
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return d + " d " + h + " h";
    if (h) return h + " h " + pad(m) + " min";
    return m + " min";
  }
  function hojeISO() { return FMT_DIA.format(new Date()); }
  function amanhaISO() { return FMT_DIA.format(new Date(Date.now() + 86400000)); }
  function diaRotulo(quando) {
    var dia = quando.slice(0, 10);
    if (dia === hojeISO()) return "hoje";
    if (dia === amanhaISO()) return "amanhã";
    return dia.slice(8, 10) + "/" + dia.slice(5, 7);
  }

  function url(caminho) { return BASE + caminho; }
  function urlWs(caminho) {
    var base = BASE
      ? BASE.replace(/^http/, "ws")
      : (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
    return base + caminho;
  }

  // ---------- token e API ----------

  function lerToken() {
    try {
      var salvo = localStorage.getItem("jarvis_token");
      if (salvo) return salvo;
    } catch (e) { /* navegador sem storage */ }
    return CFG.token ? String(CFG.token) : "";
  }
  function guardarToken(valor) {
    try { localStorage.setItem("jarvis_token", valor); } catch (e) { /* sem storage */ }
  }

  function api(caminho, opcoes) {
    opcoes = opcoes || {};
    var cabecalhos = { "X-Jarvis-Token": estado.token };
    var corpo;
    if (opcoes.corpo !== undefined) {
      cabecalhos["Content-Type"] = "application/json";
      corpo = JSON.stringify(opcoes.corpo);
    }
    return fetch(url(caminho), { method: opcoes.metodo || "GET", headers: cabecalhos, body: corpo })
      .then(function (r) {
        if (r.status === 401) { pedirToken(); throw new Error("token"); }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.status === 204 ? null : r.json();
      });
  }

  function enviarTecla(tecla, acao) {
    api("/input/key", { metodo: "POST", corpo: { key: tecla, action: acao } }).catch(function () {});
  }

  // ---------- mascote ----------

  function definirMascote(estadoNovo) {
    if (!estadoNovo) return;
    $("tela").setAttribute("data-estado", estadoNovo);
    $("rotulo").textContent = estadoNovo;
  }

  // ---------- relógio ----------

  var ultimoMinuto = "";
  function relogio() {
    var d = new Date();
    var hm = FMT_HORA.format(d);
    if (hm === ultimoMinuto) return;
    ultimoMinuto = hm;
    $("hora").textContent = hm;
    $("relogio").textContent = hm;
    $("data").textContent = FMT_DATA.format(d);
  }

  // ---------- listas ----------

  function linhaItem(hora, dia, titulo, sub) {
    var li = document.createElement("li");
    li.className = "item";
    if (hora !== null) {
      var h = document.createElement("div");
      h.className = "item-hora";
      h.textContent = hora;
      if (dia) {
        var s = document.createElement("small");
        s.textContent = dia;
        h.appendChild(s);
      }
      li.appendChild(h);
    }
    var corpo = document.createElement("div");
    corpo.className = "item-corpo";
    var t = document.createElement("div");
    t.className = "item-titulo";
    t.textContent = titulo;
    corpo.appendChild(t);
    if (sub) {
      var u = document.createElement("div");
      u.className = "item-sub";
      u.textContent = sub;
      corpo.appendChild(u);
    }
    li.appendChild(corpo);
    return li;
  }

  function mensagemVazia(lista, texto) {
    lista.innerHTML = "";
    var p = document.createElement("li");
    p.className = "vazio";
    p.textContent = texto;
    lista.appendChild(p);
  }

  function renderProximas() {
    var ul = $("proximas");
    if (!estado.proximas.length) { mensagemVazia(ul, "Nada agendado"); return; }
    ul.innerHTML = "";
    estado.proximas.slice(0, 3).forEach(function (item) {
      ul.appendChild(linhaItem(item.quando.slice(11, 16), diaRotulo(item.quando), item.titulo,
        item.tipo === "lembrete" ? "lembrete" : "compromisso"));
    });
  }

  function renderAgenda() {
    var ul = $("lista-agenda");
    if (!estado.agenda.length) { mensagemVazia(ul, "Nenhum compromisso"); return; }
    ul.innerHTML = "";
    estado.agenda.slice(0, 5).forEach(function (item) {
      ul.appendChild(linhaItem(item.quando.slice(11, 16), diaRotulo(item.quando), item.titulo, item.descricao || ""));
    });
  }

  function renderNotas() {
    var ul = $("lista-notas");
    if (!estado.notas.length) { mensagemVazia(ul, "Nenhuma nota"); return; }
    ul.innerHTML = "";
    estado.notas.slice(0, 5).forEach(function (nota) {
      var primeira = (nota.texto || "").split("\n").find(function (l) { return l.trim(); }) || "";
      ul.appendChild(linhaItem(null, null, nota.titulo, primeira));
    });
  }

  function renderStatus() {
    var s = estado.status;
    if (!s) return;
    $("s-temp").textContent = s.temperatura_c == null ? "--" : fmt(s.temperatura_c, 1) + " °C";
    $("s-cpu").textContent = Math.round(s.cpu_percent) + " %";
    $("s-cpu-barra").style.width = limitar(s.cpu_percent) + "%";
    $("s-ram").textContent = Math.round(s.ram.percent) + " %";
    $("s-ram-detalhe").textContent = fmtMB(s.ram.usada_mb) + " de " + fmtMB(s.ram.total_mb);
    $("s-ram-barra").style.width = limitar(s.ram.percent) + "%";
    $("s-uptime").textContent = formatarUptime(s.uptime_s);
  }

  // ---------- dados ----------

  function atualizarStatus() {
    if (!estado.token) return;
    return api("/status").then(function (s) {
      estado.status = s;
      $("m-temp").textContent = s.temperatura_c == null ? "--" : Math.round(s.temperatura_c) + "°";
      $("m-cpu").textContent = Math.round(s.cpu_percent) + "%";
      $("m-ram").textContent = Math.round(s.ram.percent) + "%";
      // O mascote vem só do SSE ("status"): o poll não o sobrescreve.
      $("tarefa").textContent = s.tarefa_atual || "";
      estado.proximas = s.proximas || [];
      renderProximas();
      renderStatus();
    }).catch(function () {});
  }

  function carregarAgenda() {
    if (!estado.token) return;
    return api("/agenda").then(function (lista) {
      var limite = Date.now() - 3600000;
      estado.agenda = lista
        .filter(function (x) { return Date.parse(x.quando) >= limite; })
        .sort(function (a, b) { return Date.parse(a.quando) - Date.parse(b.quando); });
      renderAgenda();
    }).catch(function () {});
  }

  function carregarNotas() {
    if (!estado.token) return;
    return api("/notes").then(function (lista) {
      estado.notas = lista.slice().sort(function (a, b) {
        return (b.atualizada_em || "").localeCompare(a.atualizada_em || "");
      });
      renderNotas();
    }).catch(function () {});
  }

  // ---------- navegação ----------

  function irPara(id, voltando) {
    if (TELAS.indexOf(id) < 0) return;
    if (!voltando && id !== estado.tela) {
      estado.historico.push(estado.tela);
      if (estado.historico.length > 20) estado.historico.shift();
    }
    estado.tela = id;
    $("tela").setAttribute("data-tela", id);
    document.querySelectorAll(".tela-conteudo").forEach(function (s) {
      s.classList.toggle("ativa", s.getAttribute("data-id") === id);
    });
    $("nome-tela").textContent = NOMES[id] + "  " + (TELAS.indexOf(id) + 1) + "/" + TELAS.length;
    if (id !== "resposta") fecharEntrada();
    if (id === "resposta") renderResposta();
    if (id === "agenda") carregarAgenda();
    if (id === "notas") carregarNotas();
    if (id === "status") atualizarStatus();
  }

  function proximaTela() {
    return TELAS[(TELAS.indexOf(estado.tela) + 1) % TELAS.length];
  }

  function voltar() {
    if (chat.gerando) { cancelarResposta(); return; }
    if (!$("entrada").hidden) { fecharEntrada(); return; }
    if (estado.historico.length) { irPara(estado.historico.pop(), true); return; }
    if (estado.tela !== "inicio") irPara("inicio", true);
  }

  // ---------- teclas ----------

  function abrirEntrada() {
    if (estado.tela !== "resposta") irPara("resposta");
    $("entrada").hidden = false;
    $("campo").focus();
  }

  function fecharEntrada() {
    if ($("entrada").hidden) return;
    $("entrada").hidden = true;
    $("campo").value = "";
    $("campo").blur();
  }

  function aoTecla(tecla) {
    if (tecla === 1) abrirEntrada();
    else if (tecla === 2) irPara(proximaTela());
    else if (tecla === 3) voltar();
  }

  function teclaDoEvento(ev) {
    if (ev.key === "Escape") return 3;
    if (ev.key === "1" || ev.key === "2" || ev.key === "3") return Number(ev.key);
    return 0;
  }

  function digitando() { return document.activeElement === $("campo"); }

  document.addEventListener("keydown", function (ev) {
    var tecla = teclaDoEvento(ev);
    if (!tecla) return;
    // Com o campo de texto aberto, 1/2/3 são letras. Esc continua valendo como tecla 3.
    if (tecla !== 3 && digitando()) return;
    ev.preventDefault();
    if (ev.repeat || teclasPressionadas[tecla]) return;
    teclasPressionadas[tecla] = true;
    enviarTecla(tecla, "down");
  });

  document.addEventListener("keyup", function (ev) {
    var tecla = teclaDoEvento(ev);
    if (!tecla || !teclasPressionadas[tecla]) return;
    delete teclasPressionadas[tecla];
    enviarTecla(tecla, "up");
  });

  document.querySelectorAll("#controles [data-tecla]").forEach(function (botao) {
    var tecla = Number(botao.getAttribute("data-tecla"));
    botao.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      if (teclasPressionadas[tecla]) return;
      teclasPressionadas[tecla] = true;
      enviarTecla(tecla, "down");
    });
    var soltar = function () {
      if (!teclasPressionadas[tecla]) return;
      delete teclasPressionadas[tecla];
      enviarTecla(tecla, "up");
    };
    botao.addEventListener("pointerup", soltar);
    botao.addEventListener("pointercancel", soltar);
  });

  // ---------- chat (/ws/chat) ----------

  function perguntar(texto) {
    if (chat.gerando) return;
    chat.pergunta = texto;
    chat.texto = "";
    chat.tokens = 0;
    chat.primeiro = 0;
    chat.metricas = null;
    chat.erro = "";
    chat.gerando = true;
    renderResposta();

    var ws = new WebSocket(urlWs("/ws/chat?token=" + encodeURIComponent(estado.token)));
    chat.ws = ws;
    ws.onopen = function () { ws.send(JSON.stringify({ text: texto })); };
    ws.onmessage = function (ev) {
      if (chat.ws !== ws) return;
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      receber(m);
    };
    ws.onclose = function () {
      if (chat.ws !== ws) return;
      chat.ws = null;
      if (chat.gerando) {
        chat.gerando = false;
        chat.erro = chat.erro || "conexão com o Jarvis caiu";
        renderResposta();
        // Um WS recusado antes do accept chega aqui sem código útil: /status diz se é o token.
        api("/status").catch(function () {});
      }
    };
  }

  function receber(m) {
    if (m.tipo === "token") {
      if (!chat.primeiro) chat.primeiro = performance.now();
      chat.tokens += 1;
      chat.texto += m.texto;
    } else if (m.tipo === "final") {
      chat.texto = m.texto;
      chat.metricas = m;
      encerrarWs();
    } else if (m.tipo === "erro") {
      chat.erro = m.erro;
      encerrarWs();
    }
    renderResposta();
  }

  function encerrarWs() {
    chat.gerando = false;
    var ws = chat.ws;
    chat.ws = null;
    if (ws) ws.close();
  }

  function cancelarResposta() {
    if (!chat.gerando) return;
    chat.erro = "cancelado";
    encerrarWs();
    renderResposta();
  }

  function renderResposta() {
    $("pergunta-atual").textContent = chat.pergunta ? "Você: " + chat.pergunta : "Pressione 1 para perguntar";
    var caixa = $("resposta");
    caixa.textContent = chat.texto;
    caixa.scrollTop = caixa.scrollHeight;

    var m = $("metricas-resposta");
    var ehErro = !!chat.erro && chat.erro !== "cancelado";
    m.classList.toggle("erro", ehErro);
    if (chat.erro) {
      m.textContent = ehErro ? "Erro: " + chat.erro : "cancelado";
    } else if (chat.metricas) {
      var f = chat.metricas;
      var partes = [];
      if (f.tokens_por_s) partes.push(fmt(f.tokens_por_s) + " tokens/s");
      partes.push(f.tokens + " tokens");
      partes.push(fmt(f.tempo_total_s, 2) + " s no total");
      m.textContent = partes.join(" · ");
    } else if (chat.gerando) {
      var decorrido = chat.primeiro ? (performance.now() - chat.primeiro) / 1000 : 0;
      if (decorrido > 0.2) {
        m.textContent = fmt(chat.tokens / decorrido) + " tokens/s · " + chat.tokens + " tokens";
      } else {
        m.textContent = chat.primeiro ? "" : "pensando…";
      }
    } else {
      m.textContent = "";
    }
  }

  $("entrada").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var texto = $("campo").value.trim();
    if (!texto || chat.gerando) return;
    fecharEntrada();
    perguntar(texto);
  });

  // ---------- eventos ao vivo (/events) ----------

  var avisoTimer = null;
  function avisar(texto) {
    var a = $("aviso");
    a.textContent = texto;
    a.hidden = false;
    clearTimeout(avisoTimer);
    avisoTimer = setTimeout(function () { a.hidden = true; }, 8000);
  }

  function marcarConexao(ok) { $("conexao").classList.toggle("on", ok); }

  function conectarEventos() {
    if (estado.sse) estado.sse.close();
    // EventSource não envia cabeçalho: o token vai na query string (ver docs/decisoes.md, seção 9).
    var es = new EventSource(url("/events?token=" + encodeURIComponent(estado.token)));
    estado.sse = es;
    es.onopen = function () { marcarConexao(true); };

    function ouvir(tipo, fn) {
      es.addEventListener(tipo, function (ev) {
        var corpo;
        try { corpo = JSON.parse(ev.data); } catch (e) { return; }
        fn(corpo.dados || {});
      });
    }

    ouvir("status", function (d) {
      definirMascote(d.mascote);
      $("tarefa").textContent = d.tarefa_atual || "";
    });
    ouvir("tecla", function (d) { if (d.acao === "down") aoTecla(d.tecla); });
    ouvir("nota_criada", carregarNotas);
    ouvir("nota_atualizada", carregarNotas);
    ouvir("nota_removida", carregarNotas);
    ouvir("agenda_criada", function () { carregarAgenda(); atualizarStatus(); });
    ouvir("agenda_atualizada", function () { carregarAgenda(); atualizarStatus(); });
    ouvir("agenda_removida", function () { carregarAgenda(); atualizarStatus(); });
    ouvir("lembrete_criado", atualizarStatus);
    ouvir("lembrete_atualizado", atualizarStatus);
    ouvir("lembrete_removido", atualizarStatus);
    ouvir("lembrete_disparado", function (d) {
      avisar("Lembrete: " + (d.texto || ""));
      atualizarStatus();
    });

    es.onerror = function () {
      marcarConexao(false);
      // Queda comum: o EventSource reconecta sozinho. CLOSED = o navegador desistiu (ex.: 401).
      if (es.readyState === EventSource.CLOSED) setTimeout(conectarEventos, 3000);
    };
  }

  // ---------- token na tela ----------

  function pedirToken() {
    $("login").hidden = false;
    setTimeout(function () { $("token").focus(); }, 0);
  }

  $("form-login").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var valor = $("token").value.trim();
    if (!valor) return;
    estado.token = valor;
    guardarToken(valor);
    $("token").value = "";
    $("login").hidden = true;
    iniciar();
  });

  // ---------- inicialização ----------

  function iniciar() {
    atualizarStatus();
    carregarAgenda();
    carregarNotas();
    if (estado.token) conectarEventos();
  }

  function iniciarTela() {
    document.body.classList.toggle("sim", SIM);
    $("controles").hidden = !SIM;

    document.querySelectorAll("[data-mascote]").forEach(function (el) { el.innerHTML = MASCOTE_SVG; });
    definirMascote("descansando");
    relogio();
    setInterval(relogio, 1000);

    var inicial = PARAMS.get("tela");
    irPara(TELAS.indexOf(inicial) >= 0 ? inicial : "inicio", true);
    renderResposta();

    setInterval(function () { if (estado.token) atualizarStatus(); }, 5000);

    // Gancho para os testes de tela (tests/e2e_tela.js): força um estado do mascote.
    window.jarvisTela = { mascote: definirMascote, tela: irPara };

    estado.token = lerToken();
    if (estado.token) iniciar(); else pedirToken();
  }

  iniciarTela();
})();
