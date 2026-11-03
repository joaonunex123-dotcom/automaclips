import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/core/models.dart';
import 'package:jarvis_app/core/stream_metrics.dart';

void main() {
  group('AppSettings.normalizeUrl', () {
    test('acrescenta http:// e tira barras finais', () {
      expect(AppSettings.normalizeUrl(' jarvis:8000/ '), 'http://jarvis:8000');
      expect(AppSettings.normalizeUrl('https://pi.tail1234.ts.net//'),
          'https://pi.tail1234.ts.net');
      expect(AppSettings.normalizeUrl(''), '');
    });

    test('isConfigured depende de haver URL', () {
      expect(const AppSettings().isConfigured, isFalse);
      expect(const AppSettings(baseUrl: 'http://jarvis:8000').isConfigured, isTrue);
    });
  });

  group('modelos', () {
    test('Note.fromJson tolera campos ausentes e id numérico', () {
      final note = Note.fromJson({'id': 7, 'title': 'Oi'});
      expect(note.id, '7');
      expect(note.title, 'Oi');
      expect(note.body, '');
      expect(note.updatedAt, isNull);
    });

    test('AgendaEvent trata location vazio como null', () {
      final event = AgendaEvent.fromJson({
        'id': '1',
        'title': 'x',
        'start': '2030-01-02T09:00:00Z',
        'location': '',
      });
      expect(event.location, isNull);
      expect(event.start.toUtc(), DateTime.utc(2030, 1, 2, 9));
    });

    test('Reminder.fromJson lê done', () {
      final r = Reminder.fromJson({'id': '1', 'text': 'x', 'at': '2030-01-01T00:00:00Z', 'done': true});
      expect(r.done, isTrue);
    });

    test('JarvisEvent sem state vira idle', () {
      expect(JarvisEvent.fromJson({'detail': 'oi'}).state, 'idle');
    });

    test('ChatMessage sobrevive a toJson/fromJson', () {
      const m = ChatMessage(
        id: 'a1',
        role: ChatRole.assistant,
        text: 'pronto',
        tokens: 12,
        tokensPerSecond: 8.5,
      );
      final back = ChatMessage.fromJson(m.toJson());
      expect(back.text, 'pronto');
      expect(back.role, ChatRole.assistant);
      expect(back.tokens, 12);
      expect(back.tokensPerSecond, 8.5);
    });
  });

  group('tokens/s', () {
    test('tokensPerSecond divide tokens pelo tempo', () {
      expect(tokensPerSecond(10, const Duration(seconds: 2)), 5.0);
    });

    test('sem tokens ou tempo curto demais não mede', () {
      expect(tokensPerSecond(0, const Duration(seconds: 1)), isNull);
      expect(tokensPerSecond(5, const Duration(milliseconds: 10)), isNull);
    });

    test('StreamMetrics mede do primeiro token até o fim', () {
      var now = DateTime(2030);
      final metrics = StreamMetrics(clock: () => now);

      metrics.onToken();
      now = now.add(const Duration(seconds: 1));
      metrics.onToken();
      metrics.onToken();
      now = now.add(const Duration(seconds: 1));
      final r = metrics.finish();

      expect(r.tokens, 3);
      expect(r.tokensPerSecond, closeTo(1.5, 1e-9));
    });

    test('contagem do servidor tem prioridade', () {
      var now = DateTime(2030);
      final metrics = StreamMetrics(clock: () => now);
      metrics.onToken();
      now = now.add(const Duration(seconds: 4));
      final r = metrics.finish(serverTokens: 20);
      expect(r.tokens, 20);
      expect(r.tokensPerSecond, 5.0);
    });
  });
}
