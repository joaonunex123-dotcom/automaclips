/// Modelos do app. Só dados e parsing; nada de rede aqui.
library;

DateTime? _parseDate(Object? value) {
  if (value is! String || value.isEmpty) return null;
  return DateTime.tryParse(value)?.toLocal();
}

String _str(Object? value) => value == null ? '' : '$value';

class Note {
  const Note({
    required this.id,
    required this.title,
    required this.body,
    this.updatedAt,
  });

  final String id;
  final String title;
  final String body;
  final DateTime? updatedAt;

  factory Note.fromJson(Map<String, dynamic> json) => Note(
        id: _str(json['id']),
        title: _str(json['title']),
        body: _str(json['body']),
        updatedAt: _parseDate(json['updated_at']),
      );

  Note copyWith({String? title, String? body}) => Note(
        id: id,
        title: title ?? this.title,
        body: body ?? this.body,
        updatedAt: updatedAt,
      );
}

class AgendaEvent {
  const AgendaEvent({
    required this.id,
    required this.title,
    required this.start,
    this.end,
    this.location,
  });

  final String id;
  final String title;
  final DateTime start;
  final DateTime? end;
  final String? location;

  factory AgendaEvent.fromJson(Map<String, dynamic> json) => AgendaEvent(
        id: _str(json['id']),
        title: _str(json['title']),
        start: _parseDate(json['start']) ?? DateTime.fromMillisecondsSinceEpoch(0),
        end: _parseDate(json['end']),
        location: json['location'] is String && (json['location'] as String).isNotEmpty
            ? json['location'] as String
            : null,
      );
}

class Reminder {
  const Reminder({
    required this.id,
    required this.text,
    required this.at,
    this.done = false,
  });

  final String id;
  final String text;
  final DateTime at;
  final bool done;

  factory Reminder.fromJson(Map<String, dynamic> json) => Reminder(
        id: _str(json['id']),
        text: _str(json['text']),
        at: _parseDate(json['at']) ?? DateTime.fromMillisecondsSinceEpoch(0),
        done: json['done'] == true,
      );
}

enum ChatRole { user, assistant }

class ChatMessage {
  const ChatMessage({
    required this.id,
    required this.role,
    required this.text,
    this.tokens,
    this.tokensPerSecond,
    this.streaming = false,
    this.failed = false,
  });

  final String id;
  final ChatRole role;
  final String text;
  final int? tokens;
  final double? tokensPerSecond;
  final bool streaming;
  final bool failed;

  ChatMessage copyWith({
    String? text,
    int? tokens,
    double? tokensPerSecond,
    bool? streaming,
    bool? failed,
  }) =>
      ChatMessage(
        id: id,
        role: role,
        text: text ?? this.text,
        tokens: tokens ?? this.tokens,
        tokensPerSecond: tokensPerSecond ?? this.tokensPerSecond,
        streaming: streaming ?? this.streaming,
        failed: failed ?? this.failed,
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'role': role.name,
        'text': text,
        if (tokens != null) 'tokens': tokens,
        if (tokensPerSecond != null) 'tps': tokensPerSecond,
        if (failed) 'failed': true,
      };

  factory ChatMessage.fromJson(Map<String, dynamic> json) => ChatMessage(
        id: _str(json['id']),
        role: json['role'] == 'user' ? ChatRole.user : ChatRole.assistant,
        text: _str(json['text']),
        tokens: json['tokens'] is int ? json['tokens'] as int : null,
        tokensPerSecond:
            json['tps'] is num ? (json['tps'] as num).toDouble() : null,
        failed: json['failed'] == true,
      );
}

/// Estado do Jarvis, vindo do SSE /events (mesmo que a tela física mostra).
class JarvisEvent {
  const JarvisEvent({required this.state, this.detail = ''});

  static const connecting = JarvisEvent(state: 'connecting');
  static const offline = JarvisEvent(state: 'offline');

  final String state;
  final String detail;

  factory JarvisEvent.fromJson(Map<String, dynamic> json) {
    final state = _str(json['state']);
    return JarvisEvent(
      state: state.isEmpty ? 'idle' : state,
      detail: _str(json['detail']),
    );
  }
}

/// Pedaço de uma resposta do chat (frames do WebSocket /ws/chat).
sealed class ChatChunk {
  const ChatChunk();
}

final class ChatToken extends ChatChunk {
  const ChatToken(this.text);
  final String text;
}

final class ChatDone extends ChatChunk {
  const ChatDone({this.tokens});
  final int? tokens;
}

class AppSettings {
  const AppSettings({this.baseUrl = '', this.token = ''});

  final String baseUrl;
  final String token;

  bool get isConfigured => baseUrl.isNotEmpty;

  /// Aceita "jarvis:8000", "http://jarvis:8000/" etc. e devolve sem barra final.
  static String normalizeUrl(String raw) {
    var value = raw.trim();
    if (value.isEmpty) return '';
    if (!value.contains('://')) value = 'http://$value';
    while (value.endsWith('/')) {
      value = value.substring(0, value.length - 1);
    }
    return value;
  }
}
