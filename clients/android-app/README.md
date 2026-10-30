# Virtual Life — Android host app

Native Android host that pairs the JVM [node SDK](../android-node/) with device
audio so a companion runs continuous dialogue: **on-device ASR → server LLM →
server TTS → on-device playback**. The app registers one device node, opens an
`android` presentation, owns the microphone/playback and never uploads raw
microphone audio.

The wire contracts live in the SDK ([voice contract](../../docs/voice-contract.md),
[runtime protocol](../../docs/protocol.md)); this app only supplies the Android
host half (permissions, foreground service, `AudioRecord` / `AudioTrack`, local
sherpa-onnx ASR).

## Layout

| Path | Purpose |
| --- | --- |
| `app/src/main/java/dev/virtuallife/app/VoiceEngine.kt` | node session, presentation, one voice session, mic → ASR → turn submit → PCM playback |
| `app/src/main/java/dev/virtuallife/app/LocalAsr.kt` | sherpa-onnx streaming recognizer (file-based weights) |
| `app/src/main/java/dev/virtuallife/app/MicCapture.kt` | mono PCM16 `AudioRecord` |
| `app/src/main/java/dev/virtuallife/app/PcmPlayer.kt` | `AudioTrack` `MODE_STREAM` playback with a real playback-head position |
| `app/src/main/java/dev/virtuallife/app/ModelRepository.kt` | local ASR model discovery (internal + adb-pushable external dir) |
| `app/src/main/java/dev/virtuallife/app/VoiceService.kt` | foreground service + notification |
| `app/src/main/java/dev/virtuallife/app/ScreenFrameFilter.kt` | pure change/interval filter (unit-tested) |
| `app/src/main/java/dev/virtuallife/app/ScreenCapture.kt` | MediaProjection mirror → JPEG + fingerprint |
| `app/src/main/java/dev/virtuallife/app/ServerScreenController.kt` | screen session, upload, frame submit, analysis poll |
| `app/src/main/java/dev/virtuallife/app/ScreenService.kt` | mediaProjection foreground service |
| `app/src/main/java/dev/virtuallife/app/MainActivity.kt` | permission prompts and controls |

## Build

Requires JDK 17+ and an Android SDK with `platforms;android-34` and
`build-tools;34.0.0`. Point `ANDROID_HOME` at the SDK (or set `sdk.dir` in
`local.properties`). The SDK build is reused as a composite build.

```sh
ANDROID_HOME=$HOME/android-sdk ./build-app.sh
# Runs :app:assembleDebug :app:testDebugUnitTest by default.
# APK: app/build/outputs/apk/debug/app-debug.apk (arm64-v8a)
# Extra checks: ./build-app.sh :app:testDebugUnitTest :app:lintDebug
```

The native libraries and ASR weights are not committed. `app/libs`/`.deps`
expect `sherpa-onnx-1.13.8.aar`
(`.deps/sherpa-onnx-1.13.8.aar`) and the mobile Zipformer model tarball
(`.deps/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23-mobile.tar.bz2`).

## Install and run

```sh
# 1. Push the extracted model into the app's external files dir (no model in the APK).
adb shell mkdir -p /sdcard/Android/data/dev.virtuallife.app/files/asr
# unzip the model tarball on the host, then:
adb push sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23-mobile \
  /sdcard/Android/data/dev.virtuallife.app/files/asr/
# 2. Install and launch; grant microphone + notifications, then tap 导入模型 → 启动.
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Point the app at the runtime from the debug build defaults (emulator reaches the
host at `http://10.0.2.2:8000/` with the local development token). The bearer
token stays in app-private preferences.

**The preferences file is `shared_prefs/virtual_life.xml`** (the prefs name is
`"virtual_life"` in `AppConfig.kt`, NOT the default
`<applicationId>_preferences.xml`). Setting it up for a non-default runtime:

```sh
# build a prefs XML on the host, then:
adb push prefs.xml /data/local/tmp/prefs.xml
adb shell run-as dev.virtuallife.app sh -c \
  'mkdir -p shared_prefs && cat /data/local/tmp/prefs.xml > shared_prefs/virtual_life.xml'
# run-as needs the file pushed first (direct heredoc fails on /data/local/tmp perms).
```

Keys: `base_url` (emulator→host is `http://10.0.2.2:8038/` for the local
runtime; `:8000` is the README example only), `token` (node reporter token),
`admin_token` (screen API is admin-token-only), `node_id`
(`android-<ANDROID_ID>`), `companion_id`. Force-stop and relaunch after writing.

## What is covered vs. not

Implemented and compiled: node registration, `android` presentation, companion
claim, voice session creation, local streaming ASR, VAD/barge-in speech
detection, PCM playback, playback reporting, interrupt, session/presentation
heartbeats, foreground service, the microphone permission flow, and the
server-side screen-vision path (MediaProjection capture → changed-frame filter →
register/upload/submit → status/analysis poll, with the `mediaProjection`
foreground service and consent flow).

Screen vision talks to the admin-token-only screen API, so it uses a separate
`admin_token` preference (defaults to the local development token) next to the
node reporter token. Frames are captured locally with `MediaProjection`
(`capture_source=android_media_projection`) and only changed/keyframe frames are
uploaded; vision itself always runs on the server. Shizuku capture is **not**
implemented — MediaProjection is the implemented path, not a claimed Shizuku
result. Screen sharing is independent from voice: stopping voice leaves screen
capture alone and vice versa.

To feed a companion that lives on an IM node (QQ/Matrix) rather than on this
Android node, set the screen session's `companion_id` to that IM companion (in
`AppConfig`, the `companion_id` preference; the default `android-companion` is
only a placeholder) and set the IM bridge's `screen_observer_node` to this
node's `node_id` (default `android-<ANDROID_ID>`). The IM bridge then attaches a
`screen_reference` to each dialogue turn and the server injects this node's
fresh frame into the reply. Each submitted frame also carries the classified
foreground `activity` (and `app_id`) in `host_metadata`, which the server
surfaces as a short `当前前台应用: <app>（<activity>）` line next to the
`[screen_image]` marker. The Android node itself stays capture-only (it has
no output capability, so the companion never migrates onto it).

Not validated here (require a real device and a running server): actual ASR
accuracy, acoustic echo cancellation, headset routing, telephony/notification
audio focus, lock-screen/background behaviour, real frame capture and rotation
reporting, server screen API round-trips, and the 16 KiB page-size compatibility
check for the sherpa native libraries.

Echo cancellation is not claimed: capture uses `VOICE_RECOGNITION` and playback
pauses the recognizer feed (half-duplex) while a reply is playing; barge-in is a
local energy threshold that stops playback, not a measured AEC result. Prefer a
headset for real full-duplex use.
