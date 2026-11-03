String _two(int n) => n.toString().padLeft(2, '0');

/// 03/11 14:05
String fmtDateTime(DateTime d) =>
    '${_two(d.day)}/${_two(d.month)} ${_two(d.hour)}:${_two(d.minute)}';

/// 03/11/2026
String fmtDate(DateTime d) => '${_two(d.day)}/${_two(d.month)}/${d.year}';

/// 14:05
String fmtTime(DateTime d) => '${_two(d.hour)}:${_two(d.minute)}';
