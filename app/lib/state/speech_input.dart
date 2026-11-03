import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:speech_to_text/speech_to_text.dart';

/// Transcrição no próprio celular (sem enviar áudio a lugar nenhum).
abstract interface class SpeechInput {
  /// false se o aparelho não tiver reconhecimento de voz ou a permissão for negada.
  Future<bool> init();

  Future<void> start({required void Function(String partial) onPartial});

  /// Para de ouvir e devolve o texto final reconhecido ("" se nada).
  Future<String> stop();
}

class DeviceSpeechInput implements SpeechInput {
  DeviceSpeechInput({SpeechToText? engine}) : _stt = engine ?? SpeechToText();

  final SpeechToText _stt;
  bool? _ready;
  String? _localeId;
  String _last = '';
  Completer<String>? _finalizer;

  @override
  Future<bool> init() async {
    _ready ??= await _initialize();
    return _ready!;
  }

  void _finish(String text) {
    final c = _finalizer;
    if (c != null && !c.isCompleted) c.complete(text);
  }

  Future<bool> _initialize() async {
    final ok = await _stt.initialize(
      onError: (_) => _finish(_last),
      onStatus: (status) {
        if (status == SpeechToText.doneStatus ||
            status == SpeechToText.notListeningStatus) {
          _finish(_last);
        }
      },
    );
    if (ok) {
      _localeId = _pickPortuguese(await _stt.locales());
    }
    return ok;
  }

  /// pt_BR se existir; senão qualquer pt_*; senão o padrão do aparelho.
  static String? _pickPortuguese(List<LocaleName> locales) {
    String norm(String s) => s.replaceAll('-', '_').toLowerCase();
    for (final l in locales) {
      if (norm(l.localeId) == 'pt_br') return l.localeId;
    }
    for (final l in locales) {
      if (norm(l.localeId).startsWith('pt')) return l.localeId;
    }
    return null;
  }

  @override
  Future<void> start({required void Function(String partial) onPartial}) async {
    _last = '';
    _finalizer = null;
    await _stt.listen(
      onResult: (result) {
        _last = result.recognizedWords;
        onPartial(_last);
        if (result.finalResult) _finish(_last);
      },
      listenOptions: SpeechListenOptions(
        localeId: _localeId,
        partialResults: true,
        listenMode: ListenMode.dictation,
        listenFor: const Duration(seconds: 60),
        pauseFor: const Duration(seconds: 30),
      ),
    );
  }

  @override
  Future<String> stop() async {
    final completer = Completer<String>();
    _finalizer = completer;
    await _stt.stop();
    return completer.future
        .timeout(const Duration(seconds: 2), onTimeout: () => _last)
        .then((text) => text.trim());
  }
}

final speechInputProvider = Provider<SpeechInput>((ref) => DeviceSpeechInput());
