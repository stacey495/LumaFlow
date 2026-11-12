"use strict";

const byId = (id) => document.getElementById(id);
const setText = (id, value) => { byId(id).textContent = value; };
const hide = (id, value) => { byId(id).hidden = value; };
const active = new Set(["queued", "running", "cancelling"]);
const labels = {
  queued: "У черзі", running: "Виконується", cancelling: "Скасовується",
  completed: "Готово", failed: "Помилка", cancelled: "Скасовано",
  ready: "Готовий", busy: "Зайнятий", starting: "Запускається", off: "Вимкнений",
};
const profiles = {light: "Легка", balanced: "Збалансована", deep: "Глибока"};
const model = {user: null, file: null, objectUrl: null, jobId: null, jobs: [], view: "studio",
  authMode: "login", afterAuth: null, refreshing: false, selection: 0};
const allowed = new Set(["image/jpeg", "image/png", "image/webp"]);
const date = new Intl.DateTimeFormat("uk-UA", {dateStyle: "short", timeStyle: "medium"});

function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

async function request(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== "GET" && model.user) headers.set("X-CSRF-Token", model.user.csrf);
  const response = await fetch(url, {...options, headers, credentials: "same-origin", cache: "no-store"});
  if (!response.ok) {
    const details = await response.json().catch(() => ({}));
    const error = new Error(details.error || "Сервер тимчасово не відповідає.");
    error.status = response.status;
    throw error;
  }
  return response;
}

async function json(url, options) {
  return (await request(url, options)).json();
}

function notify(message, error = false) {
  const node = byId("notification");
  node.hidden = !message;
  node.textContent = message || "";
  node.classList.toggle("error", error);
}

function fieldError(id, message) {
  setText(id, message || "");
  hide(id, !message);
}

function setUser(user) {
  model.user = user;
  hide("account-button", Boolean(user));
  hide("logout", !user);
  hide("account-name", !user);
  hide("admin-tab", user?.role !== "admin");
  setText("account-name", user?.username || "");
}

function authMode(mode) {
  model.authMode = mode;
  const login = mode === "login";
  byId("login-tab").classList.toggle("active", login);
  byId("register-tab").classList.toggle("active", !login);
  byId("login-tab").setAttribute("aria-pressed", login);
  byId("register-tab").setAttribute("aria-pressed", !login);
  byId("password").autocomplete = login ? "current-password" : "new-password";
  setText("auth-title", login ? "Вхід до LumaFlow" : "Створення акаунта");
  setText("auth-submit", login ? "Увійти" : "Створити акаунт");
  fieldError("auth-error");
}

function openAuth(after) {
  model.afterAuth = after || null;
  authMode("login");
  if (!byId("auth-dialog").open) byId("auth-dialog").showModal();
  byId("username").focus();
}

function showView(view) {
  if (view !== "studio" && !model.user) {
    openAuth(() => showView(view));
    return;
  }
  if (view === "admin" && model.user?.role !== "admin") return;
  model.view = view;
  for (const name of ["studio", "history", "admin"]) hide(name + "-view", name !== view);
  for (const item of document.querySelectorAll(".nav-button")) {
    item.classList.toggle("active", item.dataset.view === view);
    if (item.dataset.view === view) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  notify();
  if (view === "admin") refresh();
  if (view === "history") renderHistory();
}

function compare(before, after) {
  const first = byId("before");
  const second = byId("after");
  if (first.getAttribute("src") !== before) first.setAttribute("src", before);
  if (after && second.getAttribute("src") !== after) second.setAttribute("src", after);
  if (!after) second.removeAttribute("src");
  for (const id of ["after", "before-tag", "after-tag", "divider", "comparison", "download"]) hide(id, !after);
}

function releaseFile() {
  if (model.objectUrl) URL.revokeObjectURL(model.objectUrl);
  model.objectUrl = null;
  model.file = null;
  byId("image-input").value = "";
  setText("file-label", "Вибрати файл");
  setText("file-meta", "або перетягни сюди");
}

function renderTask() {
  const job = model.jobs.find((item) => item.id === model.jobId);
  hide("progress-row", !job);
  hide("cancel-job", !job || !active.has(job.status));
  if (!job) {
    setText("task-heading", "Поточна задача");
    setText("task-details", "Тут з’явиться прогрес після запуску обробки.");
    return;
  }
  setText("task-heading", labels[job.status] + " · " + job.name);
  let detail = job.error || job.stage;
  if (job.queue) detail = "Позиція " + job.queue.position + " · приблизно " + job.queue.seconds + " с до запуску";
  if (job.worker) detail += " · " + job.worker;
  if (job.ended && job.started && job.status === "completed") detail += " · " + (job.ended - job.started).toFixed(1) + " с";
  setText("task-details", detail);
  byId("job-progress").value = job.progress;
  setText("job-percent", job.progress + "%");
  byId("cancel-job").disabled = job.status === "cancelling";
  const base = "/api/jobs/" + job.id;
  const done = job.status === "completed";
  compare(base + "/original", done ? base + "/result" : null);
  if (done) byId("download").href = base + "/download";
  setText("preview-meta", job.width + " × " + job.height + " · " + profiles[job.profile]);
}

function selectJob(id) {
  model.selection += 1;
  releaseFile();
  model.jobId = id;
  byId("comparison").value = 50;
  byId("preview").style.setProperty("--split", "50%");
  showView("studio");
  renderTask();
}

async function loadFile(file) {
  if (!file) return;
  const sequence = ++model.selection;
  fieldError("upload-error");
  if (!file.size || file.size > 10 * 1024 * 1024) {
    fieldError("upload-error", "Файл має важити від 1 байта до 10 МБ.");
    return;
  }
  if (file.type && !allowed.has(file.type)) {
    fieldError("upload-error", "Вибери статичний JPG, PNG або WebP.");
    return;
  }
  const url = URL.createObjectURL(file);
  const image = new Image();
  image.src = url;
  try {
    await image.decode();
    if (image.naturalWidth * image.naturalHeight > 6_000_000 || Math.max(image.naturalWidth, image.naturalHeight) > 5000) {
      throw new Error("Ліміт зображення — 6 Мп і 5000 пікселів на сторону.");
    }
    if (sequence !== model.selection) return;
    releaseFile();
    model.objectUrl = url;
    model.file = file;
    model.jobId = null;
    setText("file-label", file.name);
    setText("file-meta", image.naturalWidth + " × " + image.naturalHeight + " · " + (file.size / 1048576).toFixed(2) + " МБ");
    setText("preview-meta", file.name);
    compare(url);
    renderTask();
  } catch (error) {
    if (sequence === model.selection) fieldError("upload-error", error.name === "EncodingError" ? "Файл не вдалося відкрити." : error.message);
  } finally {
    if (model.objectUrl !== url) URL.revokeObjectURL(url);
  }
}

async function demo() {
  if (!model.user) return openAuth(demo);
  const button = byId("demo-button");
  button.disabled = true;
  try {
    const blob = await (await request("/api/demo")).blob();
    await loadFile(new File([blob], "Гірське озеро · демо.jpg", {type: "image/jpeg"}));
    document.querySelector('input[value="deep"]').checked = true;
    showView("studio");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function demoLoad() {
  const button = byId("demo-load");
  button.disabled = true;
  let submitted = 0;
  try {
    const blob = await (await request("/api/demo")).blob();
    for (let number = 1; number <= 6; number += 1) {
      await json("/api/jobs", {method: "POST", body: blob, headers: {
        "Content-Type": "application/octet-stream", "X-Profile": "deep",
        "X-Image-Name": encodeURIComponent("Гірське озеро · демо " + number + ".jpg"),
      }});
      submitted += 1;
    }
    notify("Надіслано 6 демозадач. Розподіл і черга оновлюються нижче.");
  } catch (error) {
    notify((submitted ? "Надіслано задач: " + submitted + ". " : "") + error.message, true);
  } finally {
    button.disabled = false;
    refresh();
  }
}

async function startJob() {
  if (!model.user) return openAuth(startJob);
  if (!model.file) {
    fieldError("upload-error", "Вибери зображення або скористайся демонстраційним кадром.");
    return;
  }
  fieldError("upload-error");
  const button = byId("start-job");
  button.disabled = true;
  try {
    const response = await json("/api/jobs", {method: "POST", body: model.file, headers: {
      "Content-Type": "application/octet-stream", "X-Image-Name": encodeURIComponent(model.file.name),
      "X-Profile": document.querySelector('input[name="profile"]:checked').value,
    }});
    model.jobs.unshift(response.job);
    selectJob(response.job.id);
    renderHistory();
    refresh();
  } catch (error) {
    fieldError("upload-error", error.message);
  } finally {
    button.disabled = false;
  }
}

function badge(status) {
  return element("span", labels[status] || status, "badge " + status);
}

function rowName(job) {
  const cell = element("td");
  cell.append(element("span", job.name, "table-name"),
    element("small", job.width + " × " + job.height + " · " + profiles[job.profile], "table-sub"));
  return cell;
}

function action(text, callback, danger = false) {
  const button = element("button", text, "button subtle" + (danger ? " danger" : ""));
  button.type = "button";
  button.addEventListener("click", callback);
  return button;
}

async function changeJob(job, kind, admin = false) {
  const endpoint = admin ? "/api/admin/jobs/" : "/api/jobs/";
  if (kind === "delete" && !window.confirm("Видалити задачу та її зображення з історії?")) return;
  try {
    const response = await json(endpoint + job.id + (kind === "cancel" ? "/cancel" : ""),
      {method: kind === "cancel" ? "POST" : "DELETE"});
    if (kind === "cancel") {
      model.jobs = model.jobs.map((item) => item.id === job.id ? response.job : item);
    } else if (model.jobId === job.id) {
      model.jobId = null;
      setText("preview-meta", "Демонстраційний кадр");
      compare("/sample-preview.jpg");
    }
    renderTask();
    await refresh();
  } catch (error) {
    notify(error.message, true);
  }
}

function renderHistory() {
  hide("history-count", !model.jobs.length);
  setText("history-count", model.jobs.length);
  if (model.view !== "history") return;
  hide("history-empty", Boolean(model.jobs.length));
  hide("history-table", !model.jobs.length);
  const body = byId("history-body");
  const focused = document.activeElement?.dataset?.jobAction;
  const rows = model.jobs.map((job) => {
    const row = element("tr");
    const state = element("td");
    state.append(badge(job.status));
    if (job.queue) state.append(element("small", "№" + job.queue.position + " · ~" + job.queue.seconds + " с", "table-sub"));
    else if (job.status === "running") state.append(element("small", job.progress + "%", "table-sub"));
    const buttons = element("div", null, "actions");
    const viewButton = action("Відкрити", () => selectJob(job.id));
    viewButton.dataset.jobAction = "view-" + job.id;
    buttons.append(viewButton);
    if (job.status === "completed") {
      const link = element("a", "JPG ↓", "button subtle");
      link.href = "/api/jobs/" + job.id + "/download";
      link.dataset.jobAction = "download-" + job.id;
      buttons.append(link);
    }
    if (job.status !== "cancelling") {
      const kind = active.has(job.status) ? "cancel" : "delete";
      const button = action(kind === "cancel" ? "Скасувати" : "Видалити", () => changeJob(job, kind), true);
      button.dataset.jobAction = kind + "-" + job.id;
      buttons.append(button);
    }
    const cell = element("td");
    cell.append(buttons);
    row.append(rowName(job), state, element("td", job.worker || "—"), element("td", date.format(job.created * 1000)), cell);
    return row;
  });
  body.replaceChildren(...rows);
  if (focused) body.querySelector('[data-job-action="' + focused + '"]')?.focus({preventScroll: true});
}

function emptyRow(count, message) {
  const row = element("tr");
  const cell = element("td", message, "table-sub");
  cell.colSpan = count;
  row.append(cell);
  return row;
}

function renderAdmin(data) {
  setText("admin-mode", data.mode === "docker" ? "Docker · Linux-контейнери" : "Локальні процеси");
  fieldError("admin-error", data.error);
  const counts = data.counts;
  const cards = [
    ["Сервери онлайн", data.workers.filter((worker) => ["ready", "busy"].includes(worker.state)).length + " / " + data.limits.max_workers],
    ["Виконуються", (counts.running || 0) + (counts.cancelling || 0)], ["У черзі", counts.queued || 0],
    ["Завершено успішно", counts.completed || 0],
  ];
  byId("system-counts").replaceChildren(...cards.map(([label, value]) => {
    const card = element("div", null, "panel metric");
    card.append(element("span", label), element("b", value));
    return card;
  }));
  byId("worker-cards").replaceChildren(...data.workers.map((worker) => {
    const card = element("section", null, "panel worker-card");
    const header = element("div", null, "worker-head");
    header.append(element("h2", worker.name), badge(worker.state));
    const cpu = element("div", null, "worker-measure");
    cpu.append(element("span", "CPU"), element("b", worker.cpu == null ? "—" : worker.cpu + "%"));
    const ram = element("div", null, "worker-measure");
    ram.append(element("span", "RAM"), element("b", worker.ram_mb == null ? "—" : worker.ram_mb + " МБ"));
    let info = worker.pid ? "PID сервера " + worker.pid : "Сервер очікує запуску";
    if (worker.task_pid) info += " · PID обчислення " + worker.task_pid;
    if (worker.port) info += " · порт " + worker.port;
    const identity = worker.container ? element("small", worker.container, "table-sub") : "";
    card.append(header, cpu, ram, element("div", info, "worker-process"), identity);
    return card;
  }));
  for (const [id, items, completed] of [["admin-active", data.active, false], ["admin-recent", data.recent, true]]) {
    const rows = items.map((job) => {
      const row = element("tr");
      const state = element("td");
      state.append(badge(job.status));
      if (job.status === "running") state.append(element("small", job.progress + "% · " + job.stage, "table-sub"));
      if (job.error) state.append(element("small", job.error, "table-sub"));
      const final = element("td");
      if (completed) final.append(action("Видалити", () => changeJob(job, "delete", true), true));
      else final.textContent = job.queue ? "№" + job.queue.position + " · ~" + job.queue.seconds + " с" : "—";
      row.append(rowName(job), element("td", job.username), element("td", job.worker || "—"), state, final);
      return row;
    });
    byId(id).replaceChildren(...(rows.length ? rows : [emptyRow(5, "Задач поки немає.")]));
  }
}

async function refresh() {
  if (model.refreshing) return;
  model.refreshing = true;
  try {
    const pending = [json("/health"), model.user ? json("/api/jobs") : Promise.resolve(null)];
    if (model.view === "admin") pending.push(json("/api/admin"));
    const [health, history, system] = await Promise.all(pending);
    const healthElement = byId("service-health");
    healthElement.classList.toggle("online", health.ready_workers > 0);
    const count = health.ready_workers;
    healthElement.lastElementChild.textContent = "HTTPS · " + count + (count === 1 ? " сервер" : count ? " сервери" : " серверів");
    if (model.user && history) {
      model.jobs = history.jobs;
      if (model.jobId && !model.jobs.some((job) => job.id === model.jobId)) {
        model.jobId = null;
        setText("preview-meta", "Демонстраційний кадр");
        compare("/sample-preview.jpg");
      }
      renderTask();
      renderHistory();
    }
    if (system && model.view === "admin") renderAdmin(system);
  } catch (error) {
    if (error.status === 401) {
      resetSession();
      notify("Сеанс завершився. Увійди ще раз.", true);
    } else {
      const healthElement = byId("service-health");
      healthElement.classList.remove("online");
      healthElement.lastElementChild.textContent = "Зв’язок втрачено";
    }
  } finally {
    model.refreshing = false;
  }
}

function resetSession() {
  setUser(null);
  model.selection += 1;
  releaseFile();
  model.jobs = [];
  model.jobId = null;
  showView("studio");
  compare("/sample-preview.jpg");
  setText("preview-meta", "Демонстраційний кадр");
  renderTask();
  renderHistory();
}

for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => showView(button.dataset.view));
byId("account-button").addEventListener("click", () => openAuth());
byId("login-tab").addEventListener("click", () => authMode("login"));
byId("register-tab").addEventListener("click", () => authMode("register"));
byId("close-auth").addEventListener("click", () => byId("auth-dialog").close());
byId("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  fieldError("auth-error");
  byId("auth-submit").disabled = true;
  try {
    const response = await json("/api/" + model.authMode, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: byId("username").value.trim(), password: byId("password").value})});
    setUser(response.user);
    byId("password").value = "";
    byId("auth-dialog").close();
    const next = model.afterAuth;
    model.afterAuth = null;
    if (next) await next();
    await refresh();
  } catch (error) {
    fieldError("auth-error", error.message);
  } finally {
    byId("auth-submit").disabled = false;
  }
});
byId("logout").addEventListener("click", async () => {
  try {
    await json("/api/logout", {method: "POST"});
    resetSession();
  } catch (error) {
    notify(error.message, true);
  }
});
byId("file-button").addEventListener("click", () => byId("image-input").click());
byId("image-input").addEventListener("change", (event) => loadFile(event.target.files[0]));
byId("demo-button").addEventListener("click", demo);
byId("demo-load").addEventListener("click", demoLoad);
byId("start-job").addEventListener("click", startJob);
byId("cancel-job").addEventListener("click", () => {
  const job = model.jobs.find((item) => item.id === model.jobId);
  if (job) changeJob(job, "cancel");
});
byId("comparison").addEventListener("input", (event) => byId("preview").style.setProperty("--split", event.target.value + "%"));
const zone = byId("drop-zone");
zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("drag"); });
zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
zone.addEventListener("drop", (event) => {
  event.preventDefault();
  zone.classList.remove("drag");
  loadFile(event.dataTransfer.files[0]);
});
json("/api/session").then((session) => setUser(session.user)).catch(() => {}).finally(refresh);
setInterval(refresh, 950);
