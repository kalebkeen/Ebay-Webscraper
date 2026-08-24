plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    // Must come after the Android plugin.
    id("com.chaquo.python")
}

android {
    namespace = "com.spine.ps2"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.spine.ps2"
        minSdk = 24          // Chaquopy needs 21+; 24 for modern WebView behaviour
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        ndk {
            // Every ABI you list multiplies the size of the bundled CPython.
            // These two cover essentially all real Android hardware; add
            // x86_64 only if you run it on an emulator.
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false   // R8 cannot see through Chaquopy's reflection
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

chaquopy {
    defaultConfig {
        // 3.12 is well supported and the pipeline is stdlib-only, so package
        // availability is not a constraint here.
        version = "3.12"

        // NOTHING GOES HERE. numpy and rapidfuzz were removed precisely so
        // this list could stay empty -- every entry is a wheel that has to
        // exist for Android, and a missing one fails the build.
        //
        // pip { install("some-pure-python-package") }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.12.1")

    // Native barcode scanning.
    //
    // The web client reaches for BarcodeDetector, which is a Chrome API that
    // is NOT reliably exposed inside Android's WebView -- and when it is
    // missing the scan button reports "this browser cannot scan barcodes"
    // from inside your own app, which is the worst possible failure.
    //
    // play-services-code-scanner provides the entire scanning UI in one
    // call: no CameraX, no preview surface, no permission plumbing. It
    // downloads its module on demand via Play Services, so it adds
    // essentially nothing to the APK.
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
