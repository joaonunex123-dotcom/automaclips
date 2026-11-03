import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import '../core/models.dart';

/// ÚNICO arquivo que fala com o backend Jarvis (FastAPI, pela rede Tailscale).
///
/// Contrato assumido. O repositório não tinha o backend quando isto foi
/// escrito; ajuste os caminhos aqui se o backend real for diferente:
///
/// ```text
/// GET    /health                 200 {"ok": true}
/// GET    /notes                  [{id, title, body, updated_at}]
/// POST   /notes                  {title, body} -> nota
/// PUT    /notes/{id}             {title, body} -> nota
/// DELETE /notes/{id}
/// GET    /agenda                 [{id, title, start, end?, location?}]
/// POST   /agenda                 {title, start, end?, location?} -> evento
/// GET    /reminders              [{id, text, at, done}]
/// POST   /reminders              {text, at} -> lembrete
/// DELETE /reminders/{id}
/// GET    /events   (SSE)         data: {"state": "idle|listening|thinking|speaking|error", "detail": ""}
/// WS     /ws/chat
///          envia    {"type":"message","text":"..."}
///          recebe   {"type":"token","text":"..."}   (várias vezes)
///          recebe   {"type":"done","tokens":N}      (N opcional)
///          recebe   {"type":"error","message":"..."}
/// ```
///
/// Auth: "Authorization: Bearer TOKEN" em todas as chamadas, inclusive no WS.
/// Erros de HTTP com corpo {"detail": "..."} (padrão FastAPI) mostram o detail.
class JarvisApiException implements Exception {
  const JarvisApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class JarvisClient {
  JarvisClient({
    required String baseUrl,
    required this.token,
    http.Client? httpClient,
  })  : _base = Uri.parse(AppSettings.normalizeUrl(baseUrl)),
        _http = httpClient ?? http.Client();

  final Uri _base;
  final String token;
  final http.Client _http;

  static const _timeout = Duration(seconds: 15);
  static const _chatIdleTimeout = Duration(seconds: 90);

  Map<String, String> get _authHeaders => {
        'Authorization': 'Bearer $token',
        'Accept': 'application/json',
      };

  void _ensureConfigured() {
    if (_base.host.isEmpty) {
      throw const JarvisApiException(
          'Configure o endereço do Jarvis em Configurações.');
    }
  }

  Uri _uri(String path, [Map<String, String>? query]) {
    final prefix = _base.path.replaceAll(RegExp(r'/+$'), '');
    return _base.replace(
      path: '$prefix$path',
      queryParameters: query,
    );
  }

  Uri _wsUri(String path) {
    final uri = _uri(path);
    return uri.replace(scheme: uri.scheme == 'https' ? 'wss' : 'ws');
  }

  Future<dynamic> _request(String method, String path, {Object? body}) async {
    _ensureConfigured();
    final headers = {
      ..._authHeaders,
      if (body != null) 'Content-Type': 'application/json; charset=utf-8',
    };
    final encoded = body == null ? null : utf8.encode(jsonEncode(body));
    final uri = _uri(path);

    final Future<http.Response> pending = switch (method) {
      'GET' => _http.get(uri, headers: headers),
      'POST' => _http.post(uri, headers: headers, body: encoded),
      'PUT' => _http.put(uri, headers: headers, body: encoded),
      'DELETE' => _http.delete(uri, headers: headers),
      _ => throw ArgumentError.value(method, 'method'),
    };

    final http.Response res;
    try {
      res = await pending.timeout(_timeout);
    } on TimeoutException {
      throw const JarvisApiException('Tempo esgotado ao falar com o Jarvis.');
    } catch (e) {
      throw JarvisApiException('Não consegui falar com o Jarvis: $e');
    }

    if (res.statusCode == 401 || res.statusCode == 403) {
      throw JarvisApiException(
        'Token recusado pelo Jarvis (${res.statusCode}).',
        statusCode: res.statusCode,
      );
    }
    if (res.statusCode >= 400) {
      throw JarvisApiException(
        _errorDetail(res) ?? 'Erro HTTP ${res.statusCode} em $path.',
        statusCode: res.statusCode,
      );
    }
    if (res.bodyBytes.isEmpty) return null;
    return jsonDecode(utf8.decode(res.bodyBytes));
  }

  String? _errorDetail(http.Response res) {
    try {
      final data = jsonDecode(utf8.decode(res.bodyBytes));
      if (data is Map && data['detail'] is String) return data['detail'] as String;
    } catch (_) {}
    return null;
  }

  static List<T> _list<T>(
    Object? data,
    T Function(Map<String, dynamic>) parse,
  ) {
    if (data is! List) {
      throw const JarvisApiException('Resposta inesperada do Jarvis.');
    }
    return data.whereType<Map<String, dynamic>>().map(parse).toList();
  }

  static Map<String, dynamic> _map(Object? data) {
    if (data is Map<String, dynamic>) return data;
    throw const JarvisApiException('Resposta inesperada do Jarvis.');
  }

  // --- conexão ---------------------------------------------------------------

  /// Lança JarvisApiException se não conectar, se o token for recusado ou se
  /// o backend responder erro.
  Future<void> testConnection() async {
    await _request('GET', '/health');
  }

  // --- notas -----------------------------------------------------------------

  Future<List<Note>> listNotes() async =>
      _list(await _request('GET', '/notes'), Note.fromJson);

  Future<Note> createNote({required String title, required String body}) async =>
      Note.fromJson(
          _map(await _request('POST', '/notes', body: {'title': title, 'body': body})));

  Future<Note> updateNote(Note note) async => Note.fromJson(_map(await _request(
      'PUT', '/notes/${note.id}',
      body: {'title': note.title, 'body': note.body})));

  Future<void> deleteNote(String id) async {
    await _request('DELETE', '/notes/$id');
  }

  // --- agenda ----------------------------------------------------------------

  Future<List<AgendaEvent>> listAgenda() async {
    final events = _list(await _request('GET', '/agenda'), AgendaEvent.fromJson);
    events.sort((a, b) => a.start.compareTo(b.start));
    return events;
  }

  Future<AgendaEvent> createEvent({
    required String title,
    required DateTime start,
    DateTime? end,
    String? location,
  }) async {
    final data = await _request('POST', '/agenda', body: {
      'title': title,
      'start': start.toUtc().toIso8601String(),
      if (end != null) 'end': end.toUtc().toIso8601String(),
      if (location != null && location.isNotEmpty) 'location': location,
    });
    return AgendaEvent.fromJson(_map(data));
  }

  // --- lembretes -------------------------------------------------------------

  Future<List<Reminder>> listReminders() async =>
      _list(await _request('GET', '/reminders'), Reminder.fromJson);

  Future<Reminder> createReminder({
    required String text,
    required DateTime at,
  }) async {
    final data = await _request('POST', '/reminders', body: {
      'text': text,
      'at': at.toUtc().toIso8601String(),
    });
    return Reminder.fromJson(_map(data));
  }

  Future<void> deleteReminder(String id) async {
    await _request('DELETE', '/reminders/$id');
  }

  // --- estado (SSE /events) --------------------------------------------------

  /// Estado do Jarvis. Reconecta sozinho com backoff (1s, 2s, ... máx 30s) e
  /// emite JarvisEvent.offline enquanto estiver fora do ar. Cancelar a
  /// assinatura fecha a conexão SSE na hora.
  Stream<JarvisEvent> events() {
    final controller = StreamController<JarvisEvent>();
    var stopped = false;
    http.Client? sse;
    Timer? retry;
    Completer<void>? wake;

    void emit(JarvisEvent event) {
      if (!stopped) controller.add(event);
    }

    controller.onCancel = () {
      stopped = true;
      retry?.cancel();
      wake?.complete();
      sse?.close();
    };

    Future<void> run() async {
      var backoff = const Duration(seconds: 1);
      while (!stopped) {
        sse = http.Client();
        try {
          _ensureConfigured();
          final req = http.Request('GET', _uri('/events'))
            ..headers.addAll({..._authHeaders, 'Accept': 'text/event-stream'});
          final res = await sse!.send(req).timeout(_timeout);
          if (res.statusCode >= 400) {
            throw JarvisApiException('SSE HTTP ${res.statusCode}',
                statusCode: res.statusCode);
          }
          backoff = const Duration(seconds: 1);
          final lines = res.stream
              .transform(utf8.decoder)
              .transform(const LineSplitter());
          await for (final line in lines) {
            if (stopped) break;
            if (!line.startsWith('data:')) continue;
            final payload = line.substring(5).trim();
            if (payload.isEmpty) continue;
            try {
              final data = jsonDecode(payload);
              if (data is Map<String, dynamic>) {
                emit(JarvisEvent.fromJson(data));
              }
            } on FormatException {
              // evento malformado: ignora
            }
          }
        } catch (_) {
          // cai para offline e tenta de novo
        } finally {
          sse?.close();
          sse = null;
        }
        if (stopped) break;

        emit(JarvisEvent.offline);
        wake = Completer<void>();
        retry = Timer(backoff, () => wake?.complete());
        await wake!.future;
        retry = null;
        wake = null;
        if (backoff < const Duration(seconds: 30)) backoff *= 2;
      }
      await controller.close();
    }

    unawaited(run());
    return controller.stream;
  }

  // --- chat (WebSocket /ws/chat) ---------------------------------------------

  /// Envia [text] e emite os tokens conforme chegam. Termina com ChatDone.
  Stream<ChatChunk> chat(String text) async* {
    _ensureConfigured();
    final WebSocket socket;
    try {
      socket = await WebSocket.connect(_wsUri('/ws/chat').toString(),
          headers: _authHeaders).timeout(_timeout);
    } on TimeoutException {
      throw const JarvisApiException('Tempo esgotado ao abrir o chat.');
    } catch (_) {
      throw const JarvisApiException(
          'Não consegui abrir o chat (confira o token e a conexão).');
    }

    try {
      socket.add(jsonEncode({'type': 'message', 'text': text}));
      final frames = socket.timeout(_chatIdleTimeout);
      await for (final raw in frames) {
        if (raw is! String) continue;
        final frame = jsonDecode(raw);
        if (frame is! Map<String, dynamic>) continue;
        switch (frame['type']) {
          case 'token':
            yield ChatToken('${frame['text'] ?? ''}');
          case 'done':
            final tokens = frame['tokens'];
            yield ChatDone(tokens: tokens is int ? tokens : null);
            return;
          case 'error':
            throw JarvisApiException('${frame['message'] ?? 'erro no chat'}');
        }
      }
      throw const JarvisApiException(
          'Conexão do chat encerrada antes do fim da resposta.');
    } on TimeoutException {
      throw const JarvisApiException('O Jarvis demorou demais para responder.');
    } on JarvisApiException {
      rethrow;
    } catch (e) {
      throw JarvisApiException('Falha no chat: $e');
    } finally {
      try {
        await socket.close();
      } catch (_) {}
    }
  }

  void close() => _http.close();
}
