"""桌面端连接层（desktop/renderer/api.js 的 SessionSocket）：断线和「看着还连着其实已经断了」都不能丢消息。

用 Node 跑，WebSocket 换成假的：能控制什么时候算连上、服务端推什么、以及最要命的那种——
readyState 还是 OPEN，但对面其实早就没了（合盖休眠、代理超时、后端重启）。
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r"""
class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.CONNECTING;
    this.listeners = new Map();
    this.sent = [];
    FakeSocket.instances.push(this);
    setTimeout(() => this.open(), 0);
  }

  open() {
    this.readyState = FakeSocket.OPEN;
    this.fire("open", {});
  }

  fire(type, event) {
    for (const handler of this.listeners.get(type) || []) {
      handler(event);
    }
  }

  addEventListener(type, handler) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), handler]);
  }

  receive(payload) {
    this.fire("message", { data: JSON.stringify(payload) });
  }

  send(raw) {
    if (this.readyState !== FakeSocket.OPEN) {
      throw new Error("连接已经关了");
    }
    this.sent.push(JSON.parse(raw));
  }

  close() {
    this.readyState = FakeSocket.CLOSED;
    this.fire("close", {});
  }
}

globalThis.WebSocket = FakeSocket;
globalThis.window = { addEventListener() {}, removeEventListener() {} };
globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const api = await import("./desktop/renderer/api.js");

const events = [];
const socket = new api.SessionSocket("s1", {
  onMessage: (message) => events.push(["message", message.type]),
  onOpen: () => events.push(["open"]),
  onClose: () => events.push(["close"]),
});
socket.connect({ baseUrl: "http://127.0.0.1:8765", token: "t" });

// 1. 还没连上就发：不报错，先攒着，连上自动补发
const whileConnecting = socket.send({ type: "user_message", text: "一" });
const sentBeforeOpen = FakeSocket.instances[0].sent.length;
await sleep(20);
const firstSocket = FakeSocket.instances[0];
const flushed = firstSocket.sent.map((message) => message.type);
const clientId = firstSocket.sent[0].client_id;

// 2. 后端 ack 之后才把它从待发送里拿掉
const outboxBeforeAck = socket.outbox.size;
firstSocket.receive({ type: "ack", client_id: clientId });
const outboxAfterAck = socket.outbox.size;

// 3. 连接"看着还是 OPEN"其实对面没了：往过去挪一分钟，什么都没收到，判活要主动重连
const realNow = Date.now;
Date.now = () => realNow() + 60000;
socket.checkAlive();
Date.now = realNow;
const closesAfterDeath = events.filter((event) => event[0] === "close").length;

// 4. 判死之后发的消息不能丢：留在 outbox 里，重连后补发
const whileDead = socket.send({ type: "user_message", text: "断线时发的" });
await sleep(800);
const reconnected = FakeSocket.instances.length;
const resent = FakeSocket.instances.at(-1).sent.map((message) => message.text);
FakeSocket.instances.at(-1).receive({ type: "ack", client_id: FakeSocket.instances.at(-1).sent[0].client_id });
const outboxAfterResendAck = socket.outbox.size;

// 5. 正常关掉（切任务）时把还没 ack 的原样交回来
socket.send({ type: "user_message", text: "还没确认的" });
const unsent = socket.close().map((message) => message.text);

// 6. 老后端：snapshot 里没有 features，不会 ack，消息不能一直留在 outbox 里（不然重连后重复发）
const old = new api.SessionSocket("s2", { onMessage() {}, onOpen() {}, onClose() {} });
old.connect({ baseUrl: "http://127.0.0.1:8765", token: "t" });
await sleep(20);
const oldSocket = FakeSocket.instances.at(-1);
old.send({ type: "user_message", text: "老后端" });
const oldOutboxBeforeSnapshot = old.outbox.size;
oldSocket.receive({ type: "snapshot", session_id: "s2" });
const oldOutboxAfterSnapshot = old.outbox.size;
old.close();

process.stdout.write(
  JSON.stringify({
    whileConnecting,
    sentBeforeOpen,
    flushed,
    hasClientId: typeof clientId === "string" && clientId.length > 0,
    outboxBeforeAck,
    outboxAfterAck,
    closesAfterDeath,
    whileDead,
    reconnected,
    resent,
    outboxAfterResendAck,
    unsent,
    oldOutboxBeforeSnapshot,
    oldOutboxAfterSnapshot,
  })
);
"""


class FrontendSocketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("没装 Node，跳过前端连接层测试")

    def run_script(self) -> dict:
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", SCRIPT],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_messages_survive_a_dead_or_half_dead_connection(self) -> None:
        results = self.run_script()

        # 没连上就发：返回 false（还在队列里），一条都不该发出去
        self.assertFalse(results["whileConnecting"])
        self.assertEqual(results["sentBeforeOpen"], 0)
        # 连上之后自动补发，并且带上 client_id（后端靠它去重）
        self.assertEqual(results["flushed"], ["user_message"])
        self.assertTrue(results["hasClientId"])
        # ack 前留在 outbox，ack 后清掉
        self.assertEqual(results["outboxBeforeAck"], 1)
        self.assertEqual(results["outboxAfterAck"], 0)
        # 半死的连接要自己判死、主动重连
        self.assertEqual(results["closesAfterDeath"], 1)
        # 判死之后发的那条：先别发（返回 false），重连后补发出去
        self.assertFalse(results["whileDead"])
        self.assertEqual(results["reconnected"], 2)
        self.assertEqual(results["resent"], ["断线时发的"])
        self.assertEqual(results["outboxAfterResendAck"], 0)
        # 关掉连接时把没确认的交回来，别让用户白打
        self.assertEqual(results["unsent"], ["还没确认的"])
        # 老后端：先留着（还不知道对方支不支持 ack），看到没有 features 的 snapshot 就清掉
        self.assertEqual(results["oldOutboxBeforeSnapshot"], 1)
        self.assertEqual(results["oldOutboxAfterSnapshot"], 0)


if __name__ == "__main__":
    unittest.main()
