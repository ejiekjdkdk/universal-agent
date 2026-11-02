"use strict";
const $ = (id) => document.getElementById(id);
const api = "/api/v1/agent";
const state = {scenes: [], scene: null, token: "", session: null, task: null, busy: false, view: 0};
const labels = {
  task_started: "任务开始", planner_started: "启动规划器", tools_discovered: "发现可用工具",
  authorization: "权限校验", step_started: "开始执行工具", tool_input: "输入摘要", tool_result: "输出与耗时",
  step_completed: "工具完成", step_failed: "工具失败", tool_retry: "重试工具", knowledge_retrieved: "检索资料",
  message_delta: "生成回答", task_completed: "任务完成", task_cancelled: "已取消", task_failed: "任务失败",
  model_request: "调用模型", model_response: "收到模型响应", model_round: "模型规划", tool_selected: "选择工具"
};
const statuses = {completed: "已完成", failed: "失败", cancelled: "已取消", running: "运行中", partial: "部分完成"};
function element(tag, content, cls) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (cls) node.className = cls;
  return node;
}
function notice(message = "") { $("notice").textContent = message; }
async function request(path, options = {}) {
  const result = await fetch(path, {...options, headers: {Authorization: "Bearer " + state.token,
    ...(options.body ? {"Content-Type": "application/json"} : {}), ...options.headers}});
  if (!result.ok) {
    const body = await result.json().catch(() => ({}));
    throw Error(body.error?.message || ("请求失败 (" + result.status + ")"));
  }
  return result;
}
function showBusy(value) {
  state.busy = value;
  $("submit").disabled = value || !state.scene;
  $("mode").disabled = value;
  $("new").disabled = value;
  $("cancel").hidden = !value || !state.task;
  document.querySelectorAll(".scene,.past").forEach(node => {node.disabled = value;});
}
function resetView() {
  state.view++;
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
  const item = element("li"), details = element("details");
  const data = entry.data || {};
  const title = data.capability_id || data.tool || data.resource || "";
  details.append(element("summary", (labels[entry.event] || entry.event) + (title ? " · " + title : "")));
  details.append(element("pre", JSON.stringify(data, null, 2)));
  const stamp = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : "";
  item.append(element("div", "#" + entry.id + "  " + stamp, "meta"), details);
  $("trace").append(item);
}
function showResult(run) {
  state.task = run.task_id;
  state.session = run.session_id;
  $("taskid").textContent = run.task_id;
  $("status").textContent = statuses[run.status] || run.status;
  $("prompt").textContent = run.message ? "提问 · " + run.message : "";
  $("answer").textContent = run.answer || run.error?.message || (run.status === "cancelled" ? "本次运行已取消。" : "等待回答…");
  $("evidence").replaceChildren();
  for (const [i, row] of (run.evidence || []).entries()) {
    const details = element("details");
    details.append(element("summary", "[E" + (i + 1) + "] " + row.title),
      element("div", row.source_id + " · " + row.version, "meta"), element("p", row.snippet, "evidence-text"));
    $("evidence").append(details);
  }
}
async function openTask(id) {
  const view = ++state.view;
  const path = api + "/tasks/" + encodeURIComponent(id);
  const [run, trace] = await Promise.all([request(path).then(r => r.json()), request(path + "/trace").then(r => r.json())]);
  if (view !== state.view) return;
  showResult(run);
  $("trace").replaceChildren();
  trace.events.forEach(drawTrace);
}
async function history() {
  const sid = state.scene?.id;
  if (!sid) return;
  const {tasks} = await (await request(api + "/tasks?business_id=" + encodeURIComponent(sid))).json();
  if (sid !== state.scene?.id) return;
  $("history").replaceChildren();
  for (const run of tasks) {
    const node = element("button", run.message || run.task_id, "past");
    node.title = node.textContent;
    node.disabled = state.busy;
    node.onclick = () => openTask(run.task_id).catch(e => notice(e.message));
    $("history").append(node);
  }
  if (!tasks.length) $("history").append(element("span", "暂无运行记录", "meta"));
}
async function select(scene) {
  resetView();
  state.scene = scene;
  $("title").textContent = scene.name;
  $("description").textContent = scene.description;
  $("question").value = "";
  for (const node of $("scenes").children) node.classList.toggle("active", node.dataset.id === scene.id);
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
  if (result.scenarios.length) await select(result.scenarios[0]);
  else {
    $("title").textContent = "还没有场景";
    $("description").textContent = "将场景包放入 scenarios 目录并重启服务。";
    $("submit").disabled = true;
    $("example").disabled = true;
  }
}
async function consume(response, onEvent) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buffer += value;
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const lines = buffer.slice(0, cut).split("\n");
      buffer = buffer.slice(cut + 2);
      const fields = Object.fromEntries(lines.filter(l => l.includes(": ")).map(l => {
        const index = l.indexOf(": ");
        return [l.slice(0, index), l.slice(index + 2)];
      }));
      if (fields.event) onEvent({id: fields.id, event: fields.event, data: JSON.parse(fields.data)});
    }
  }
}
$("ask").onsubmit = async e => {
  e.preventDefault();
  if (state.busy || !state.scene || !$("question").value.trim()) return;
  state.view++;
  state.task = null;
  $("trace").replaceChildren(); $("evidence").replaceChildren();
  $("status").textContent = "运行中"; $("answer").textContent = "等待回答…";
  $("prompt").textContent = $("question").value;
  $("taskid").textContent = "";
  notice(); showBusy(true);
  try {
    const body = {business_id: state.scene.id, session_id: state.session, message: $("question").value, mode: $("mode").value, response_mode: "streaming"};
    const response = await request(api + "/chat", {method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}, body: JSON.stringify(body)});
    await consume(response, entry => {
      drawTrace(entry);
      state.task = entry.data.task_id;
      $("taskid").textContent = state.task;
      $("cancel").hidden = false;
      if (entry.event === "message_delta") $("answer").textContent = entry.data.delta;
    });
    if (state.task) await openTask(state.task);
  } catch (error) {
    notice(error.message);
    $("status").textContent = "连接中断";
    if (!state.task) $("answer").textContent = "本次请求未获得结果。";
  } finally {
    showBusy(false);
    await history().catch(e => notice(e.message));
  }
};
$("example").onclick = () => { $("question").value = state.scene?.example || ""; $("question").focus(); };
$("new").onclick = () => { resetView(); $("question").value = ""; $("question").focus(); };
$("refresh").onclick = () => history().catch(e => notice(e.message));
$("cancel").onclick = () => request(api + "/tasks/" + state.task + "/cancel", {method: "POST"}).catch(e => notice(e.message));
$("unlock").onsubmit = async e => {
  e.preventDefault(); state.token = $("secret").value;
  try { await boot(); $("login").close(); $("secret").value = ""; }
  catch (error) { $("secret").setCustomValidity(error.message); $("secret").reportValidity(); }
};
$("secret").oninput = () => $("secret").setCustomValidity("");
(async () => {
  const response = await fetch("/local/access", {method: "POST", headers: {"X-Local-UI": "1"}});
  if (response.status === 403) { $("login").showModal(); return; }
  state.token = (await response.json()).token;
  await boot();
})().catch(e => notice(e.message));
