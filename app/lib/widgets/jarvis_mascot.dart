import 'package:flutter/material.dart';

import '../core/theme.dart';

Color stateColor(String state) => switch (state) {
      'listening' => JarvisColors.stateListening,
      'thinking' => JarvisColors.stateThinking,
      'speaking' => JarvisColors.stateSpeaking,
      'error' => JarvisColors.stateError,
      'offline' => JarvisColors.stateOffline,
      'connecting' => JarvisColors.stateOffline,
      _ => JarvisColors.stateIdle,
    };

String stateLabel(String state) => switch (state) {
      'listening' => 'ouvindo',
      'thinking' => 'pensando',
      'speaking' => 'falando',
      'error' => 'erro',
      'offline' => 'sem conexão',
      'connecting' => 'conectando…',
      _ => 'ocioso',
    };

/// Mascote pequeno: uma cabeça de robô cujos olhos e boca mudam com o estado.
class JarvisMascot extends StatelessWidget {
  const JarvisMascot({super.key, required this.state, this.size = 32});

  final String state;
  final double size;

  @override
  Widget build(BuildContext context) {
    final target = stateColor(state);
    return TweenAnimationBuilder<Color?>(
      tween: ColorTween(end: target),
      duration: const Duration(milliseconds: 300),
      builder: (context, color, _) => CustomPaint(
        size: Size.square(size),
        painter: _MascotPainter(color: color ?? target, state: state),
      ),
    );
  }
}

class _MascotPainter extends CustomPainter {
  _MascotPainter({required this.color, required this.state});

  final Color color;
  final String state;

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;

    final stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = w * 0.07
      ..strokeCap = StrokeCap.round;
    final fill = Paint()..color = color;

    // antena
    canvas.drawLine(Offset(w * 0.5, h * 0.12), Offset(w * 0.5, h * 0.02), stroke);
    canvas.drawCircle(Offset(w * 0.5, h * 0.02), w * 0.05, fill);

    // cabeça
    final head = RRect.fromRectAndRadius(
      Rect.fromLTWH(w * 0.12, h * 0.14, w * 0.76, h * 0.74),
      Radius.circular(w * 0.24),
    );
    canvas.drawRRect(
      head,
      Paint()..color = JarvisColors.surfaceHigh,
    );
    canvas.drawRRect(head, stroke);

    final eyeY = h * 0.42;
    final leftEye = Offset(w * 0.36, eyeY);
    final rightEye = Offset(w * 0.64, eyeY);

    switch (state) {
      case 'thinking':
        // olhos em "—" (concentrado)
        canvas.drawLine(leftEye.translate(-w * 0.07, 0), leftEye.translate(w * 0.07, 0), stroke);
        canvas.drawLine(rightEye.translate(-w * 0.07, 0), rightEye.translate(w * 0.07, 0), stroke);
      case 'offline' || 'connecting':
        // olhos fechados
        canvas.drawLine(leftEye.translate(-w * 0.07, 0), leftEye.translate(w * 0.07, 0), stroke);
        canvas.drawLine(rightEye.translate(-w * 0.07, 0), rightEye.translate(w * 0.07, 0), stroke);
      case 'listening':
        canvas.drawCircle(leftEye, w * 0.1, fill);
        canvas.drawCircle(rightEye, w * 0.1, fill);
      default:
        canvas.drawCircle(leftEye, w * 0.07, fill);
        canvas.drawCircle(rightEye, w * 0.07, fill);
    }

    // boca
    final mouthY = h * 0.68;
    switch (state) {
      case 'speaking':
        canvas.drawOval(
          Rect.fromCenter(center: Offset(w * 0.5, mouthY), width: w * 0.26, height: h * 0.14),
          fill,
        );
      case 'error':
        final path = Path()
          ..moveTo(w * 0.36, mouthY + h * 0.06)
          ..quadraticBezierTo(w * 0.5, mouthY - h * 0.04, w * 0.64, mouthY + h * 0.06);
        canvas.drawPath(path, stroke);
      default:
        final path = Path()
          ..moveTo(w * 0.36, mouthY)
          ..quadraticBezierTo(w * 0.5, mouthY + h * 0.07, w * 0.64, mouthY);
        canvas.drawPath(path, stroke);
    }
  }

  @override
  bool shouldRepaint(_MascotPainter old) => old.color != color || old.state != state;
}
