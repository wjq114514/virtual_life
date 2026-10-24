#!/bin/sh
set -eu
vision_project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
vision_jdk=${ANDROID_VISION_JAVA_HOME:-${JAVA_HOME:-}}
if [ -z "$vision_jdk" ] && [ -x /usr/lib/jvm/java-21-openjdk-amd64/bin/java ]; then
    vision_jdk=/usr/lib/jvm/java-21-openjdk-amd64
fi
if [ -z "$vision_jdk" ]; then
    printf '%s\n' 'Set ANDROID_VISION_JAVA_HOME to JDK 17 or newer.' >&2
    exit 1
fi
vision_sdk=${ANDROID_VISION_SDK:-${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}}
if [ -z "$vision_sdk" ] && [ -d /tmp/virtual-life-android-sdk/platforms/android-35 ]; then
    vision_sdk=/tmp/virtual-life-android-sdk
fi
if [ -z "$vision_sdk" ]; then
    printf '%s\n' 'Set ANDROID_VISION_SDK to an Android SDK with platform 35, build-tools 35.0.0, NDK 27.2.12479018, and CMake 3.22.1.' >&2
    exit 1
fi
vision_cache=${ANDROID_VISION_GRADLE_CACHE:-${ANDROID_NODE_GRADLE_CACHE:-${TMPDIR:-/tmp}/virtual-life-android-node-gradle}}
if [ ! -f "$vision_project_dir/.deps/MNN/CMakeLists.txt" ]; then
    "$vision_project_dir/scripts/prepare-mnn.sh"
fi
if [ "$#" -eq 0 ]; then set -- :app:assembleDebug :app:testDebugUnitTest :app:lintDebug; fi
cd "$vision_project_dir"
JAVA_HOME="$vision_jdk" ANDROID_HOME="$vision_sdk" ANDROID_SDK_ROOT="$vision_sdk" \
    GRADLE_USER_HOME="$vision_cache" exec "$vision_project_dir/gradlew" --no-daemon "$@"
