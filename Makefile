PYTHON ?= python

.PHONY: test test-live test-all run desktop desktop-dist apk

# 快速、不联网：假模型 + 假服务器，改代码和加功能后都跑这一套
test:
	$(PYTHON) -m unittest discover -s tests/mock -t .
	$(PYTHON) -m unittest discover -s tests/vendor_pocketflow -t .

# 真实 API 联调：默认用配置里的免费模型，没有可用配置就全部跳过
test-live:
	POCKETAGENT_LIVE=1 $(PYTHON) -m unittest discover -s tests/live -t .

test-all: test test-live

run:
	$(PYTHON) -m pocketagent

# 桌面客户端（desktop/ 里是独立的 Electron 前端；第一次跑会先装依赖）
desktop:
	cd desktop && [ -d node_modules ] || npm install --no-audit --no-fund
	cd desktop && npm start

# 打桌面端安装包（当前系统的：macOS 出 dmg + zip，Windows 出 nsis 安装器，Linux 出 AppImage），产物在 desktop/dist/。
# 包里只有界面；后端还是用户自己装的 pocketagent（见 README「桌面端」）
desktop-dist:
	cd desktop && [ -d node_modules ] || npm install --no-audit --no-fund
	cd desktop && npm run dist

# Android 单 APK（内置 Alpine + PRoot，见 android/README.md）；需要 JDK 17+ 和 Android SDK 34
apk:
	@SDK="$${ANDROID_HOME:-$$HOME/tools/android/sdk}"; \
	if [ -d "$$SDK" ]; then export ANDROID_HOME="$$SDK"; fi; \
	JDK="$${JAVA_HOME:-$$HOME/tools/android/jdk-17/Contents/Home}"; \
	if [ -d "$$JDK" ]; then export JAVA_HOME="$$JDK"; fi; \
	if [ -n "$$SDK" ] && [ -d "$$SDK" ] && [ ! -f android/local.properties ]; then \
		echo "sdk.dir=$$SDK" > android/local.properties; \
	fi; \
	cd android && ./gradlew :app:assembleDebug
	@echo "APK: android/app/build/outputs/apk/debug/app-debug.apk"
