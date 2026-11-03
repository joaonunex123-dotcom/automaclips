import 'package:flutter/material.dart';

/// Paleta do Jarvis.
///
/// PLACEHOLDER: o repositório não tinha screen/ (tela web) quando este app foi
/// criado. Troque os valores abaixo pelas cores de screen/ para ficar igual.
abstract final class JarvisColors {
  static const background = Color(0xFF0B0E14);
  static const surface = Color(0xFF151A23);
  static const surfaceHigh = Color(0xFF1E2531);
  static const border = Color(0xFF2A3342);
  static const text = Color(0xFFE6EAF2);
  static const textMuted = Color(0xFF8A94A6);
  static const accent = Color(0xFF4FD1C5);
  static const danger = Color(0xFFF87171);

  // Estados do Jarvis (usados no mascote e no texto do topo).
  static const stateIdle = Color(0xFF4FD1C5);
  static const stateListening = Color(0xFF68D391);
  static const stateThinking = Color(0xFFF6AD55);
  static const stateSpeaking = Color(0xFFB794F4);
  static const stateError = Color(0xFFF87171);
  static const stateOffline = Color(0xFF64748B);
}

ThemeData buildJarvisTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: JarvisColors.accent,
    brightness: Brightness.dark,
  ).copyWith(
    primary: JarvisColors.accent,
    onPrimary: JarvisColors.background,
    surface: JarvisColors.surface,
    onSurface: JarvisColors.text,
    error: JarvisColors.danger,
    outline: JarvisColors.border,
  );

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: JarvisColors.background,
    canvasColor: JarvisColors.background,
    appBarTheme: const AppBarTheme(
      backgroundColor: JarvisColors.surface,
      foregroundColor: JarvisColors.text,
      elevation: 0,
      scrolledUnderElevation: 0,
    ),
    navigationBarTheme: const NavigationBarThemeData(
      backgroundColor: JarvisColors.surface,
      indicatorColor: JarvisColors.surfaceHigh,
    ),
    cardTheme: const CardThemeData(
      color: JarvisColors.surface,
      elevation: 0,
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.all(Radius.circular(12)),
        side: BorderSide(color: JarvisColors.border),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: JarvisColors.surfaceHigh,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: JarvisColors.border),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: JarvisColors.border),
      ),
    ),
    dividerColor: JarvisColors.border,
    textTheme: const TextTheme().apply(
      bodyColor: JarvisColors.text,
      displayColor: JarvisColors.text,
    ),
  );
}
