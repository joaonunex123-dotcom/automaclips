import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/jarvis_client.dart';
import '../core/models.dart';
import '../core/stream_metrics.dart';

/// Inicializado no main() e sobrescrito nos testes.
final sharedPrefsProvider = Provider<SharedPreferences>(
  (ref) => throw UnimplementedError('sharedPrefsProvider não foi sobrescrito'),
);

// --- configurações ---------------------------------------------------------

class SettingsNotifier extends Notifier<AppSettings> {
  static const _kUrl = 'jarvis.baseUrl';
  static const _kToken = 'jarvis.token';

  @override
  AppSettings build() {
    final prefs = ref.watch(sharedPrefsProvider);
    return AppSettings(
      baseUrl: prefs.getString(_kUrl) ?? '',
      token: prefs.getString(_kToken) ?? '',
    );
  }

  Future<void> save({required String baseUrl, required String token}) async {
    final next = AppSettings(
      baseUrl: AppSettings.normalizeUrl(baseUrl),
      token: token.trim(),
    );
    final prefs = ref.read(sharedPrefsProvider);
    await prefs.setString(_kUrl, next.baseUrl);
    await prefs.setString(_kToken, next.token);
    state = next;
  }
}

final settingsProvider =
    NotifierProvider<SettingsNotifier, AppSettings>(SettingsNotifier.new);

/// Fábrica separada para o botão "Testar conexão" usar valores ainda não salvos.
final jarvisClientFactoryProvider = Provider<JarvisClient Function(String, String)>(
  (ref) => (baseUrl, token) => JarvisClient(baseUrl: baseUrl, token: token),
);

final apiClientProvider = Provider<JarvisClient>((ref) {
  final settings = ref.watch(settingsProvider);
  final client = JarvisClient(baseUrl: settings.baseUrl, token: settings.token);
  ref.onDispose(client.close);
  return client;
});

// --- estado do Jarvis (SSE) ------------------------------------------------

final jarvisEventsProvider = StreamProvider<JarvisEvent>(
  (ref) => ref.watch(apiClientProvider).events(),
);

// --- chat ------------------------------------------------------------------

class ChatState {
  const ChatState({this.messages = const [], this.busy = false});

  final List<ChatMessage> messages;
  final bool busy;

  ChatState copyWith({List<ChatMessage>? messages, bool? busy}) => ChatState(
        messages: messages ?? this.messages,
        busy: busy ?? this.busy,
      );
}

class ChatNotifier extends Notifier<ChatState> {
  static const storageKey = 'jarvis.chat.history';
  static const maxStored = 200;

  int _seq = 0;

  @override
  ChatState build() => ChatState(messages: _load(ref.watch(sharedPrefsProvider)));

  static List<ChatMessage> _load(SharedPreferences prefs) {
    final raw = prefs.getString(storageKey);
    if (raw == null) return const [];
    try {
      final list = jsonDecode(raw) as List;
      return list
          .whereType<Map<String, dynamic>>()
          .map(ChatMessage.fromJson)
          .toList();
    } catch (_) {
      return const [];
    }
  }

  void _persist() {
    final finished = state.messages.where((m) => !m.streaming).toList();
    final kept = finished.length > maxStored
        ? finished.sublist(finished.length - maxStored)
        : finished;
    ref
        .read(sharedPrefsProvider)
        .setString(storageKey, jsonEncode(kept.map((m) => m.toJson()).toList()));
  }

  String _nextId(String prefix) => '$prefix${DateTime.now().microsecondsSinceEpoch}-${_seq++}';

  void _replace(String id, ChatMessage Function(ChatMessage) update) {
    state = state.copyWith(
      messages: [for (final m in state.messages) m.id == id ? update(m) : m],
    );
  }

  /// Envia [input] ao Jarvis e vai preenchendo a resposta conforme os tokens chegam.
  Future<void> send(String input) async {
    final text = input.trim();
    if (text.isEmpty || state.busy) return;

    final botId = _nextId('a');
    state = ChatState(
      busy: true,
      messages: [
        ...state.messages,
        ChatMessage(id: _nextId('u'), role: ChatRole.user, text: text),
        ChatMessage(
          id: botId,
          role: ChatRole.assistant,
          text: '',
          streaming: true,
        ),
      ],
    );

    final metrics = StreamMetrics();
    final buffer = StringBuffer();
    try {
      await for (final chunk in ref.read(apiClientProvider).chat(text)) {
        switch (chunk) {
          case ChatToken(text: final piece):
            metrics.onToken();
            buffer.write(piece);
            _replace(botId, (m) => m.copyWith(text: buffer.toString()));
          case ChatDone(tokens: final serverTokens):
            final r = metrics.finish(serverTokens: serverTokens);
            _replace(
              botId,
              (m) => m.copyWith(
                streaming: false,
                tokens: r.tokens,
                tokensPerSecond: r.tokensPerSecond,
              ),
            );
        }
      }
      _replace(botId, (m) => m.copyWith(streaming: false));
    } catch (e) {
      _replace(
        botId,
        (m) => m.copyWith(
          streaming: false,
          failed: true,
          text: buffer.isEmpty ? '$e' : buffer.toString(),
        ),
      );
    } finally {
      state = state.copyWith(busy: false);
      _persist();
    }
  }

  void clearHistory() {
    state = const ChatState();
    _persist();
  }
}

final chatProvider =
    NotifierProvider<ChatNotifier, ChatState>(ChatNotifier.new);

// --- notas, agenda, lembretes ----------------------------------------------

class NotesNotifier extends AsyncNotifier<List<Note>> {
  @override
  Future<List<Note>> build() => ref.watch(apiClientProvider).listNotes();

  Future<void> create({required String title, required String body}) async {
    await ref.read(apiClientProvider).createNote(title: title, body: body);
    ref.invalidateSelf();
  }

  Future<void> edit(Note note) async {
    await ref.read(apiClientProvider).updateNote(note);
    ref.invalidateSelf();
  }

  Future<void> remove(String id) async {
    await ref.read(apiClientProvider).deleteNote(id);
    ref.invalidateSelf();
  }
}

final notesProvider =
    AsyncNotifierProvider<NotesNotifier, List<Note>>(NotesNotifier.new);

class AgendaNotifier extends AsyncNotifier<List<AgendaEvent>> {
  @override
  Future<List<AgendaEvent>> build() => ref.watch(apiClientProvider).listAgenda();

  Future<void> create({
    required String title,
    required DateTime start,
    String? location,
  }) async {
    await ref.read(apiClientProvider).createEvent(
          title: title,
          start: start,
          location: location,
        );
    ref.invalidateSelf();
  }
}

final agendaProvider =
    AsyncNotifierProvider<AgendaNotifier, List<AgendaEvent>>(AgendaNotifier.new);

class RemindersNotifier extends AsyncNotifier<List<Reminder>> {
  @override
  Future<List<Reminder>> build() async {
    final list = await ref.watch(apiClientProvider).listReminders();
    list.sort((a, b) => a.at.compareTo(b.at));
    return list;
  }

  Future<void> create({required String text, required DateTime at}) async {
    await ref.read(apiClientProvider).createReminder(text: text, at: at);
    ref.invalidateSelf();
  }

  Future<void> remove(String id) async {
    await ref.read(apiClientProvider).deleteReminder(id);
    ref.invalidateSelf();
  }
}

final remindersProvider =
    AsyncNotifierProvider<RemindersNotifier, List<Reminder>>(RemindersNotifier.new);
