import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/api/jarvis_client.dart';
import 'package:jarvis_app/core/models.dart';
import 'package:jarvis_app/core/theme.dart';
import 'package:jarvis_app/screens/chat_tab.dart';
import 'package:jarvis_app/state/providers.dart';
import 'package:jarvis_app/state/speech_input.dart';
import 'package:jarvis_app/widgets/chat_bubble.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../support/fakes.dart';

Future<void> pumpChat(
  WidgetTester tester, {
  required FakeJarvisClient client,
  FakeSpeechInput? speech,
}) async {
  SharedPreferences.setMockInitialValues({
    'jarvis.baseUrl': 'http://jarvis:8000',
    'jarvis.token': 'segredo',
  });
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(ProviderScope(
    overrides: [
      sharedPrefsProvider.overrideWithValue(prefs),
      apiClientProvider.overrideWithValue(client),
      speechInputProvider.overrideWithValue(speech ?? FakeSpeechInput()),
    ],
    child: MaterialApp(
      theme: buildJarvisTheme(),
      home: const Scaffold(body: ChatTab()),
    ),
  ));
}

void main() {
  testWidgets('mostra tokens e tok/s embaixo da resposta', (tester) async {
    await tester.pumpWidget(MaterialApp(
      theme: buildJarvisTheme(),
      home: const Scaffold(
        body: ChatBubble(
          message: ChatMessage(
            id: 'a1',
            role: ChatRole.assistant,
            text: 'Olá **mundo**',
            tokens: 5,
            tokensPerSecond: 12.34,
          ),
        ),
      ),
    ));

    expect(find.text('5 tokens · 12.3 tok/s'), findsOneWidget);
    expect(find.byType(MarkdownBody), findsOneWidget);
  });

  testWidgets('envia o texto digitado e mostra a resposta em streaming',
      (tester) async {
    final client = FakeJarvisClient(chatChunks: const [
      ChatToken('Olá'),
      ChatToken(', tudo'),
      ChatDone(tokens: 2),
    ]);
    await pumpChat(tester, client: client);

    await tester.enterText(find.byType(TextField), 'oi');
    await tester.tap(find.byTooltip('Enviar'));
    await tester.pumpAndSettle();

    expect(client.sentMessages, ['oi']);
    expect(find.text('oi'), findsOneWidget);
    expect(
      tester.widget<MarkdownBody>(find.byType(MarkdownBody)).data,
      'Olá, tudo',
    );
    expect(find.text('2 tokens'), findsOneWidget);
  });

  testWidgets('segurar pra falar transcreve e envia o texto', (tester) async {
    final client = FakeJarvisClient(chatChunks: const [
      ChatToken('Certo'),
      ChatDone(tokens: 1),
    ]);
    final speech = FakeSpeechInput(partial: 'oi', finalText: 'oi jarvis');
    await pumpChat(tester, client: client, speech: speech);

    final mic = find.byKey(const Key('hold-to-talk'));
    final gesture = await tester.startGesture(tester.getCenter(mic));
    await tester.pump(const Duration(milliseconds: 600));
    await tester.pump();

    expect(speech.started, isTrue);
    expect(find.text('oi'), findsOneWidget); // transcrição parcial aparece

    await gesture.up();
    await tester.pumpAndSettle();

    expect(speech.started, isFalse);
    expect(client.sentMessages, ['oi jarvis']);
    expect(find.text('oi jarvis'), findsOneWidget);
  });

  testWidgets('sem reconhecimento de voz avisa em vez de ouvir', (tester) async {
    final client = FakeJarvisClient();
    final speech = FakeSpeechInput(available: false);
    await pumpChat(tester, client: client, speech: speech);

    final gesture = await tester
        .startGesture(tester.getCenter(find.byKey(const Key('hold-to-talk'))));
    await tester.pump(const Duration(milliseconds: 600));
    await gesture.up();
    await tester.pumpAndSettle();

    expect(speech.started, isFalse);
    expect(client.sentMessages, isEmpty);
    expect(find.textContaining('Reconhecimento de voz indisponível'), findsOneWidget);
  });

  testWidgets('erro do chat aparece marcado como falha', (tester) async {
    await pumpChat(tester, client: _ThrowingClient());

    await tester.enterText(find.byType(TextField), 'teste');
    await tester.tap(find.byTooltip('Enviar'));
    await tester.pumpAndSettle();

    expect(find.text('falhou'), findsOneWidget);
    expect(find.textContaining('Token recusado'), findsOneWidget);
  });
}

class _ThrowingClient extends FakeJarvisClient {
  @override
  Stream<ChatChunk> chat(String text) async* {
    throw const JarvisApiException('Token recusado pelo Jarvis (401).', statusCode: 401);
  }
}
