import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// Mock HTTP do backend Jarvis para os testes: REST em memória, SSE em /events
/// e WebSocket em /ws/chat. Escuta em 127.0.0.1 numa porta livre.
class MockJarvisServer {
  MockJarvisServer({this.token = 'segredo'});

  final String token;

  HttpServer? _server;
  final List<Map<String, dynamic>> notes = [];
  final List<Map<String, dynamic>> agenda = [];
  final List<Map<String, dynamic>> reminders = [];
  final List<HttpResponse> _sseClients = [];
  int _nextId = 1;

  /// Pedaços que o chat responde, um por frame "token".
  List<String> chatReply = const ['Olá', ', ', 'tudo', ' bem', '?'];

  /// Atraso entre tokens, para dar tempo mensurável de tokens/s.
  Duration chatDelay = const Duration(milliseconds: 25);

  /// Se definido, vai no frame "done" como contagem do servidor.
  int? doneTokens;

  /// Se definido, o chat responde com um frame de erro em vez de tokens.
  String? chatError;

  String? lastChatText;
  final List<String> requests = [];

  Uri get url => Uri.parse('http://127.0.0.1:${_server!.port}');

  Future<void> start() async {
    _server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    _server!.listen(_handle);
  }

  Future<void> close() async {
    for (final client in List.of(_sseClients)) {
      try {
        await client.close();
      } catch (_) {}
    }
    _sseClients.clear();
    await _server?.close(force: true);
  }

  /// Espera até algum cliente estar conectado ao SSE /events.
  Future<void> waitForSseClient() async {
    while (_sseClients.isEmpty) {
      await Future<void>.delayed(const Duration(milliseconds: 5));
    }
  }

  /// Manda um evento de estado para todos os clientes do SSE.
  void pushEvent(Map<String, dynamic> data) {
    for (final client in List.of(_sseClients)) {
      try {
        client.write('data: ${jsonEncode(data)}\n\n');
      } catch (_) {
        _sseClients.remove(client);
      }
    }
  }

  /// Derruba as conexões SSE (simula queda do Pi).
  Future<void> dropSse() async {
    for (final client in List.of(_sseClients)) {
      await client.close();
    }
    _sseClients.clear();
  }

  Future<void> _handle(HttpRequest req) async {
    requests.add('${req.method} ${req.uri.path}');

    if (req.uri.path == '/ws/chat' && WebSocketTransformer.isUpgradeRequest(req)) {
      return _chat(req);
    }
    if (req.headers.value(HttpHeaders.authorizationHeader) != 'Bearer $token') {
      return _json(req, 401, {'detail': 'token inválido'});
    }

    final segments = req.uri.pathSegments;
    final first = segments.isEmpty ? '' : segments.first;
    final id = segments.length > 1 ? segments[1] : null;

    switch ((req.method, first, id)) {
      case ('GET', 'health', null):
        return _json(req, 200, {'ok': true});
      case ('GET', 'notes', null):
        return _json(req, 200, notes);
      case ('POST', 'notes', null):
        return _create(req, notes, ['title', 'body']);
      case ('PUT', 'notes', final String noteId):
        return _update(req, notes, noteId, ['title', 'body']);
      case ('DELETE', 'notes', final String noteId):
        return _delete(req, notes, noteId);
      case ('GET', 'agenda', null):
        return _json(req, 200, agenda);
      case ('POST', 'agenda', null):
        return _create(req, agenda, ['title', 'start', 'end', 'location']);
      case ('GET', 'reminders', null):
        return _json(req, 200, reminders);
      case ('POST', 'reminders', null):
        return _create(req, reminders, ['text', 'at']);
      case ('DELETE', 'reminders', final String rid):
        return _delete(req, reminders, rid);
      case ('GET', 'events', null):
        return _sse(req);
      default:
        return _json(req, 404, {'detail': 'não encontrado'});
    }
  }

  Future<Map<String, dynamic>> _body(HttpRequest req) async {
    final text = await utf8.decoder.bind(req).join();
    return jsonDecode(text) as Map<String, dynamic>;
  }

  Future<void> _create(
    HttpRequest req,
    List<Map<String, dynamic>> store,
    List<String> fields,
  ) async {
    final body = await _body(req);
    final item = <String, dynamic>{
      'id': '${_nextId++}',
      'updated_at': DateTime.now().toUtc().toIso8601String(),
      for (final f in fields)
        if (body.containsKey(f)) f: body[f],
    };
    if (store == reminders) item['done'] = false;
    store.add(item);
    return _json(req, 201, item);
  }

  Future<void> _update(
    HttpRequest req,
    List<Map<String, dynamic>> store,
    String id,
    List<String> fields,
  ) async {
    final body = await _body(req);
    final index = store.indexWhere((e) => e['id'] == id);
    if (index < 0) return _json(req, 404, {'detail': 'não existe'});
    for (final f in fields) {
      if (body.containsKey(f)) store[index][f] = body[f];
    }
    return _json(req, 200, store[index]);
  }

  Future<void> _delete(
    HttpRequest req,
    List<Map<String, dynamic>> store,
    String id,
  ) async {
    store.removeWhere((e) => e['id'] == id);
    req.response.statusCode = 204;
    await req.response.close();
  }

  Future<void> _json(HttpRequest req, int status, Object? body) async {
    req.response
      ..statusCode = status
      ..headers.contentType = ContentType.json
      ..write(jsonEncode(body));
    await req.response.close();
  }

  Future<void> _sse(HttpRequest req) async {
    final res = req.response
      ..statusCode = 200
      ..headers.contentType = ContentType('text', 'event-stream', charset: 'utf-8')
      ..bufferOutput = false;
    _sseClients.add(res);
    res.write('data: ${jsonEncode({'state': 'idle'})}\n\n');
  }

  Future<void> _chat(HttpRequest req) async {
    if (req.headers.value(HttpHeaders.authorizationHeader) != 'Bearer $token') {
      req.response.statusCode = 401;
      await req.response.close();
      return;
    }
    final ws = await WebSocketTransformer.upgrade(req);
    ws.listen((raw) async {
      final msg = jsonDecode(raw as String) as Map<String, dynamic>;
      lastChatText = msg['text'] as String?;
      if (chatError != null) {
        ws.add(jsonEncode({'type': 'error', 'message': chatError}));
        return;
      }
      for (final piece in chatReply) {
        ws.add(jsonEncode({'type': 'token', 'text': piece}));
        await Future<void>.delayed(chatDelay);
      }
      ws.add(jsonEncode({
        'type': 'done',
        if (doneTokens != null) 'tokens': doneTokens,
      }));
    });
  }
}
