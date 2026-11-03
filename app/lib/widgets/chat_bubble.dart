import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

import '../core/models.dart';
import '../core/theme.dart';

class ChatBubble extends StatelessWidget {
  const ChatBubble({super.key, required this.message});

  final ChatMessage message;

  @override
  Widget build(BuildContext context) {
    final isUser = message.role == ChatRole.user;
    final textTheme = Theme.of(context).textTheme;

    final content = isUser
        ? Text(message.text, style: textTheme.bodyMedium)
        : message.text.isEmpty && message.streaming
            ? const _Thinking()
            : MarkdownBody(
                data: message.text,
                selectable: true,
                styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
                  code: textTheme.bodyMedium?.copyWith(
                    fontFamily: 'monospace',
                    backgroundColor: JarvisColors.surfaceHigh,
                  ),
                ),
              );

    final footer = _footer();

    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.sizeOf(context).width * 0.86,
        ),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: isUser ? JarvisColors.surfaceHigh : JarvisColors.surface,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(
              color: message.failed ? JarvisColors.danger : JarvisColors.border,
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              content,
              if (footer != null) ...[
                const SizedBox(height: 6),
                footer,
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget? _footer() {
    if (message.role != ChatRole.assistant || message.streaming) {
      return null;
    }
    if (message.failed) {
      return const Text(
        'falhou',
        style: TextStyle(color: JarvisColors.danger, fontSize: 11),
      );
    }
    final tokens = message.tokens;
    final tps = message.tokensPerSecond;
    if (tokens == null) return null;
    final rate = tps == null ? '' : ' · ${tps.toStringAsFixed(1)} tok/s';
    return Text(
      '$tokens tokens$rate',
      style: const TextStyle(color: JarvisColors.textMuted, fontSize: 11),
    );
  }
}

class _Thinking extends StatelessWidget {
  const _Thinking();

  @override
  Widget build(BuildContext context) {
    return const SizedBox(
      height: 18,
      width: 18,
      child: CircularProgressIndicator(strokeWidth: 2, color: JarvisColors.accent),
    );
  }
}
