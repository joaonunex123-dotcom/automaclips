import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/api/jarvis_client.dart';
import 'package:jarvis_app/core/theme.dart';
import 'package:jarvis_app/screens/settings_screen.dart';
import 'package:jarvis_app/state/providers.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../support/fakes.dart';

Future<SharedPreferences> pumpSettings(
  WidgetTester tester,
  FakeJarvisClient client,
) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(ProviderScope(
    overrides: [
      sharedPrefsProvider.overrideWithValue(prefs),
      jarvisClientFactoryProvider.overrideWithValue((url, token) => client),
    ],
    child: MaterialApp(
      theme: buildJarvisTheme(),
      home: const SettingsScreen(),
    ),
  ));
  return prefs;
}

void main() {
  testWidgets('Testar conexão com sucesso mostra confirmação', (tester) async {
    await pumpSettings(tester, FakeJarvisClient());

    await tester.enterText(find.byKey(const Key('settings-url')), 'jarvis:8000/');
    await tester.enterText(find.byKey(const Key('settings-token')), ' abc ');
    await tester.tap(find.byKey(const Key('settings-test')));
    await tester.pumpAndSettle();

    expect(find.text('Conectado ao Jarvis.'), findsOneWidget);
  });

  testWidgets('Testar conexão com falha mostra o motivo', (tester) async {
    await pumpSettings(
      tester,
      FakeJarvisClient(
        healthError: const JarvisApiException(
          'Token recusado pelo Jarvis (401).',
          statusCode: 401,
        ),
      ),
    );

    await tester.enterText(find.byKey(const Key('settings-url')), 'http://jarvis:8000');
    await tester.enterText(find.byKey(const Key('settings-token')), 'errado');
    await tester.tap(find.byKey(const Key('settings-test')));
    await tester.pumpAndSettle();

    expect(find.text('Token recusado pelo Jarvis (401).'), findsOneWidget);
  });

  testWidgets('Salvar grava URL normalizada e token sem espaços', (tester) async {
    final prefs = await pumpSettings(tester, FakeJarvisClient());

    await tester.enterText(find.byKey(const Key('settings-url')), 'jarvis:8000/');
    await tester.enterText(find.byKey(const Key('settings-token')), ' abc ');
    await tester.tap(find.byKey(const Key('settings-save')));
    await tester.pumpAndSettle();

    expect(prefs.getString('jarvis.baseUrl'), 'http://jarvis:8000');
    expect(prefs.getString('jarvis.token'), 'abc');
    expect(find.text('Configurações salvas.'), findsOneWidget);
  });
}
