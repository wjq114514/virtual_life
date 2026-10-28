#!/bin/sh
set -eu
android_chat_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
android_chat_jdk=${ANDROID_CHAT_JAVA_HOME:-${JAVA_HOME:-}}
if [ -z "$android_chat_jdk" ] && [ -x /usr/lib/jvm/java-21-openjdk-amd64/bin/java ]; then
    android_chat_jdk=/usr/lib/jvm/java-21-openjdk-amd64
fi
if [ -z "$android_chat_jdk" ]; then
    printf '%s\n' 'Set ANDROID_CHAT_JAVA_HOME to a JDK 17 or newer.' >&2
    exit 1
fi
android_chat_sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/android-sdk}}
if [ ! -d "$android_chat_sdk/platforms" ]; then
    printf 'Android SDK not found at %s (set ANDROID_HOME).\n' "$android_chat_sdk" >&2
    exit 1
fi
android_chat_cache=${ANDROID_CHAT_GRADLE_CACHE:-${TMPDIR:-/tmp}/virtual-life-android-node-gradle}
cd "$android_chat_dir"
if [ "$#" -eq 0 ]; then set -- :app:assembleDebug :app:testDebugUnitTest; fi
JAVA_HOME="$android_chat_jdk" ANDROID_HOME="$android_chat_sdk" ANDROID_SDK_ROOT="$android_chat_sdk" \
    GRADLE_USER_HOME="$android_chat_cache" \
    exec ./gradlew --no-daemon "$@"
