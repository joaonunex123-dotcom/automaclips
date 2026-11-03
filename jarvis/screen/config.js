// Configuração da tela 800×480. Carregado antes de app.js.
// Ver docs/decisoes.md, seção 19.
window.JARVIS_CONFIG = {
  // URL do backend. Vazio = mesma origem (o próprio serviço serve esta tela).
  backend: "",
  // Fuso da casa: relógio e rótulos de dia. Igual ao JARVIS_TZ do backend.
  tz: "America/Sao_Paulo",
  // Token X-Jarvis-Token. Deixe vazio: a tela pede na primeira abertura e guarda
  // no navegador. Este arquivo é servido pela porta 8000 sem autenticação, então
  // não commite o token real aqui.
  token: "",
};
