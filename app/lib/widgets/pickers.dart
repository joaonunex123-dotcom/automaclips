import 'package:flutter/material.dart';

import '../core/format.dart';

/// Escolhe data e hora. Devolve null se o usuário cancelar.
Future<DateTime?> pickDateTime(BuildContext context, DateTime initial) async {
  final date = await showDatePicker(
    context: context,
    initialDate: initial,
    firstDate: DateTime.now().subtract(const Duration(days: 1)),
    lastDate: DateTime.now().add(const Duration(days: 365 * 3)),
  );
  if (date == null || !context.mounted) return null;
  final time = await showTimePicker(
    context: context,
    initialTime: TimeOfDay.fromDateTime(initial),
  );
  if (time == null) return null;
  return DateTime(date.year, date.month, date.day, time.hour, time.minute);
}

/// Linha "Data 03/11/2026  ·  Hora 14:05" que abre os pickers ao tocar.
class DateTimeField extends StatelessWidget {
  const DateTimeField({super.key, required this.value, required this.onChanged});

  final DateTime value;
  final ValueChanged<DateTime> onChanged;

  @override
  Widget build(BuildContext context) {
    return OutlinedButton.icon(
      icon: const Icon(Icons.event),
      label: Text('${fmtDate(value)} às ${fmtTime(value)}'),
      onPressed: () async {
        final picked = await pickDateTime(context, value);
        if (picked != null) onChanged(picked);
      },
    );
  }
}
