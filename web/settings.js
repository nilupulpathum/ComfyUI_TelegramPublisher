// ComfyUI-TelegramPublisher settings/status UI (T050).
//
// ComfyUI frontend extension: refreshes the `account` / `destination`
// COMBO widgets on Telegram nodes from the local config (via the
// /telegram_publisher/* HTTP API), and adds "Test Telegram connection"
// and "Refresh Telegram lists" buttons to each node.
//
// Version note: written against the long-standing ComfyUI frontend APIs
// (app.registerExtension, nodeCreated, node.addWidget("button", ...),
// api.fetchApi) stable for years; tested target is "ComfyUI frontend 1.x,
// manual browser verification required" (see docs/INSTALL.md section 6).
// Extension import convention:
//   import { app } from "../../scripts/app.js";
//   import { api } from "../../scripts/api.js";
// (relative to this file in web/). If those paths ever move in a future
// frontend, this module degrades gracefully (plain nodes keep working).
//
// Display + fetch only: no business logic here (docs/ARCHITECTURE.md,
// frontend layer). Everything is wrapped in try/catch so a failed fetch
// or a missing widget never breaks the graph.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASSES = ["Telegram Send Image", "Telegram Send Album"];

async function fetchJson(url, options) {
  const res = await api.fetchApi(url, options);
  return await res.json();
}

function setComboOptions(node, name, values) {
  try {
    const widget = (node.widgets || []).find((w) => w.name === name);
    if (!widget || !Array.isArray(widget.options)) return;
    // ComfyUI COMBO widgets read widget.options.values; keep the current
    // value when still valid, else fall back to "" (explicit unset).
    widget.options.values = values;
    if (!values.includes(widget.value)) {
      widget.value = values.includes("") ? "" : values[0];
    }
    if (typeof widget.callback === "function") {
      try {
        widget.callback(widget.value);
      } catch (_) {
        /* leave static */
      }
    }
  } catch (_) {
    /* leave static */
  }
}

async function refreshLists(node) {
  try {
    const accountWidget = (node.widgets || []).find(
      (w) => w.name === "account"
    );
    const accountId =
      accountWidget && typeof accountWidget.value === "string"
        ? accountWidget.value
        : "";
    let accounts = [""];
    try {
      const body = await fetchJson("/telegram_publisher/accounts");
      if (body && body.ok && Array.isArray(body.accounts)) {
        const ids = body.accounts.map((a) => a.id).filter(Boolean).sort();
        accounts = ["", ...ids];
      }
    } catch (_) {
      /* leave static */
    }
    setComboOptions(node, "account", accounts);

    let destinations = [""];
    try {
      const url = accountId
        ? `/telegram_publisher/destinations?account_id=${encodeURIComponent(
            accountId
          )}`
        : "/telegram_publisher/destinations";
      const body = await fetchJson(url);
      if (body && body.ok && Array.isArray(body.destinations)) {
        const ids = body.destinations.map((d) => d.id).filter(Boolean).sort();
        destinations = ["", ...ids];
      }
    } catch (_) {
      /* leave static */
    }
    setComboOptions(node, "destination", destinations);
  } catch (_) {
    /* leave static */
  }
}

async function testConnection(node) {
  try {
    const valueOf = (name) => {
      try {
        const widget = (node.widgets || []).find((w) => w.name === name);
        return widget && typeof widget.value === "string" ? widget.value : "";
      } catch (_) {
        return "";
      }
    };
    const payload = {
      account_id: valueOf("account"),
      destination_id: valueOf("destination") || undefined,
    };
    const body = await fetchJson("/telegram_publisher/test_connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (body && body.ok) {
      alert(
        `Telegram connection OK${
          body.bot_username ? ` (@${body.bot_username})` : ""
        }.`
      );
    } else {
      alert(
        `Telegram connection failed: ${
          (body && body.error) || "unknown error"
        }`
      );
    }
  } catch (err) {
    try {
      alert(`Telegram connection failed: ${err && err.message ? err.message : err}`);
    } catch (_) {
      /* leave static */
    }
  }
}

try {
  app.registerExtension({
    name: "ComfyUI-TelegramPublisher",
    async nodeCreated(node) {
      try {
        if (!node || !NODE_CLASSES.includes(node.comfyClass)) return;
        // Refresh server-driven options shortly after creation; any
        // failure leaves the static COMBO values untouched.
        try {
          await refreshLists(node);
        } catch (_) {
          /* leave static */
        }
        try {
          node.addWidget("button", "Test Telegram connection", null, () => {
            testConnection(node);
          });
        } catch (_) {
          /* leave static */
        }
        try {
          node.addWidget("button", "Refresh Telegram lists", null, () => {
            refreshLists(node);
          });
        } catch (_) {
          /* leave static */
        }
      } catch (_) {
        /* leave static */
      }
    },
  });
} catch (_) {
  /* Extension loader unavailable: nodes keep working without the UI. */
}
