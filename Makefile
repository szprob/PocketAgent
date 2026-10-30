PYTHON ?= python

.PHONY: test test-live test-all run desktop

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
