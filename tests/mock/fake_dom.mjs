// Node 里跑前端共享模块用的极简 DOM：只实现 shared/*.js 和 KaTeX 的 render 用到的接口，
// 再加一个 toHtml 把节点树序列化成字符串，测试按字符串断言。不追求和浏览器完全一致，
// 够用就行；共享模块用了新的 DOM 接口、这里没有时测试会直接报错，到时候在这里补上。

const VOID_TAGS = new Set(["br", "hr", "img", "input"]);
// 浏览器里这些属性赋值会反映到 HTML 属性上，序列化时要看得见
const REFLECTED_PROPERTIES = ["href", "target", "rel", "type", "alt", "loading", "src", "title", "value"];
const BOOLEAN_PROPERTIES = ["checked", "disabled"];

class FakeNode {
  constructor() {
    this.parentNode = null;
  }

  remove() {
    this.parentNode?.removeChild(this);
  }
}

class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.nodeType = 3;
    this.data = String(text);
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  names() {
    return this.element.className.split(/\s+/).filter(Boolean);
  }

  contains(name) {
    return this.names().includes(name);
  }

  add(...names) {
    const merged = new Set([...this.names(), ...names]);
    this.element.className = [...merged].join(" ");
  }

  remove(...names) {
    this.element.className = this.names()
      .filter((name) => !names.includes(name))
      .join(" ");
  }

  toggle(name, force) {
    const shouldHave = force === undefined ? !this.contains(name) : Boolean(force);
    if (shouldHave) {
      this.add(name);
    } else {
      this.remove(name);
    }
    return shouldHave;
  }
}

class FakeParent extends FakeNode {
  constructor() {
    super();
    this.childNodes = [];
  }

  get firstChild() {
    return this.childNodes[0] ?? null;
  }

  get children() {
    return this.childNodes.filter((child) => child.nodeType === 1);
  }

  appendChild(child) {
    if (child instanceof FakeFragment) {
      for (const grandChild of [...child.childNodes]) {
        this.appendChild(grandChild);
      }
      return child;
    }
    child.remove();
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  append(...children) {
    for (const child of children) {
      this.appendChild(typeof child === "string" ? new FakeText(child) : child);
    }
  }

  replaceChildren(...children) {
    for (const child of this.childNodes) {
      child.parentNode = null;
    }
    this.childNodes = [];
    this.append(...children);
  }

  removeChild(child) {
    this.childNodes = this.childNodes.filter((existing) => existing !== child);
    child.parentNode = null;
    return child;
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.replaceChildren();
    if (value !== "" && value !== null && value !== undefined) {
      this.appendChild(new FakeText(value));
    }
  }
}

class FakeFragment extends FakeParent {
  constructor() {
    super();
    this.nodeType = 11;
  }
}

class FakeElement extends FakeParent {
  constructor(tagName) {
    super();
    this.nodeType = 1;
    this.tagName = tagName.toLowerCase();
    this.attributes = new Map();
    this.style = {};
    this.dataset = {};
    this.listeners = new Map();
    this.classList = new FakeClassList(this);
  }

  get className() {
    return this.attributes.get("class") ?? "";
  }

  set className(value) {
    this.attributes.set("class", String(value));
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type).push(handler);
  }

  // 测试里模拟点击：按顺序调监听函数，返回它们的结果（async 监听返回 Promise）
  dispatch(type, event = {}) {
    const results = [];
    for (const handler of this.listeners.get(type) || []) {
      results.push(handler({ preventDefault() {}, target: this, ...event }));
    }
    return Promise.all(results);
  }
}

for (const property of REFLECTED_PROPERTIES) {
  Object.defineProperty(FakeElement.prototype, property, {
    get() {
      return this.attributes.get(property) ?? "";
    },
    set(value) {
      this.attributes.set(property, String(value));
    },
  });
}

for (const property of BOOLEAN_PROPERTIES) {
  Object.defineProperty(FakeElement.prototype, property, {
    get() {
      return this.attributes.has(property);
    },
    set(value) {
      if (value) {
        this.attributes.set(property, "");
      } else {
        this.attributes.delete(property);
      }
    },
  });
}

function escapeText(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeAttribute(text) {
  return escapeText(text).replace(/"/g, "&quot;");
}

function kebabCase(name) {
  return name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

function attributeText(element) {
  const attributes = new Map(element.attributes);
  const styleText = Object.entries(element.style)
    .filter(([, value]) => value !== "" && value !== undefined)
    .map(([name, value]) => `${kebabCase(name)}:${value}`)
    .join(";");
  if (styleText) {
    attributes.set("style", styleText);
  }
  return [...attributes]
    .map(([name, value]) => (value === "" && BOOLEAN_PROPERTIES.includes(name) ? ` ${name}` : ` ${name}="${escapeAttribute(value)}"`))
    .join("");
}

export function toHtml(node) {
  if (node.nodeType === 3) {
    return escapeText(node.data);
  }
  const inner = node.childNodes.map(toHtml).join("");
  if (node.nodeType === 11) {
    return inner;
  }
  const open = `<${node.tagName}${attributeText(node)}>`;
  return VOID_TAGS.has(node.tagName) ? open : `${open}${inner}</${node.tagName}>`;
}

export function installFakeDom() {
  const body = new FakeElement("body");
  globalThis.document = {
    // KaTeX 发现不是标准模式会打警告
    compatMode: "CSS1Compat",
    body,
    documentElement: new FakeElement("html"),
    createElement: (tagName) => new FakeElement(tagName),
    createElementNS: (namespace, tagName) => new FakeElement(tagName),
    createTextNode: (text) => new FakeText(text),
    createDocumentFragment: () => new FakeFragment(),
    getElementById: () => null,
    addEventListener() {},
    execCommand: () => false,
  };
  return globalThis.document;
}
