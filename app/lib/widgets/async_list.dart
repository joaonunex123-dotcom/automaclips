import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/theme.dart';

/// Lista com pull-to-refresh, estado de carregando, vazio e erro com "tentar de novo".
class AsyncList<T> extends StatelessWidget {
  const AsyncList({
    super.key,
    required this.value,
    required this.onRefresh,
    required this.itemBuilder,
    required this.emptyText,
  });

  final AsyncValue<List<T>> value;
  final Future<void> Function() onRefresh;
  final Widget Function(BuildContext context, T item) itemBuilder;
  final String emptyText;

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: onRefresh,
      child: value.when(
        loading: () => _centered(
          context,
          const CircularProgressIndicator(color: JarvisColors.accent),
        ),
        error: (error, _) => _centered(
          context,
          Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                '$error',
                textAlign: TextAlign.center,
                style: const TextStyle(color: JarvisColors.danger),
              ),
              const SizedBox(height: 12),
              OutlinedButton(
                onPressed: onRefresh,
                child: const Text('Tentar de novo'),
              ),
            ],
          ),
        ),
        data: (items) => items.isEmpty
            ? _centered(
                context,
                Text(emptyText, style: const TextStyle(color: JarvisColors.textMuted)),
              )
            : ListView.builder(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 12, 12, 96),
                itemCount: items.length,
                itemBuilder: (context, i) => itemBuilder(context, items[i]),
              ),
      ),
    );
  }

  Widget _centered(BuildContext context, Widget child) {
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      children: [
        SizedBox(
          height: MediaQuery.sizeOf(context).height * 0.6,
          child: Center(
            child: Padding(padding: const EdgeInsets.all(24), child: child),
          ),
        ),
      ],
    );
  }
}
