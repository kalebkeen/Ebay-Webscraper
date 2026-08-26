package com.spine.ps2

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.view.ViewGroup
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.JavascriptInterface
import android.webkit.WebViewClient
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/**
 * Boots the Python pipeline, starts its loopback HTTP server, and shows the
 * existing web client against it.
 *
 * Why a WebView over a loopback server rather than a native UI calling
 * Python directly: the client already exists, works, and is the thing that
 * has been tested. Rewriting it in Compose would mean maintaining two
 * front-ends for one tool, and the JSON boundary keeps the Kotlin side
 * thin enough to be obviously correct.
 *
 * Python starts on a background thread. AndroidPlatform unpacks the
 * interpreter and the stdlib on first launch, which takes a second or two
 * on a cold install, and doing that on the main thread would trip the ANR
 * watchdog on slower hardware.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var status: TextView
    private var port: Int = 0

    // Pending <input type="file"> callback + the picker launcher. Registered as
    // a field so it is ready before the activity starts. Storage Access
    // Framework grants access to the picked file, so no media permission is
    // needed for gallery selection.
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val uris = WebChromeClient.FileChooserParams.parseResult(
            result.resultCode, result.data
        )
        filePathCallback?.onReceiveValue(uris)
        filePathCallback = null
    }

    companion object {
        private const val TAG = "Spine"
        private const val CAMERA_REQUEST = 101
    }

    /**
     * Native barcode scanning, exposed to the web client.
     *
     * The client's first choice is BarcodeDetector, a Chrome API that is not
     * reliably present in Android's WebView. When it is absent the scan
     * button reports that the browser cannot scan -- inside our own app.
     * This bridge is the dependable path, and bridge.js prefers it.
     *
     * Asynchronous by necessity: the scanner is a separate activity. JS gets
     * a Promise, resolved when the result is pushed back in.
     */
    inner class NativeScanner {
        @JavascriptInterface
        fun isAvailable(): Boolean = true

        @JavascriptInterface
        fun scan() {
            runOnUiThread {
                val options = GmsBarcodeScannerOptions.Builder()
                    .setBarcodeFormats(
                        Barcode.FORMAT_UPC_A, Barcode.FORMAT_UPC_E,
                        Barcode.FORMAT_EAN_13, Barcode.FORMAT_EAN_8
                    )
                    .enableAutoZoom()
                    .build()
                GmsBarcodeScanning.getClient(this@MainActivity, options)
                    .startScan()
                    .addOnSuccessListener { code ->
                        deliver(code.rawValue ?: "", null)
                    }
                    .addOnCanceledListener { deliver(null, "cancelled") }
                    .addOnFailureListener { e ->
                        Log.e(TAG, "scan failed", e)
                        deliver(null, e.message ?: "scan failed")
                    }
            }
        }

        private fun deliver(value: String?, error: String?) {
            val payload = if (value != null)
                "{\"code\":${quote(value)}}"
            else
                "{\"error\":${quote(error ?: "unknown")}}"
            runOnUiThread {
                webView.evaluateJavascript(
                    "window.__spineScanResult && window.__spineScanResult($payload)",
                    null
                )
            }
        }

        private fun quote(s: String): String =
            "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        status = TextView(this).apply {
            text = "Starting Python…"
            textSize = 14f
            setPadding(48, 96, 48, 48)
            setTextColor(0xFF9C8FD6.toInt())
        }
        setContentView(status)

        // The scan button needs this. Asking up front rather than at the
        // moment of use avoids a permission dialog appearing over the
        // camera preview, which reads as a crash.
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.CAMERA), CAMERA_REQUEST
            )
        }

        thread(name = "python-boot") {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this))
                }
                val py = Python.getInstance()
                val module = py.getModule("android_main")
                port = module.callAttr("start").toInt()
                Log.i(TAG, "python server listening on 127.0.0.1:$port")
                runOnUiThread { showWebView() }
            } catch (e: Exception) {
                Log.e(TAG, "python failed to start", e)
                runOnUiThread {
                    status.text = "Python failed to start:\n\n${e.message}\n\n" +
                            "Check Logcat, filter on \"$TAG\"."
                }
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun showWebView() {
        webView = WebView(this).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
            setBackgroundColor(0xFF120C24.toInt())

            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true      // the client caches state here
                databaseEnabled = true
                mediaPlaybackRequiresUserGesture = false   // camera without a tap
                useWideViewPort = true
                loadWithOverviewMode = true
            }

            webViewClient = object : WebViewClient() {
                override fun onReceivedError(
                    view: WebView?, request: android.webkit.WebResourceRequest?,
                    error: android.webkit.WebResourceError?
                ) {
                    Log.e(TAG, "webview error: ${error?.description}")
                }
            }

            // getUserMedia inside a WebView is denied unless the host app
            // grants it explicitly, even with CAMERA held. Without this the
            // scan button fails silently, which is a miserable thing to debug.
            webChromeClient = object : WebChromeClient() {
                override fun onPermissionRequest(request: PermissionRequest) {
                    runOnUiThread {
                        val wanted = request.resources.filter {
                            it == PermissionRequest.RESOURCE_VIDEO_CAPTURE
                        }.toTypedArray()
                        if (wanted.isNotEmpty() &&
                            ContextCompat.checkSelfPermission(
                                this@MainActivity, Manifest.permission.CAMERA
                            ) == PackageManager.PERMISSION_GRANTED
                        ) {
                            request.grant(wanted)
                        } else {
                            request.deny()
                        }
                    }
                }

                override fun onConsoleMessage(
                    msg: android.webkit.ConsoleMessage?
                ): Boolean {
                    Log.d(TAG, "console: ${msg?.message()} @${msg?.lineNumber()}")
                    return true
                }

                // Makes <input type="file"> work — the photo-identify buttons.
                // createIntent() honours accept="image/*"; with no `capture`
                // attribute the system chooser offers camera AND gallery.
                override fun onShowFileChooser(
                    view: WebView?,
                    callback: ValueCallback<Array<Uri>>?,
                    params: FileChooserParams?
                ): Boolean {
                    val intent = params?.createIntent() ?: return false
                    filePathCallback?.onReceiveValue(null)   // cancel a stale one
                    filePathCallback = callback
                    return try {
                        fileChooserLauncher.launch(intent)
                        true
                    } catch (e: Exception) {
                        Log.e(TAG, "file chooser failed", e)
                        filePathCallback = null
                        false
                    }
                }
            }
        }

        webView.addJavascriptInterface(NativeScanner(), "SpineNative")

        setContentView(webView)
        webView.loadUrl("http://127.0.0.1:$port/")

        onBackPressedDispatcher.addCallback(this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (webView.canGoBack()) webView.goBack() else finish()
                }
            })
    }
}
