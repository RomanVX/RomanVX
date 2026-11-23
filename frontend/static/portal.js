"use strict";

const api = "/api/auth/portal/";
const login = document.getElementById("login");
const form = document.getElementById("login-form");
const submit = document.getElementById("login-submit");
const boards = document.getElementById("dashboards");
const account = document.getElementById("account");
const logout = document.getElementById("logout");
const status = document.getElementById("status");

async function request(path, options = {}) {
  const result = await fetch(api + path, { cache: "no-store", ...options });
  if (result.status === 401 && path === "me") return null;
  const payload = await result.json().catch(() => ({}));
  if (!result.ok) {
    throw new Error(typeof payload.detail === "string" ? payload.detail : "Портал недоступен. Попробуйте ещё раз.");
  }
  return payload;
}

function render(user) {
  login.hidden = Boolean(user);
  boards.hidden = account.hidden = !user;
  document.getElementById("account-name").textContent = user?.login ?? "";
  status.textContent = "";
}

async function refresh() {
  try {
    render(await request("me"));
  } catch (error) {
    status.textContent = error.message;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submit.disabled = true;
  status.textContent = "";
  try {
    const fields = new FormData(form);
    const user = await request("login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(fields)),
    });
    render(user);
  } catch (error) {
    status.textContent = error.message;
  } finally {
    form.elements.password.value = "";
    submit.disabled = false;
  }
});

logout.addEventListener("click", async () => {
  logout.disabled = true;
  try {
    await request("logout", { method: "POST" });
    render(null);
  } catch (error) {
    status.textContent = error.message;
  } finally {
    logout.disabled = false;
  }
});

for (const button of boards.querySelectorAll("[data-cabinet]")) {
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "Открываем кабинет…";
    try {
      const data = await request("launch/" + button.dataset.cabinet, { method: "POST" });
      const relay = document.createElement("form");
      relay.method = "POST";
      relay.action = data.destination;
      const ticket = document.createElement("input");
      ticket.type = "hidden";
      ticket.name = "ticket";
      ticket.value = data.ticket;
      relay.append(ticket);
      document.body.append(relay);
      relay.submit();
      relay.remove();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}

window.addEventListener("pageshow", refresh);
