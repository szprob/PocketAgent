PYTHON ?= python

.PHONY: test run desktop

test:
	$(PYTHON) -m unittest discover -s tests -t .

run:
	$(PYTHON) -m pocketagent

# 桌面客户端（desktop/ 里是独立的 Electron 前端；第一次跑会先装依赖）
desktop:
	cd desktop && [ -d node_modules ] || npm install --no-audit --no-fund
	cd desktop && npm start
