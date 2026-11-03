import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/format.dart';
import '../core/models.dart';
import '../core/theme.dart';
import '../state/providers.dart';
import '../widgets/async_list.dart';

class NotesTab extends ConsumerWidget {
  const NotesTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final notes = ref.watch(notesProvider);

    return Scaffold(
      backgroundColor: Colors.transparent,
      floatingActionButton: FloatingActionButton.extended(
        heroTag: null,
        onPressed: () => Navigator.of(context).push(
          MaterialPageRoute(builder: (_) => const NoteEditorScreen()),
        ),
        icon: const Icon(Icons.add),
        label: const Text('Nota'),
      ),
      body: AsyncList<Note>(
        value: notes,
        onRefresh: () async {
          ref.invalidate(notesProvider);
          await ref.read(notesProvider.future);
        },
        emptyText: 'Nenhuma nota ainda.',
        itemBuilder: (context, note) => Card(
          child: ListTile(
            title: Text(
              note.title.isEmpty ? '(sem título)' : note.title,
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
            subtitle: Text(
              note.body,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
            ),
            trailing: IconButton(
              tooltip: 'Apagar',
              icon: const Icon(Icons.delete_outline, color: JarvisColors.textMuted),
              onPressed: () => ref.read(notesProvider.notifier).remove(note.id),
            ),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => NoteEditorScreen(note: note)),
            ),
          ),
        ),
      ),
    );
  }
}

class NoteEditorScreen extends ConsumerStatefulWidget {
  const NoteEditorScreen({super.key, this.note});

  final Note? note;

  @override
  ConsumerState<NoteEditorScreen> createState() => _NoteEditorScreenState();
}

class _NoteEditorScreenState extends ConsumerState<NoteEditorScreen> {
  late final TextEditingController _title =
      TextEditingController(text: widget.note?.title ?? '');
  late final TextEditingController _body =
      TextEditingController(text: widget.note?.body ?? '');
  bool _saving = false;

  @override
  void dispose() {
    _title.dispose();
    _body.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    final notifier = ref.read(notesProvider.notifier);
    final note = widget.note;
    try {
      if (note == null) {
        await notifier.create(title: _title.text.trim(), body: _body.text);
      } else {
        await notifier.edit(note.copyWith(
          title: _title.text.trim(),
          body: _body.text,
        ));
      }
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      if (mounted) {
        setState(() => _saving = false);
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$e')));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final updated = widget.note?.updatedAt;
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.note == null ? 'Nova nota' : 'Editar nota'),
        actions: [
          TextButton(
            onPressed: _saving ? null : _save,
            child: const Text('Salvar'),
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _title,
            decoration: const InputDecoration(hintText: 'Título'),
            textCapitalization: TextCapitalization.sentences,
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _body,
            decoration: const InputDecoration(hintText: 'Escreva aqui…'),
            minLines: 12,
            maxLines: null,
            keyboardType: TextInputType.multiline,
            textCapitalization: TextCapitalization.sentences,
          ),
          if (updated != null) ...[
            const SizedBox(height: 12),
            Text(
              'Atualizada em ${fmtDateTime(updated)}',
              style: const TextStyle(color: JarvisColors.textMuted, fontSize: 12),
            ),
          ],
        ],
      ),
    );
  }
}
