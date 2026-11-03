/// Contagem de tokens e tokens/s de uma resposta em streaming.
///
/// tokens/s = tokens / (tempo entre o primeiro token e o fim da resposta).
/// Se o backend mandar a contagem no frame "done", ela tem prioridade.
class StreamMetrics {
  StreamMetrics({DateTime Function()? clock}) : _clock = clock ?? DateTime.now;

  final DateTime Function() _clock;
  int _count = 0;
  DateTime? _first;

  int get count => _count;

  void onToken() {
    _count++;
    _first ??= _clock();
  }

  ({int tokens, double? tokensPerSecond}) finish({int? serverTokens}) {
    final tokens = serverTokens ?? _count;
    final first = _first;
    final elapsed = first == null ? Duration.zero : _clock().difference(first);
    return (tokens: tokens, tokensPerSecond: tokensPerSecond(tokens, elapsed));
  }
}

/// Retorna null quando não dá para medir (zero tokens ou tempo muito curto).
double? tokensPerSecond(int tokens, Duration elapsed) {
  if (tokens <= 0 || elapsed.inMilliseconds < 50) return null;
  return tokens / (elapsed.inMicroseconds / Duration.microsecondsPerSecond);
}
