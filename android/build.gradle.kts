// 版本组合选择理由：
// - AGP 9.0.1：AGP 9.x 起支持 minor SDK 版本（compileSdk { version = release(36) { minorApiLevel = 1 } }），
//   可直接使用本机仅有的 android-36.1 平台；AGP 9.0 起内置 Kotlin（KGP 2.2.10），无需再声明
//   org.jetbrains.kotlin.android 插件；AGP 9.0 要求 JDK 17+（本机用 Android Studio 自带 JBR 21 运行）。
// - Gradle 9.1.0：官方对应表中 AGP 9.0 要求的最低 Gradle 版本（9.1→9.3.1、9.2→9.4.1，
//   选 9.0.x 可停留在 Gradle 9.1.0，避开 Gradle 9.2/9.3 在 Windows 上的已知缓存问题）。
// - Chaquopy 17.1.0：当前版本（chaquo.com/chaquopy/doc/current/android.html），官方声明支持
//   AGP 7.3.x–9.4.x、Python 3.10–3.15（含 3.13）、最低 minSdk 24，完整覆盖上述组合。
//   注意 17.x 使用顶层 chaquopy {} DSL，而非旧的 android.defaultConfig.python {}。
plugins {
    id("com.android.application") version "9.0.1" apply false
    id("com.chaquo.python") version "17.1.0" apply false
}
