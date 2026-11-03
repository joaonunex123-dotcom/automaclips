import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/jarvis_client.dart';
import '../core/models.dart';
import '../core/theme.dart';
import '../state/providers.dart';

class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  late final TextEditingController _url =
      TextEditingController(text: ref.read(settingsProvider).baseUrl);
  late final TextEditingController _token =
      TextEditingController(text: ref.read(settingsProvider).token);

  bool _obscure = true;
  bool _testing = false;
  String? _result;
  bool _resultOk = false;

  @override
  void dispose() {
    _url.dispose();
    _token.dispose();
    super.dispose();
  }

  /// Testa com os valores digitados, mesmo que ainda não estejam salvos.
  Future<void> _test() async {
    setState(() {
      _testing = true;
      _result = null;
    });
    final url = AppSettings.normalizeUrl(_url.text);
    final client = ref.read(jarvisClientFactoryProvider)(url, _token.text.trim());
    try {
      await client.testConnection();
      _show(true, 'Conectado ao Jarvis.');
    } on JarvisApiException catch (e) {
      _show(false, e.message);
    } finally {
      client.close();
    }
  }

  void _show(bool ok, String message) {
    if (!mounted) return;
    setState(() {
      _testing = false;
      _resultOk = ok;
      _result = message;
    });
  }

  Future<void> _save() async {
    await ref
        .read(settingsProvider.notifier)
        .save(baseUrl: _url.text, token: _token.text);
    if (!mounted) return;
    setState(() => _url.text = ref.read(settingsProvider).baseUrl);
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Configurações salvas.')),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Configurações')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text(
            'Endereço do Pi na rede Tailscale. Ex.: http://jarvis:8000',
            style: TextStyle(color: JarvisColors.textMuted),
          ),
          const SizedBox(height: 16),
          TextField(
            key: const Key('settings-url'),
            controller: _url,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'URL do Jarvis',
              hintText: 'http://jarvis:8000',
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            key: const Key('settings-token'),
            controller: _token,
            obscureText: _obscure,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
              labelText: 'Token',
              suffixIcon: IconButton(
                icon: Icon(_obscure ? Icons.visibility : Icons.visibility_off),
                onPressed: () => setState(() => _obscure = !_obscure),
              ),
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  key: const Key('settings-test'),
                  onPressed: _testing ? null : _test,
                  icon: _testing
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.wifi_tethering),
                  label: const Text('Testar conexão'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton(
                  key: const Key('settings-save'),
                  onPressed: _save,
                  child: const Text('Salvar'),
                ),
              ),
            ],
          ),
          if (_result != null) ...[
            const SizedBox(height: 16),
            Text(
              _result!,
              key: const Key('settings-result'),
              style: TextStyle(
                color: _resultOk ? JarvisColors.stateListening : JarvisColors.danger,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
