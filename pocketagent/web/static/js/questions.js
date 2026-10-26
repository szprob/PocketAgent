// ask_user 的问题卡片：每个问题一组选项（单选或多选），最后一项是「其他」可以自己写；提交或跳过。

import { button, el } from "./dom.js";

const OTHER_VALUE = "__other__";

let groupCounter = 0;

// onSubmit(answers)：answers 是每个问题的 {selected: [...], other: "..."}；跳过时传 null
export function renderQuestionCard(request, onSubmit) {
  const card = el("div", "question-card");
  card.appendChild(el("div", "question-card-title", request.questions.length > 1 ? `有 ${request.questions.length} 个问题想问你` : "有个问题想问你"));
  const readers = request.questions.map((question) => {
    const block = renderQuestion(question);
    card.appendChild(block.element);
    return block.read;
  });

  const actions = el("div", "approval-actions");
  const submit = button("primary-button", "提交");
  const skip = button("ghost-button", "跳过，让它自己判断");
  const finish = (answers) => {
    actions.querySelectorAll("button").forEach((each) => (each.disabled = true));
    card.querySelectorAll("input, textarea").forEach((each) => (each.disabled = true));
    onSubmit(answers);
  };
  submit.addEventListener("click", () => finish(readers.map((read) => read())));
  skip.addEventListener("click", () => finish(null));
  actions.append(submit, skip);
  card.appendChild(actions);
  // 第一个选项拿到焦点，键盘也能直接选
  queueMicrotask(() => card.querySelector("input")?.focus());
  return card;
}

function renderQuestion(question) {
  groupCounter += 1;
  const groupName = `question-${groupCounter}`;
  const inputType = question.multi_select ? "checkbox" : "radio";
  const element = el("fieldset", "question-block");
  const legend = el("legend", "question-text");
  if (question.header) {
    legend.appendChild(el("span", "question-header", question.header));
  }
  legend.appendChild(document.createTextNode(question.question));
  // 模型自己在问题里写了「多选」就不重复提示
  if (question.multi_select && !question.question.includes("多选")) {
    legend.appendChild(el("span", "question-hint", "可多选"));
  }
  element.appendChild(legend);

  for (const option of question.options) {
    element.appendChild(optionRow(groupName, inputType, option.label, option.label, option.description));
  }
  const otherRow = optionRow(groupName, inputType, OTHER_VALUE, "其他", "");
  const otherText = el("textarea", "question-other");
  otherText.rows = 1;
  otherText.placeholder = "自己写…";
  const otherInput = otherRow.querySelector("input");
  // 在「其他」里打字就自动选上它
  otherText.addEventListener("input", () => {
    if (otherText.value.trim() && !otherInput.checked) {
      otherInput.checked = true;
    }
  });
  otherInput.addEventListener("change", () => {
    if (otherInput.checked) {
      otherText.focus();
    }
  });
  otherRow.querySelector(".question-option-text").appendChild(otherText);
  element.appendChild(otherRow);

  const read = () => {
    const checked = [...element.querySelectorAll(`input[name="${groupName}"]:checked`)].map((input) => input.value);
    return {
      selected: checked.filter((value) => value !== OTHER_VALUE),
      other: checked.includes(OTHER_VALUE) ? otherText.value.trim() : "",
    };
  };
  return { element, read };
}

function optionRow(groupName, inputType, value, label, description) {
  const row = el("label", "question-option");
  const input = document.createElement("input");
  input.type = inputType;
  input.name = groupName;
  input.value = value;
  const text = el("span", "question-option-text");
  text.appendChild(el("span", "question-option-label", label));
  if (description) {
    text.appendChild(el("span", "question-option-description", description));
  }
  row.append(input, text);
  return row;
}
