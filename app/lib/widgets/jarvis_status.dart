import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/models.dart';
import '../state/providers.dart';
import 'jarvis_mascot.dart';

/// Topo do app: mascote + estado atual do Jarvis (vindo do SSE /events).
class JarvisStatusLine extends ConsumerWidget {
  const JarvisStatusLine({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final event = ref.watch(jarvisEventsProvider).value ?? JarvisEvent.connecting;
    final color = stateColor(event.state);
    final detail = event.detail.isEmpty ? '' : ' · ${event.detail}';

    return Row(
      children: [
        JarvisMascot(state: event.state, size: 30),
        const SizedBox(width: 10),
        Flexible(
          child: Text(
            'Jarvis · ${stateLabel(event.state)}$detail',
            overflow: TextOverflow.ellipsis,
            style: TextStyle(color: color, fontWeight: FontWeight.w600, fontSize: 14),
          ),
        ),
      ],
    );
  }
}
