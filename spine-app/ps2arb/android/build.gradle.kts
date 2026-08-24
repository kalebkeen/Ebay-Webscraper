// Chaquopy 17 requires the Android Gradle plugin to be between 7.3.x and 9.2.x.
// 8.7.3 sits comfortably inside that range; bump it only after checking
// https://chaquo.com/chaquopy/doc/current/changelog.html for the supported span.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("com.chaquo.python") version "17.0.0" apply false
}
