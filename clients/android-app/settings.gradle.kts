pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "virtual-life-android-app"

include(":app")

// Include the JVM SDK as a composite build so its Kotlin JVM plugin stays isolated
// from the Android Kotlin plugin on this build's classpath.
includeBuild("../android-node")
