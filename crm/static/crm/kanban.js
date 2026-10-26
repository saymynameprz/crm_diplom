(() => {
    "use strict";
    const board = document.querySelector(".kanban-scroll");
    if (!board || !window.fetch || !window.AbortController) return;
    const feedback = document.getElementById("kanban-feedback");
    document.getElementById("kanban-dismiss").addEventListener("click", () => { feedback.hidden = true; });
    const status = document.getElementById("kanban-status");
    const refresh = document.getElementById("kanban-refresh");
    let dragging = null;
    let busy = false;
    let uncertain = false;
    function announce(message, needsRefresh = false) {
        feedback.hidden = false;
        status.textContent = message;
        refresh.hidden = !needsRefresh;
    }
    function enhance() {
        board.querySelectorAll(".kanban-deal").forEach(card => {
            card.draggable = true;
            card.querySelector(".kanban-drag-handle").hidden = false;
        });
    }
    function clearDrag() {
        board.querySelectorAll(".is-dragging, .is-drop-target").forEach(node => node.classList.remove("is-dragging", "is-drop-target"));
    }
    board.addEventListener("dragstart", event => {
        const card = event.target.closest(".kanban-deal");
        if (!card || busy || uncertain || event.target.closest("a, input, select, button")) {
            event.preventDefault();
            return;
        }
        dragging = {card, id: card.dataset.dealId, source: card.closest(".kanban-column").dataset.stage};
        card.classList.add("is-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", dragging.id);
    });
    board.addEventListener("dragover", event => {
        const column = event.target.closest(".kanban-column");
        if (!column || !dragging || busy || uncertain) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        board.querySelectorAll(".is-drop-target").forEach(node => node.classList.remove("is-drop-target"));
        column.classList.add("is-drop-target");
    });
    board.addEventListener("dragleave", event => {
        const column = event.target.closest(".kanban-column");
        if (column && !column.contains(event.relatedTarget)) column.classList.remove("is-drop-target");
    });
    board.addEventListener("dragend", () => { clearDrag(); dragging = null; });
    board.addEventListener("submit", event => {
        if (busy) {
            event.preventDefault();
            announce("Дождитесь завершения текущего перемещения.");
        }
    });
    board.addEventListener("drop", async event => {
        const column = event.target.closest(".kanban-column");
        if (!column || !dragging || busy || uncertain) return;
        event.preventDefault();
        const move = dragging;
        dragging = null;
        clearDrag();
        const target = column.dataset.stage;
        if (target === move.source) {
            announce("Сделка уже находится на выбранном этапе.");
            return;
        }
        busy = true;
        board.setAttribute("aria-busy", "true");
        move.card.classList.add("is-saving");
        announce("Сохраняем перемещение…");
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), 15000);
        try {
            const url = new URL(move.card.dataset.changeUrl, window.location.href);
            if (url.origin !== window.location.origin) throw new Error("Unexpected endpoint");
            const csrf = move.card.querySelector("[name=csrfmiddlewaretoken]").value;
            const response = await fetch(url.href, {
                method: "POST", credentials: "same-origin", signal: controller.signal,
                headers: {"Accept": "application/json", "X-CSRFToken": csrf},
                body: new URLSearchParams({stage: target, expected_updated_at: move.card.querySelector("[name=expected_updated_at]").value})
            });
            if (response.redirected || !response.headers.get("Content-Type")?.includes("application/json")) {
                uncertain = true;
                announce("Не получено подтверждение. Возможно, сессия истекла. Обновите доску и войдите в систему.", true);
                return;
            }
            const data = await response.json();
            if (!response.ok || data.success !== true) {
                uncertain = response.status !== 400;
                announce(typeof data.message === "string" ? data.message : "Не удалось переместить сделку. Обновите доску.", uncertain);
                return;
            }
            if (String(data.deal_id) !== move.id || data.stage !== target || !Array.isArray(data.columns)) throw new Error("Invalid response");
            const expected = new Set([move.source, target]);
            const replacements = [];
            for (const part of data.columns) {
                if (!expected.has(part.stage) || typeof part.html !== "string") throw new Error("Unexpected column");
                const current = [...board.querySelectorAll(".kanban-column")].find(node => node.dataset.stage === part.stage);
                const template = document.createElement("template");
                // Only same-origin server-rendered Django fragments enter this template.
                template.innerHTML = part.html;
                const next = template.content.firstElementChild;
                if (!current || !next?.matches("section.kanban-column") || next.dataset.stage !== part.stage) throw new Error("Invalid fragment");
                replacements.push([current, next]);
                expected.delete(part.stage);
            }
            if (expected.size) throw new Error("Stale board");
            replacements.forEach(([current, next]) => current.replaceWith(next));
            enhance();
            announce(data.message);
        } catch (error) {
            uncertain = true;
            announce("Не удалось подтвердить результат из-за ошибки связи или ответа сервера. Сервер мог сохранить этап. Обновите доску перед повтором.", true);
        } finally {
            window.clearTimeout(timer);
            busy = false;
            board.removeAttribute("aria-busy");
            move.card.classList.remove("is-saving");
            clearDrag();
        }
    });
    document.getElementById("kanban-drag-help").hidden = false;
    enhance();
})();
