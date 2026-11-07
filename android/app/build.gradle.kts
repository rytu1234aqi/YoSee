plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

// pyproject.toml is the single source of truth for the release version.
val appVersion = rootProject.projectDir.parentFile.resolve("pyproject.toml")
    .useLines { lines ->
        lines.first { it.trim().startsWith("version = ") }
            .substringAfter('"').substringBefore('"')
    }

android {
    namespace = "com.youdekan.reader"
    // 本机 SDK 只有 android-36.1 平台；AGP 9.x 的 minor SDK DSL 直接编译到 36.1
    compileSdk {
        version = release(36) {
            minorApiLevel = 1
        }
    }

    defaultConfig {
        applicationId = "com.youdekan.reader"
        minSdk = 24
        targetSdk = 36
        // Increment on every published build; a fixed code blocks in-place upgrades.
        versionCode = 2
        versionName = appVersion

        ndk {
            // lxml 的 Chaquopy 官方 wheel 只提供这两个 ABI
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}

// Chaquopy 17 的 DSL：顶层 chaquopy 块（android.defaultConfig.python 是已废弃的旧写法）
chaquopy {
    defaultConfig {
        version = "3.13"
        // 保留 .py 源文件：alembic 用 importlib 从文件系统加载 versions/*.py 迁移脚本，
        // 默认的 pyc 字节码编译会把 .py 从 APK 里剔除，导致迁移脚本目录为空。
        pyc {
            src = false
        }
        // server 是含 __init__.py 的顶层包，其数据文件（migrations/script.py.mako 等）
        // 只在包被导入时才解包；extractPackages 保证整个包在文件系统上可见，
        // 让 alembic 的 script_location（%(here)s/server/database/migrations）可用。
        extractPackages("server")
        pip {
            install("fastapi==0.142.2")
            install("pydantic==2.13.5")
            install("pydantic-core==2.46.5")
            // Chaquopy 仓库对 cp313 只提供 lxml 5.3.0；项目里 lxml 仅作 BeautifulSoup 的解析后端
            install("lxml==5.3.0")
            install("uvicorn==0.54.0")
            install("httpx==0.28.1")
            install("feedparser==6.0.14")
            install("beautifulsoup4==4.15.0")
            install("sqlalchemy==2.1.1")
            install("alembic==1.20.0")
            install("pyyaml==6.0.3")
            // pydantic-core 的 Android wheel 由并行任务编译，产出到本目录
            options("--find-links", "$rootDir/pip-repo")
        }
    }
}

// The Python backend and web assets live at the repository root. Previously a
// developer had to run sync_python.sh by hand, and the copied directories were
// gitignored, so a clean clone could silently ship stale code. Every build now
// refreshes them and Gradle tracks the inputs for incremental builds.
val sharedPythonIncludes = listOf("server/**", "web/**", "configs/**", "alembic.ini")
val sharedPythonExcludes = listOf("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**")

val syncSharedPython by tasks.registering {
    val repoRoot = rootProject.projectDir.parentFile
    val dest = File(projectDir, "src/main/python")
    inputs.files(fileTree(repoRoot) {
        include(sharedPythonIncludes)
        exclude(sharedPythonExcludes)
    })
    outputs.dir(dest)
    doLast {
        listOf("server", "web", "configs").forEach { name ->
            project.delete(File(dest, name))
        }
        project.copy {
            from(repoRoot) {
                include(sharedPythonIncludes)
                exclude(sharedPythonExcludes)
            }
            into(dest)
        }
    }
}

tasks.named("preBuild") { dependsOn(syncSharedPython) }
