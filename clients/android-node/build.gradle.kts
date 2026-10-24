import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm") version "2.0.21"
    kotlin("plugin.serialization") version "2.0.21"
    `java-library`
    `maven-publish`
}

group = "dev.virtuallife"
version = "0.1.0"

repositories { mavenCentral() }

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_1_8)
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_1_8
    targetCompatibility = JavaVersion.VERSION_1_8
    withSourcesJar()
}

dependencies {
    api("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.9.0")
    api("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    api("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-datetime:0.6.1")
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}

val example by sourceSets.creating
configurations[example.implementationConfigurationName].extendsFrom(configurations.implementation.get())
example.compileClasspath += sourceSets.main.get().output
example.runtimeClasspath += sourceSets.main.get().output

tasks.register<JavaExec>("nodeExample") {
    description = "Connect a JVM console host to a separately running runtime API"
    group = "application"
    classpath = example.runtimeClasspath
    mainClass.set("dev.virtuallife.node.NodeExampleKt")
}

publishing {
    publications {
        create<MavenPublication>("node") { from(components["java"]) }
    }
}
