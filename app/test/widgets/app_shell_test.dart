import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/core/models.dart';
import 'package:jarvis_app/main.dart';
import 'package:jarvis_app/state/providers.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../support/fakes.dart';

void main() {
  testWidgets('topo mostra o estado do SSE e as quatro abas existem', (tester) async {
    SharedPreferences.setMockInitialValues({
      'jarvis.baseUrl': 'http://jarvis:8000',
      'jarvis.token': 'segredo',
    });
    final prefs = await SharedPreferences.getInstance();
    final client = FakeJarvisClient(
      eventStream: () => Stream.fromIterable(const [
        JarvisEvent(state: 'listening', detail: 'ouvindo você'),
      ]),
    );

    await tester.pumpWidget(ProviderScope(
      overrides: [
        sharedPrefsProvider.overrideWithValue(prefs),
        apiClientProvider.overrideWithValue(client),
      ],
      child: const JarvisApp(),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Jarvis · ouvindo · ouvindo você'), findsOneWidget);
    expect(find.text('Chat'), findsOneWidget);
    expect(find.text('Notas'), findsOneWidget);
    expect(find.text('Agenda'), findsOneWidget);
    expect(find.text('Lembretes'), findsOneWidget);
  });

  testWidgets('sem endereço configurado abre direto as configurações', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();

    await tester.pumpWidget(ProviderScope(
      overrides: [
        sharedPrefsProvider.overrideWithValue(prefs),
        apiClientProvider.overrideWithValue(FakeJarvisClient()),
      ],
      child: const JarvisApp(),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Configurações'), findsWidgets);
    expect(find.byKey(const Key('settings-url')), findsOneWidget);
  });
}
