import 'dart:async';

import 'package:jarvis_app/api/jarvis_client.dart';
import 'package:jarvis_app/core/models.dart';
import 'package:jarvis_app/state/speech_input.dart';

/// Cliente falso para testes de widget: sem rede.
class FakeJarvisClient extends JarvisClient {
  FakeJarvisClient({
    this.healthError,
    this.chatChunks = const [],
    this.eventStream,
  }) : super(baseUrl: '', token: '');

  Object? healthError;
  List<ChatChunk> chatChunks;
  Stream<JarvisEvent> Function()? eventStream;
  final List<String> sentMessages = [];

  @override
  Future<void> testConnection() async {
    if (healthError != null) throw healthError!;
  }

  @override
  Stream<JarvisEvent> events() => eventStream?.call() ?? const Stream.empty();

  @override
  Stream<ChatChunk> chat(String text) {
    sentMessages.add(text);
    return Stream.fromIterable(chatChunks);
  }

  @override
  Future<List<Note>> listNotes() async => const [];

  @override
  Future<List<AgendaEvent>> listAgenda() async => const [];

  @override
  Future<List<Reminder>> listReminders() async => const [];

  @override
  void close() {}
}

/// Reconhecimento de voz falso: devolve o texto pré-definido.
class FakeSpeechInput implements SpeechInput {
  FakeSpeechInput({this.available = true, this.partial = 'oi', this.finalText = 'oi jarvis'});

  final bool available;
  final String partial;
  final String finalText;
  bool started = false;

  @override
  Future<bool> init() async => available;

  @override
  Future<void> start({required void Function(String partial) onPartial}) async {
    started = true;
    onPartial(partial);
  }

  @override
  Future<String> stop() async {
    started = false;
    return finalText;
  }
}
