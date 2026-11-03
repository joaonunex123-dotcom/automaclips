import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/api/jarvis_client.dart';
import 'package:jarvis_app/core/models.dart';

import '../support/mock_jarvis_server.dart';

Future<void> waitUntil(bool Function() condition,
    {Duration timeout = const Duration(seconds: 5)}) async {
  final end = DateTime.now().add(timeout);
  while (!condition()) {
    if (DateTime.now().isAfter(end)) fail('timeout esperando condição');
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}

void main() {
  late MockJarvisServer server;
  late JarvisClient client;

  setUp(() async {
    server = MockJarvisServer(token: 'segredo');
    await server.start();
    client = JarvisClient(baseUrl: server.url.toString(), token: 'segredo');
  });

  tearDown(() async {
    client.close();
    await server.close();
  });

  group('conexão', () {
    test('testConnection passa com o token certo', () async {
      await client.testConnection();
      expect(server.requests, contains('GET /health'));
    });

    test('token recusado vira JarvisApiException com status 401', () async {
      final bad = JarvisClient(baseUrl: server.url.toString(), token: 'errado');
      addTearDown(bad.close);
      await expectLater(
        bad.testConnection(),
        throwsA(isA<JarvisApiException>()
            .having((e) => e.statusCode, 'statusCode', 401)
            .having((e) => e.message, 'message', contains('Token recusado'))),
      );
    });

    test('servidor fora do ar vira JarvisApiException legível', () async {
      final dead = JarvisClient(baseUrl: 'http://127.0.0.1:1', token: 'x');
      addTearDown(dead.close);
      await expectLater(
        dead.testConnection(),
        throwsA(isA<JarvisApiException>()
            .having((e) => e.message, 'message', contains('Não consegui falar'))),
      );
    });

    test('sem endereço configurado avisa para configurar', () async {
      final empty = JarvisClient(baseUrl: '', token: '');
      addTearDown(empty.close);
      await expectLater(
        empty.listNotes(),
        throwsA(isA<JarvisApiException>()
            .having((e) => e.message, 'message', contains('Configure'))),
      );
    });

    test('aceita URL com barra final e prefixo de caminho', () async {
      final withSlash =
          JarvisClient(baseUrl: '${server.url}/', token: 'segredo');
      addTearDown(withSlash.close);
      await withSlash.testConnection();
      expect(server.requests, contains('GET /health'));
    });
  });

  group('notas', () {
    test('criar, listar, editar e apagar, com acentos', () async {
      final note = await client.createNote(
          title: 'Reunião às 10h', body: 'levar o relatório');
      expect(note.id, isNotEmpty);
      expect(note.title, 'Reunião às 10h');

      final listed = await client.listNotes();
      expect(listed.map((n) => n.title), ['Reunião às 10h']);

      final edited = await client.updateNote(note.copyWith(body: 'ação: revisar'));
      expect(edited.body, 'ação: revisar');
      expect((await client.listNotes()).single.body, 'ação: revisar');

      await client.deleteNote(note.id);
      expect(await client.listNotes(), isEmpty);
    });

    test('erro 404 mostra o detail do FastAPI', () async {
      await expectLater(
        client.updateNote(const Note(id: '999', title: 't', body: 'b')),
        throwsA(isA<JarvisApiException>()
            .having((e) => e.statusCode, 'statusCode', 404)
            .having((e) => e.message, 'message', 'não existe')),
      );
    });
  });

  group('agenda', () {
    test('criar evento e listar em ordem de início', () async {
      await client.createEvent(title: 'Tarde', start: DateTime(2030, 1, 2, 15));
      await client.createEvent(
        title: 'Manhã',
        start: DateTime(2030, 1, 2, 9),
        location: 'Casa',
      );

      final events = await client.listAgenda();
      expect(events.map((e) => e.title), ['Manhã', 'Tarde']);
      expect(events.first.start, DateTime(2030, 1, 2, 9));
      expect(events.first.location, 'Casa');
      expect(events.last.location, isNull);
    });
  });

  group('lembretes', () {
    test('criar com horário e apagar', () async {
      final at = DateTime(2030, 5, 10, 7, 30);
      final created = await client.createReminder(text: 'Tomar remédio', at: at);
      expect(created.at, at);
      expect(created.done, isFalse);

      final listed = await client.listReminders();
      expect(listed.single.text, 'Tomar remédio');

      await client.deleteReminder(created.id);
      expect(await client.listReminders(), isEmpty);
    });
  });

  group('estado (SSE /events)', () {
    test('emite os estados que o servidor manda', () async {
      final states = <String>[];
      final sub = client.events().listen((e) => states.add(e.state));

      await server.waitForSseClient();
      server.pushEvent({'state': 'listening'});
      server.pushEvent({'state': 'thinking', 'detail': 'pensando'});
      await waitUntil(() => states.length >= 3);
      await sub.cancel();

      expect(states.take(3).toList(), ['idle', 'listening', 'thinking']);
    });

    test('marca offline ao cair e reconecta sozinho', () async {
      final states = <String>[];
      final sub = client.events().listen((e) => states.add(e.state));

      await server.waitForSseClient();
      await server.dropSse();
      await waitUntil(() => states.contains('offline'));

      await server.waitForSseClient();
      await waitUntil(() => states.where((s) => s == 'idle').length >= 2);
      await sub.cancel();
    });
  });

  group('chat (WebSocket /ws/chat)', () {
    test('recebe tokens em streaming e termina com done', () async {
      final chunks = await client.chat('oi').toList();

      final text = chunks.whereType<ChatToken>().map((t) => t.text).join();
      expect(text, 'Olá, tudo bem?');
      expect(chunks.last, isA<ChatDone>());
      expect((chunks.last as ChatDone).tokens, isNull);
      expect(server.lastChatText, 'oi');
    });

    test('usa a contagem de tokens enviada pelo servidor', () async {
      server.doneTokens = 42;
      final chunks = await client.chat('oi').toList();
      expect((chunks.last as ChatDone).tokens, 42);
    });

    test('frame de erro vira JarvisApiException com a mensagem', () async {
      server.chatError = 'modelo indisponível';
      await expectLater(
        client.chat('oi').toList(),
        throwsA(isA<JarvisApiException>()
            .having((e) => e.message, 'message', 'modelo indisponível')),
      );
    });

    test('token recusado no WebSocket vira JarvisApiException', () async {
      final bad = JarvisClient(baseUrl: server.url.toString(), token: 'errado');
      addTearDown(bad.close);
      await expectLater(
        bad.chat('oi').toList(),
        throwsA(isA<JarvisApiException>()),
      );
    });
  });
}
