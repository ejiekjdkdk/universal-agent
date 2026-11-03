"use strict";
const $ = (id) => document.getElementById(id);
const api = "/api/v1/agent";
const state = {
  scenes: [],
  scene: null,
  token: "",
  session: null,
  task: null,
  busy: false,
  running: false,
  poll: null,
  view: 0,
  historyRequest: 0
};

const labels = {
  task_started: "任务开始",
  planner_started: "启动规划器",
  tools_discovered: "发现可用工具",
  authorization: "权限校验",
  step_started: "开始执行工具",
  tool_input: "输入摘要",
  tool_result: "输出与耗时",
  step_completed: "工具完成",
  step_failed: "工具失败",
  tool_retry: "重试工具",
  knowledge_retrieved: "检索资料",
  message_delta: "生成回答",
  task_completed: "任务完成",
  task_cancelled: "已取消",
  task_failed: "任务失败",
  model_request: "调用模型",
  model_response: "收到模型响应",
  model_round: "模型规划",
  tool_selected: "选择工具",
  plan_created: "创建计划",
  plan_validated: "计划校验通过",
  step_queued: "步骤已就绪",
  step_leased: "工作进程已领取",
  step_running: "执行步骤",
  step_retrying: "重新尝试步骤",
  step_suspended: "步骤已挂起",
  step_skipped: "步骤已跳过",
  step_cancelled: "步骤已取消",
  task_partial: "任务部分完成",
  model_tool_call: "模型请求工具",
  provider_selected: "选择规划器",
  approval_required: "等待审批",
  approval_decided: "已记录审批决定",
  mock_commit_verified: "模拟账本已确认",
  mock_commit_unknown: "模拟提交待查证"
};
const statuses = {
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  running: "运行中",
  partial: "部分完成",
  waiting_approval: "等待审批",
  outcome_unknown: "结果待查证"
};
const stepStatuses = {
  ...statuses,
  blocked: "等待依赖",
  ready: "已就绪",
  leased: "已领取",
  succeeded: "已完成",
  skipped: "已跳过",
  suspended: "已挂起"
};

function element(tag, content, cls) {
  const node = document.createElement(tag);
  if (content !== undefined) {
    node.textContent = content;
  }
  if (cls) {
    node.className = cls;
  }
  return node;
}

function notice(message = "") {
  $("notice").textContent = message;
}

async function request(path, options = {}) {
  const result = await fetch(path, {
    ...options,
    headers: {
      Authorization: "Bearer " + state.token,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers
    }
  });
  if (!result.ok) {
    const body = await result.json().catch(() => ({}));
    throw Error(body.error?.message || ("请求失败 (" + result.status + ")"));
  }
  return result;
}

function showBusy(value) {
  state.busy = value;
  $("submit").disabled = value || !state.scene;
  const planned = $("executionMode").value === "durable_plan" && state.scene?.durable_plan;
  $("executionMode").disabled = value;
  $("mode").disabled = value || planned;
  $("mode").hidden = Boolean(planned);
  $("planHint").hidden = !planned;
  $("new").disabled = value;
  $("cancel").hidden = !state.running || !state.task;
  document.querySelectorAll(".scene,.past").forEach(node => {
    node.disabled = value;
  });
}

function beginView() {
  // 视图编号同时约束任务与计划请求，切换任务后的旧响应不能再改写页面。
  state.view++;
  clearTimeout(state.poll);
  state.running = false;
  $("cancel").hidden = true;
  $("planPanel").hidden = true;
  $("planSteps").replaceChildren();
  return state.view;
}

function resetView() {
  beginView();
  state.task = null;
  state.session = null;
  $("status").textContent = "等待运行";
  $("taskid").textContent = $("prompt").textContent = "";
  $("answer").textContent = "运行一个问题，就能在这里查看回答。";
  $("trace").replaceChildren();
  $("evidence").replaceChildren();
  notice();
}

function drawTrace(entry) {
  const item = element("li");
  const details = element("details");
  const data = entry.data || {};
  const title = data.step_id || data.capability_id || data.tool || data.resource || "";
  details.append(element("summary", (labels[entry.event] || entry.event) + (title ? " · " + title : "")));
  details.append(element("pre", JSON.stringify(data, null, 2)));
  const stamp = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : "";
  item.append(
    element("div", "#" + entry.id + "  " + stamp, "meta"),
    details
  );
  $("trace").append(item);
}

function showResult(run) {
  state.task = run.task_id;
  state.running = ["running", "waiting_approval"].includes(run.status);
  $("cancel").hidden = !state.running;
  state.session = run.session_id;
  $("taskid").textContent = run.task_id;
  $("status").textContent = statuses[run.status] || run.status;
  $("prompt").textContent = run.message ? "提问 · " + run.message : "";
  $("answer").textContent =
    run.answer || run.error?.message || (run.status === "cancelled" ? "本次运行已取消。" : "等待回答…");
  $("evidence").replaceChildren();
  for (const [i, row] of (run.evidence || []).entries()) {
    const details = element("details");
    details.append(
      element("summary", "[E" + (i + 1) + "] " + row.title),
      element("div", row.source_id + " · " + row.version, "meta"),
      element("p", row.snippet, "evidence-text")
    );
    $("evidence").append(details);
  }
}

function showPlan(plan) {
  const rows = plan.steps.map(step => {
    const row = element("li");
    const heading = element("div", undefined, "step-heading");
    heading.append(
      element("strong", step.step_id),
      element("span", stepStatuses[step.status] || step.status, "badge")
    );
    const deps = step.depends_on.length ? step.depends_on.join("、") : "无";
    const attempt = step.attempt_no ? "第 " + step.attempt_no + " 次尝试" : "尚未尝试";
    const phase = {proposal: "提案阶段", execution: "执行阶段"}[step.phase];
    const progress = phase ? phase + " · " + attempt : attempt;
    row.append(
      heading,
      element("div", step.capability_id + " · " + (step.required ? "必需" : "可选"), "meta"),
      element("div", "依赖：" + deps + " · " + progress, "step-detail")
    );
    if (step.error?.code) {
      row.append(element("div", step.error.code, "meta"));
    }
    return row;
  });
  $("planCount").textContent = "共 " + rows.length + " 步";
  $("planSteps").replaceChildren(...rows);
  $("planPanel").hidden = false;
}

async function loadPlan(id, view, live = false) {
  try {
    const plan = await (await request(api + "/tasks/" + encodeURIComponent(id) + "/plan")).json();
    if (view === state.view) {
      showPlan(plan);
    }
  } catch (error) {
    if (view === state.view) {
      notice(error.message);
    }
  }
  if (live && view === state.view && state.busy) {
    state.poll = setTimeout(() => loadPlan(id, view, true), 1000);
  }
}

async function loadTask(id, view) {
  const path = api + "/tasks/" + encodeURIComponent(id);
  try {
    const [run, trace] = await Promise.all([
      request(path).then(response => response.json()),
      request(path + "/trace").then(response => response.json())
    ]);
    if (view !== state.view) {
      return;
    }
    showResult(run);
    $("trace").replaceChildren();
    trace.events.forEach(drawTrace);
    if (run.execution_mode === "durable_plan") {
      await loadPlan(id, view);
      if (view === state.view && state.running) {
        state.poll = setTimeout(() => loadTask(id, view), 1000);
      }
    }
  } catch (error) {
    if (view === state.view) {
      notice(error.message);
    }
  }
}

function openTask(id) {
  resetView();
  $("answer").textContent = "正在读取任务…";
  return loadTask(id, state.view);
}

async function history() {
  const sid = state.scene?.id;
  const requestId = ++state.historyRequest;
  if (!sid) {
    return;
  }
  let tasks;
  try {
    ({ tasks } = await (await request(api + "/tasks?business_id=" + encodeURIComponent(sid))).json());
  } catch (error) {
    if (requestId === state.historyRequest && sid === state.scene?.id) {
      notice(error.message);
    }
    return;
  }
  if (sid !== state.scene?.id || requestId !== state.historyRequest) {
    return;
  }
  $("history").replaceChildren();
  for (const run of tasks) {
    const node = element("button", run.message || run.task_id, "past");
    node.title = node.textContent;
    node.disabled = state.busy;
    node.onclick = () => openTask(run.task_id).catch(e => notice(e.message));
    $("history").append(node);
  }
  if (!tasks.length) {
    $("history").append(element("span", "暂无运行记录", "meta"));
  }
}

async function select(scene) {
  resetView();
  state.scene = scene;
  $("objectScope").hidden = !scene.default_object;
  $("objectId").value = scene.default_object || "";
  $("executionOptions").hidden = !scene.durable_plan;
  $("executionMode").value = scene.durable_plan ? "durable_plan" : "reactive";
  $("title").textContent = scene.name;
  $("description").textContent = scene.description;
  $("question").value = "";
  for (const node of $("scenes").children) {
    node.classList.toggle("active", node.dataset.id === scene.id);
  }
  showBusy(false);
  await history();
}

async function boot() {
  const result = await (await request(api + "/scenarios")).json();
  state.scenes = result.scenarios;
  $("mode").options[1].disabled = !result.model_available;
  $("mode").options[1].textContent = result.model_available ? "模型 · " + result.model_name : "模型模式（待配置）";
  $("scenes").replaceChildren();
  for (const scene of result.scenarios) {
    const button = element("button", scene.name, "scene");
    button.dataset.id = scene.id;
    button.onclick = () => select(scene).catch(e => notice(e.message));
    $("scenes").append(button);
  }
  if (result.scenarios.length) {
    await select(result.scenarios[0]);
  } else if (result.unavailable_scenarios?.length) {
    $("title").textContent = "场景暂不可用";
    $("description").textContent = "修复发布快照后，刷新页面即可重试。";
    $("submit").disabled = true;
    $("example").disabled = true;
  } else {
    $("title").textContent = "还没有场景";
    $("description").textContent = "将场景包放入 scenarios 目录并重启服务。";
    $("submit").disabled = true;
    $("example").disabled = true;
  }
  if (result.unavailable_scenarios?.length) {
    notice("快照校验失败：" + result.unavailable_scenarios.join("、"));
  }
}

async function consume(response, onEvent) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (; ;) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += value;
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const lines = buffer.slice(0, cut).split("\n");
      buffer = buffer.slice(cut + 2);
      const fields = Object.fromEntries(lines.filter(l => l.includes(": ")).map(l => {
        const index = l.indexOf(": ");
        return [l.slice(0, index), l.slice(index + 2)];
      }));
      if (fields.event) {
        onEvent({
          id: fields.id,
          event: fields.event,
          data: JSON.parse(fields.data)
        });
      }
    }
  }
}

$("ask").onsubmit = async e => {
  e.preventDefault();
  if (state.busy || !state.scene || !$("question").value.trim()) {
    return;
  }
  const view = beginView();
  state.task = null;
  $("trace").replaceChildren();
  $("evidence").replaceChildren();
  $("status").textContent = "运行中";
  $("answer").textContent = "等待回答…";
  $("prompt").textContent = $("question").value;
  $("taskid").textContent = "";
  notice();
  showBusy(true);
  try {
    const body = {
      business_id: state.scene.id,
      session_id: state.session,
      message: $("question").value,
      mode: $("mode").value,
      response_mode: "streaming"
    };
    if (!$("objectScope").hidden && $("objectId").value.trim()) {
      body.context = { object_ids: [$("objectId").value.trim()] };
    }
    if (state.scene.durable_plan && $("executionMode").value === "durable_plan") {
      body.execution_mode = "durable_plan";
      body.mode = "offline";
    }
    const response = await request(api + "/chat", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body)
    });
    await consume(response, entry => {
      drawTrace(entry);
      state.task = entry.data.task_id;
      state.running = ![
        "task_completed", "task_partial", "task_cancelled", "task_failed"
      ].includes(entry.event);
      $("taskid").textContent = state.task;
      $("cancel").hidden = !state.running;
      if (body.execution_mode === "durable_plan" && entry.event === "plan_validated") {
        loadPlan(state.task, view, true);
      }
      if (entry.event === "message_delta") {
        $("answer").textContent = entry.data.delta;
      }
      if (entry.event === "approval_required") {
        $("status").textContent = "等待审批";
        $("answer").textContent = "提案已提交，请审批人在独立的审批队列中复核。";
      }
    });
    if (state.task) {
      await openTask(state.task);
    }
  } catch (error) {
    notice(error.message);
    $("status").textContent = "连接中断";
    if (!state.task) {
      $("answer").textContent = "本次请求未获得结果。";
    }
  } finally {
    showBusy(false);
    await history().catch(e => notice(e.message));
  }
};

$("executionMode").onchange = () => showBusy(state.busy);
$("example").onclick = () => {
  $("question").value = state.scene?.example || "";
  $("question").focus();
};
$("new").onclick = () => {
  resetView();
  $("question").value = "";
  $("question").focus();
};
$("refresh").onclick = () => history().catch(e => notice(e.message));
$("cancel").onclick = () =>
  request(api + "/tasks/" + state.task + "/cancel", { method: "POST" }).catch(error => notice(error.message));
$("unlock").onsubmit = async e => {
  e.preventDefault();
  state.token = $("secret").value;
  try {
    await boot();
    $("login").close();
    $("secret").value = "";
  } catch (error) {
    $("secret").setCustomValidity(error.message);
    $("secret").reportValidity();
  }
};
$("secret").oninput = () => $("secret").setCustomValidity("");
(async () => {
  const response = await fetch("/local/access", { method: "POST", headers: { "X-Local-UI": "1" } });
  if (response.status === 403) {
    $("login").showModal();
    return;
  }
  state.token = (await response.json()).token;
  await boot();
})().catch(e => notice(e.message));
