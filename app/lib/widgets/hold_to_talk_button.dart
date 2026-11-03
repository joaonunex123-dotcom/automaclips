import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/theme.dart';
import '../state/speech_input.dart';

/// Botão "segurar pra falar": enquanto segura, ouve e mostra a transcrição
/// parcial via [onPartial]; ao soltar, entrega o texto final em [onTranscript].
class HoldToTalkButton extends ConsumerStatefulWidget {
  const HoldToTalkButton({
    super.key,
    required this.onTranscript,
    required this.onPartial,
  });

  final ValueChanged<String> onTranscript;

  /// Recebe o texto parcial enquanto ouve e null quando para.
  final ValueChanged<String?> onPartial;

  @override
  ConsumerState<HoldToTalkButton> createState() => _HoldToTalkButtonState();
}

class _HoldToTalkButtonState extends ConsumerState<HoldToTalkButton> {
  bool _held = false;
  bool _listening = false;
  Future<void>? _starting;

  Future<void> _begin() async {
    final speech = ref.read(speechInputProvider);
    try {
      final ok = await speech.init();
      if (!mounted) return;
      if (!ok) {
        _warn('Reconhecimento de voz indisponível. Confira a permissão do microfone.');
        return;
      }
      await speech.start(onPartial: (text) {
        if (mounted) widget.onPartial(text);
      });
      _listening = true;
    } catch (e) {
      if (mounted) _warn('Não consegui ouvir agora: $e');
    }
  }

  void _warn(String message) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  void _down(LongPressStartDetails _) {
    setState(() => _held = true);
    _starting = _begin();
  }

  Future<void> _up(LongPressEndDetails _) async {
    setState(() => _held = false);
    await _starting;
    if (!_listening) return;
    _listening = false;
    final text = await ref.read(speechInputProvider).stop();
    if (mounted) widget.onPartial(null);
    if (text.isNotEmpty) widget.onTranscript(text);
  }

  Future<void> _cancel() async {
    setState(() => _held = false);
    await _starting;
    if (!_listening) return;
    _listening = false;
    await ref.read(speechInputProvider).stop();
    if (mounted) widget.onPartial(null);
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onLongPressStart: _down,
      onLongPressEnd: _up,
      onLongPressCancel: _cancel,
      child: AnimatedScale(
        scale: _held ? 1.15 : 1,
        duration: const Duration(milliseconds: 150),
        child: Container(
          key: const Key('hold-to-talk'),
          width: 48,
          height: 48,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: _held ? JarvisColors.accent : JarvisColors.surfaceHigh,
            border: Border.all(color: JarvisColors.accent),
          ),
          child: Icon(
            _held ? Icons.mic : Icons.mic_none,
            color: _held ? JarvisColors.background : JarvisColors.accent,
          ),
        ),
      ),
    );
  }
}
