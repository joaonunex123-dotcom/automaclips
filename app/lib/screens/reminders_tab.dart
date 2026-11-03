import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/format.dart';
import '../core/models.dart';
import '../core/theme.dart';
import '../state/providers.dart';
import '../widgets/async_list.dart';
import '../widgets/pickers.dart';

class RemindersTab extends ConsumerWidget {
  const RemindersTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final reminders = ref.watch(remindersProvider);

    return Scaffold(
      backgroundColor: Colors.transparent,
      floatingActionButton: FloatingActionButton.extended(
        heroTag: null,
        onPressed: () => showModalBottomSheet<void>(
          context: context,
          isScrollControlled: true,
          useSafeArea: true,
          builder: (_) => const _NewReminderSheet(),
        ),
        icon: const Icon(Icons.add_alarm),
        label: const Text('Lembrete'),
      ),
      body: AsyncList<Reminder>(
        value: reminders,
        onRefresh: () async {
          ref.invalidate(remindersProvider);
          await ref.read(remindersProvider.future);
        },
        emptyText: 'Nenhum lembrete.',
        itemBuilder: (context, reminder) {
          final past = reminder.at.isBefore(DateTime.now());
          return Card(
            child: ListTile(
              leading: Icon(
                past ? Icons.notifications_off_outlined : Icons.alarm,
                color: past ? JarvisColors.textMuted : JarvisColors.accent,
              ),
              title: Text(
                reminder.text,
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
              subtitle: Text(fmtDateTime(reminder.at)),
              trailing: IconButton(
                tooltip: 'Apagar',
                icon: const Icon(Icons.delete_outline, color: JarvisColors.textMuted),
                onPressed: () =>
                    ref.read(remindersProvider.notifier).remove(reminder.id),
              ),
            ),
          );
        },
      ),
    );
  }
}

class _NewReminderSheet extends ConsumerStatefulWidget {
  const _NewReminderSheet();

  @override
  ConsumerState<_NewReminderSheet> createState() => _NewReminderSheetState();
}

class _NewReminderSheetState extends ConsumerState<_NewReminderSheet> {
  final _text = TextEditingController();
  late DateTime _at = _defaultTime();
  bool _saving = false;
  String? _error;

  static DateTime _defaultTime() {
    final now = DateTime.now().add(const Duration(minutes: 30));
    return DateTime(now.year, now.month, now.day, now.hour, now.minute);
  }

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final text = _text.text.trim();
    if (text.isEmpty) {
      setState(() => _error = 'Escreva o que lembrar.');
      return;
    }
    if (_at.isBefore(DateTime.now())) {
      setState(() => _error = 'Escolha um horário no futuro.');
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await ref.read(remindersProvider.notifier).create(text: text, at: _at);
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      if (mounted) {
        setState(() {
          _saving = false;
          _error = '$e';
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(
        16,
        16,
        16,
        16 + MediaQuery.viewInsetsOf(context).bottom,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text('Novo lembrete', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          TextField(
            controller: _text,
            autofocus: true,
            decoration: const InputDecoration(hintText: 'Me lembre de…'),
            textCapitalization: TextCapitalization.sentences,
          ),
          const SizedBox(height: 10),
          DateTimeField(
            value: _at,
            onChanged: (value) => setState(() => _at = value),
          ),
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!, style: const TextStyle(color: JarvisColors.danger)),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _saving ? null : _save,
            child: const Text('Criar lembrete'),
          ),
        ],
      ),
    );
  }
}
