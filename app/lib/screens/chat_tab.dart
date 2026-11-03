import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/theme.dart';
import '../state/providers.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/hold_to_talk_button.dart';

class ChatTab extends ConsumerStatefulWidget {
  const ChatTab({super.key});

  @override
  ConsumerState<ChatTab> createState() => _ChatTabState();
}

class _ChatTabState extends ConsumerState<ChatTab> {
  final _input = TextEditingController();
  String? _partial;

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  void _send(String text) {
    ref.read(chatProvider.notifier).send(text);
  }

  void _sendTyped() {
    final text = _input.text.trim();
    if (text.isEmpty) return;
    _input.clear();
    _send(text);
  }

  @override
  Widget build(BuildContext context) {
    final chat = ref.watch(chatProvider);
    final settings = ref.watch(settingsProvider);
    final messages = chat.messages;

    return Column(
      children: [
        if (messages.isNotEmpty)
          Align(
            alignment: Alignment.centerRight,
            child: TextButton.icon(
              onPressed: chat.busy
                  ? null
                  : () => ref.read(chatProvider.notifier).clearHistory(),
              icon: const Icon(Icons.delete_outline, size: 18),
              label: const Text('Limpar conversa'),
            ),
          ),
        Expanded(
          child: messages.isEmpty
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: Text(
                      settings.isConfigured
                          ? 'Escreva ou segure o microfone para falar com o Jarvis.'
                          : 'Configure o endereço do Jarvis em ⚙ (Configurações) para começar.',
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: JarvisColors.textMuted),
                    ),
                  ),
                )
              : ListView.builder(
                  reverse: true,
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
                  itemCount: messages.length,
                  itemBuilder: (context, i) =>
                      ChatBubble(message: messages[messages.length - 1 - i]),
                ),
        ),
        if (_partial != null)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.symmetric(horizontal: 16),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            decoration: BoxDecoration(
              color: JarvisColors.surfaceHigh,
              borderRadius: BorderRadius.circular(10),
            ),
            child: Text(
              _partial!.isEmpty ? 'ouvindo…' : _partial!,
              style: const TextStyle(
                color: JarvisColors.textMuted,
                fontStyle: FontStyle.italic,
              ),
            ),
          ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                HoldToTalkButton(
                  onPartial: (text) => setState(() => _partial = text),
                  onTranscript: _send,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: TextField(
                    controller: _input,
                    minLines: 1,
                    maxLines: 5,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (_) => _sendTyped(),
                    decoration: const InputDecoration(
                      hintText: 'Mensagem',
                      contentPadding: EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton.filled(
                  onPressed: chat.busy ? null : _sendTyped,
                  icon: const Icon(Icons.send),
                  tooltip: 'Enviar',
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
