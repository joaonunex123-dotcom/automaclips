# Jarvis — app Flutter (Android/iOS)

Extensão do Jarvis no celular: chat com streaming, notas, agenda, lembretes e
botão "segurar pra falar". Fala com o backend FastAPI no Pi pela rede Tailscale.

## Estrutura

```
lib/
  api/jarvis_client.dart   TODA a comunicação com o backend (REST, SSE, WebSocket)
  core/                    modelos, tema, formatação, medição de tokens/s
  state/                   Riverpod: configurações, chat, notas, agenda, lembretes, voz
  screens/                 Chat, Notas, Agenda, Lembretes, Configurações
  widgets/                 mascote, barra de estado, bolha de chat, botão de voz
test/
  support/mock_jarvis_server.dart   mock HTTP/SSE/WebSocket do backend (testes)
  api/ core/ widgets/               testes
```

## Pré-requisitos

- Flutter stable 3.47+ (Dart 3.13+). Testado com 3.47.9.
- Para o APK: Android SDK (pelo Android Studio ou `sdkmanager`) e JDK 17.
- Para iOS: Xcode em um Mac.

## Rodar em desenvolvimento

```bash
cd app
flutter pub get
flutter run            # aparelho ou emulador conectado
```

## Gerar o APK

```bash
cd app
flutter pub get
flutter build apk --release
```

Saída: `build/app/outputs/flutter-apk/app-release.apk`.

Para um APK menor por arquitetura (um arquivo por ABI):

```bash
flutter build apk --release --split-per-abi
```

Sem configuração de assinatura, o build de release usa a chave de debug do
projeto. Para distribuir, crie uma keystore e configure `signingConfigs` em
`android/app/build.gradle.kts`.

Instale no aparelho com `adb install build/app/outputs/flutter-apk/app-release.apk`
ou copie o arquivo para o celular.

## Primeiro uso

1. Instale o Tailscale no celular e confirme que o Pi (`jarvis`) está na mesma tailnet.
2. Abra o app. Na primeira vez ele já abre **Configurações**.
3. Preencha **URL do Jarvis** (ex.: `http://jarvis:8000`) e o **Token**.
4. Toque em **Testar conexão**. Se aparecer "Conectado ao Jarvis.", toque em **Salvar**.
5. O topo mostra o mascote e o estado do Jarvis (vem do SSE `/events`), igual à tela física.

Configurações ficam salvas no aparelho (SharedPreferences). Nada vai para a nuvem.

### Voz

Segure o botão de microfone e fale. O reconhecimento roda no próprio aparelho,
em pt-BR (se o aparelho tiver). Ao soltar, o texto é enviado ao chat.

## Testes

```bash
cd app
flutter analyze
flutter test
```

Os testes do cliente HTTP sobem um servidor mock local (`test/support/mock_jarvis_server.dart`).
Os testes de widget usam clientes e voz falsos. Nenhum teste precisa do Pi.

## Contrato com o backend

O cliente assume estes endpoints. Estão documentados no cabeçalho de
`lib/api/jarvis_client.dart`. Se o backend real usar outros caminhos ou campos,
ajuste **só esse arquivo**.

| Método | Caminho | Uso |
| --- | --- | --- |
| GET | `/health` | Testar conexão |
| GET / POST | `/notes` | Listar / criar nota |
| PUT / DELETE | `/notes/{id}` | Editar / apagar nota |
| GET / POST | `/agenda` | Listar / criar evento |
| GET / POST | `/reminders` | Listar / criar lembrete |
| DELETE | `/reminders/{id}` | Apagar lembrete |
| GET (SSE) | `/events` | Estado do Jarvis: `data: {"state": "...", "detail": "..."}` |
| WS | `/ws/chat` | Envia `{"type":"message","text":...}`; recebe `{"type":"token","text":...}`, `{"type":"done","tokens":N}` ou `{"type":"error","message":...}` |

Auth: `Authorization: Bearer <token>`, também no WebSocket.
Erros HTTP com corpo `{"detail": "..."}` (padrão FastAPI) mostram o `detail`.

Tokens/s são calculados no app: tokens ÷ tempo entre o primeiro token e o `done`.
Se o `done` trouxer `tokens`, esse número é usado.

## Permissões

- Android: `INTERNET`, `RECORD_AUDIO`; `usesCleartextTraffic` (para `http://`).
- iOS: `NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription`,
  `NSAllowsArbitraryLoads` (para `http://` na tailnet).
