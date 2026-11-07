package com.youdekan.reader

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.WebView
import android.widget.LinearLayout
import android.widget.TextView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.net.HttpURLConnection
import java.net.ServerSocket
import java.net.URL

/**
 * WebView 壳：启动内嵌 Python 后端（uvicorn 监听 127.0.0.1），健康检查通过后加载页面。
 * 启动时序复刻桌面端 desktop/src-tauri/src/lib.rs。
 */
class MainActivity : Activity() {

    private lateinit var statusText: TextView
    private lateinit var webView: WebView
    @Volatile private var backendThread: Thread? = null
    private var port: Int = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        statusText = TextView(this).apply {
            text = "正在启动本地服务…"
            val padding = (16 * resources.displayMetrics.density).toInt()
            setPadding(padding, padding, padding, padding)
        }
        webView = WebView(this).apply {
            visibility = View.GONE
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
        }
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(statusText, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(webView, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        }
        setContentView(layout)

        // Chaquopy 要求整个进程只启动一次 Python
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        val python = Python.getInstance()

        // 先占用一个空闲端口再释放，交给后端绑定
        port = ServerSocket(0).use { it.localPort }

        // start() 会一直阻塞到服务退出
        val serverThread = Thread {
            try {
                python.getModule("android_entry").callAttr("start", filesDir.absolutePath, port)
            } catch (e: Exception) {
                Log.e(TAG, "内置服务异常退出", e)
            }
        }
        serverThread.isDaemon = true
        serverThread.start()
        backendThread = serverThread

        val pollThread = Thread { waitForBackend() }
        pollThread.isDaemon = true
        pollThread.start()
    }

    private fun waitForBackend() {
        val deadline = System.currentTimeMillis() + HEALTH_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (healthy()) {
                runOnUiThread {
                    statusText.visibility = View.GONE
                    webView.visibility = View.VISIBLE
                    webView.loadUrl("http://127.0.0.1:$port/")
                }
                return
            }
            try {
                Thread.sleep(HEALTH_INTERVAL_MS)
            } catch (_: InterruptedException) {
                return
            }
        }
        runOnUiThread { statusText.text = "内置服务未能完成启动" }
    }

    private fun healthy(): Boolean {
        return try {
            val conn = URL("http://127.0.0.1:$port/health").openConnection() as HttpURLConnection
            try {
                conn.connectTimeout = 1000
                conn.readTimeout = 1000
                conn.responseCode == 200 &&
                    conn.inputStream.bufferedReader().readText().contains("\"status\":\"ok\"")
            } finally {
                conn.disconnect()
            }
        } catch (_: Exception) {
            false
        }
    }

    override fun onDestroy() {
        if (Python.isStarted()) {
            try {
                Python.getInstance().getModule("android_entry").callAttr("stop")
            } catch (e: Exception) {
                Log.w(TAG, "停止内置服务失败", e)
            }
        }
        try {
            backendThread?.join(3000)
        } catch (_: InterruptedException) {
        }
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MainActivity"
        private const val HEALTH_INTERVAL_MS = 300L
        private const val HEALTH_TIMEOUT_MS = 60_000L
    }
}
