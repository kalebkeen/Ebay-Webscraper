pluginManagement {
    repositories {
        google()
        mavenCentral()   // Chaquopy is published here; the build fails without it
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "Spine"
include(":app")
