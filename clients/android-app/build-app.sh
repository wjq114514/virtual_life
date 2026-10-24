#!/bin/sh
set -eu
android_app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
android_app_jdk=${ANDROID_APP_JAVA_HOME:-${JAVA_HOME:-}}
if [ -z "$android_app_jdk" ] && [ -x /usr/lib/jvm/java-21-openjdk-amd64/bin/java ]; then
    android_app_jdk=/usr/lib/jvm/java-21-openjdk-amd64
fi
if [ -z "$android_app_jdk" ]; then
    printf '%s\n' 'Set ANDROID_APP_JAVA_HOME to a JDK 17 or newer.' >&2
    exit 1
fi
android_app_sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/android-sdk}}
if [ ! -d "$android_app_sdk/platforms" ]; then
    printf 'Android SDK not found at %s (set ANDROID_HOME).\n' "$android_app_sdk" >&2
    exit 1
fi
android_app_cache=${ANDROID_APP_GRADLE_CACHE:-${TMPDIR:-/tmp}/virtual-life-android-node-gradle}
cd "$android_app_dir"
if [ "$#" -eq 0 ]; then set -- :app:assembleDebug :app:testDebugUnitTest; fi
JAVA_HOME="$android_app_jdk" ANDROID_HOME="$android_app_sdk" ANDROID_SDK_ROOT="$android_app_sdk" \
    GRADLE_USER_HOME="$android_app_cache" \
    exec ./gradlew --no-daemon "$@"
