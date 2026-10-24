#!/bin/sh
set -eu
android_node_project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
android_node_jdk=${ANDROID_NODE_JAVA_HOME:-${JAVA_HOME:-}}
if [ -z "$android_node_jdk" ] && [ -x /usr/lib/jvm/java-21-openjdk-amd64/bin/java ]; then
    android_node_jdk=/usr/lib/jvm/java-21-openjdk-amd64
fi
if [ -z "$android_node_jdk" ]; then
    printf '%s\n' 'Set ANDROID_NODE_JAVA_HOME to a JDK 17 or newer.' >&2
    exit 1
fi
android_node_cache=${ANDROID_NODE_GRADLE_CACHE:-${TMPDIR:-/tmp}/virtual-life-android-node-gradle}
cd "$android_node_project_dir"
if [ "$#" -eq 0 ]; then set -- build; fi
JAVA_HOME="$android_node_jdk" GRADLE_USER_HOME="$android_node_cache" \
    exec ./gradlew --no-daemon "$@"
