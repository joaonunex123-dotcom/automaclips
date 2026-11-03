import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/format.dart';
import '../core/models.dart';
import '../core/theme.dart';
import '../state/providers.dart';
import '../widgets/async_list.dart';
import '../widgets/pickers.dart';

class AgendaTab extends ConsumerWidget {
  const AgendaTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final agenda = ref.watch(agendaProvider);

    return Scaffold(
      backgroundColor: Colors.transparent,
      floatingActionButton: FloatingActionButton.extended(
        heroTag: null,
        onPressed: () => showModalBottomSheet<void>(
          context: context,
          isScrollControlled: true,
          useSafeArea: true,
          builder: (_) => const _NewEventSheet(),
        ),
        icon: const Icon(Icons.add),
        label: const Text('Evento'),
      ),
      body: AsyncList<AgendaEvent>(
        value: agenda,
        onRefresh: () async {
          ref.invalidate(agendaProvider);
          await ref.read(agendaProvider.future);
        },
        emptyText: 'Nenhum evento na agenda.',
        itemBuilder: (context, event) => Card(
          child: ListTile(
            leading: Container(
              width: 52,
              padding: const EdgeInsets.symmetric(vertical: 6),
              decoration: BoxDecoration(
                color: JarvisColors.surfaceHigh,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    '${event.start.day.toString().padLeft(2, '0')}/'
                    '${event.start.month.toString().padLeft(2, '0')}',
                    style: const TextStyle(fontSize: 12, color: JarvisColors.textMuted),
                  ),
                  Text(
                    fmtTime(event.start),
                    style: const TextStyle(fontWeight: FontWeight.w700, color: JarvisColors.accent),
                  ),
                ],
              ),
            ),
            title: Text(event.title, style: const TextStyle(fontWeight: FontWeight.w600)),
            subtitle: event.location == null ? null : Text(event.location!),
          ),
        ),
      ),
    );
  }
}

class _NewEventSheet extends ConsumerStatefulWidget {
  const _NewEventSheet();

  @override
  ConsumerState<_NewEventSheet> createState() => _NewEventSheetState();
}

class _NewEventSheetState extends ConsumerState<_NewEventSheet> {
  final _title = TextEditingController();
  final _location = TextEditingController();
  late DateTime _start = _nextHour();
  bool _saving = false;
  String? _error;

  static DateTime _nextHour() {
    final now = DateTime.now();
    return DateTime(now.year, now.month, now.day, now.hour + 1);
  }

  @override
  void dispose() {
    _title.dispose();
    _location.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final title = _title.text.trim();
    if (title.isEmpty) {
      setState(() => _error = 'Dê um título ao evento.');
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await ref.read(agendaProvider.notifier).create(
            title: title,
            start: _start,
            location: _location.text.trim(),
          );
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
          const Text('Novo evento', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          TextField(
            controller: _title,
            autofocus: true,
            decoration: const InputDecoration(hintText: 'Título'),
            textCapitalization: TextCapitalization.sentences,
          ),
          const SizedBox(height: 10),
          DateTimeField(
            value: _start,
            onChanged: (value) => setState(() => _start = value),
          ),
          const SizedBox(height: 10),
          TextField(
            controller: _location,
            decoration: const InputDecoration(hintText: 'Local (opcional)'),
          ),
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!, style: const TextStyle(color: JarvisColors.danger)),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _saving ? null : _save,
            child: const Text('Criar evento'),
          ),
        ],
      ),
    );
  }
}
