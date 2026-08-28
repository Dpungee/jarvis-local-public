from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

from .agent import Agent, AgentRunCancelled
from .cli import _ForegroundLease
from .config import Config
from .memory import Memory
from .model_client import user_model_error_message
from .ollama_client import OllamaError
from .proactive import RuntimeGuard
from .redaction import redact_secrets


APP_TITLE = "JARVIS Desktop"
MODEL_CHOICES = (
    "Auto",
    "Fast",
    "Reasoning",
    "Coding",
    "Deep 30B",
)
MODEL_OVERRIDES = {
    "Auto": "auto",
    "Fast": "fast",
    "Reasoning": "reasoning",
    "Coding": "coding",
    "Deep 30B": "deep",
}


def model_override_for(label: str) -> str:
    """Resolve one bounded operator-facing model label."""
    return MODEL_OVERRIDES.get(str(label).strip(), "auto")


def safe_ui_text(value: Any, limit: int = 100_000) -> str:
    """Redact control-plane secrets before text crosses into a UI widget."""
    text = redact_secrets(str(value), "[REDACTED]").replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)] + "\n…[display truncated]"


def compact_activity(value: Any, limit: int = 140) -> str:
    text = " ".join(safe_ui_text(value, limit * 2).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    payload: Any = None


class JarvisSession(threading.Thread):
    """Own SQLite and Agent on one thread; Tk remains isolated on its UI thread."""

    def __init__(self, config: Config) -> None:
        super().__init__(name="jarvis-desktop-session", daemon=True)
        self.config = config
        self.commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.events: queue.Queue[SessionEvent] = queue.Queue()
        self.cancel_event = threading.Event()
        self._shutdown = threading.Event()

    def emit(self, kind: str, payload: Any = None) -> None:
        self.events.put(SessionEvent(kind, payload))

    def submit(self, prompt: str, model_label: str) -> None:
        self.commands.put(("send", (prompt, model_override_for(model_label))))

    def new_chat(self) -> None:
        self.commands.put(("new_chat", None))

    def request_approvals(self) -> None:
        self.commands.put(("approvals", None))

    def decide_approval(self, approval_id: int, approve: bool) -> None:
        self.commands.put(("decide_approval", (int(approval_id), bool(approve))))

    def cancel(self) -> None:
        self.cancel_event.set()

    def shutdown(self) -> None:
        self._shutdown.set()
        self.cancel_event.set()
        self.commands.put(("shutdown", None))

    def _run_prompt(
        self,
        agent: Agent,
        memory: Memory,
        conversation_id: int,
        prompt: str,
        model_override: str,
    ) -> None:
        self.cancel_event.clear()
        self.emit("busy", True)
        self.emit("activity", "Preparing request")
        runtime_guard = RuntimeGuard(memory, self.config, background=False)

        def cancelled() -> bool:
            return self.cancel_event.is_set() or runtime_guard()

        try:
            with _ForegroundLease(self.config.data_dir):
                result = agent.run(
                    prompt,
                    conversation_id=conversation_id,
                    model_override=model_override,
                    cancellation_guard=cancelled,
                    prediction_origin="interactive",
                )
            self.emit(
                "assistant",
                {
                    "content": safe_ui_text(result),
                    "status": str(getattr(result, "status", "complete")),
                    "reason": safe_ui_text(getattr(result, "reason", "") or "", 1_000),
                    "approval_id": getattr(result, "approval_id", None),
                },
            )
        except AgentRunCancelled:
            self.emit("assistant", {
                "content": "Request stopped.",
                "status": "cancelled",
                "reason": "",
                "approval_id": None,
            })
        except OllamaError as exc:
            self.emit("assistant", {
                "content": user_model_error_message(exc),
                "status": "incomplete",
                "reason": "model provider unavailable after automatic fallbacks",
                "approval_id": None,
            })
        except Exception as exc:
            self.emit("error", f"Jarvis could not complete this request ({type(exc).__name__}): {exc}")
        finally:
            self.cancel_event.clear()
            self.emit("busy", False)
            self.emit("activity", "Ready")

    def run(self) -> None:
        try:
            with Memory(self.config.data_dir / "jarvis.db") as memory:
                agent = Agent(
                    self.config,
                    memory,
                    lambda message: self.emit("activity", compact_activity(message)),
                )
                conversation_id = memory.new_conversation("Desktop chat")
                control = memory.control_state()
                self.emit("ready", {
                    "conversation_id": conversation_id,
                    "control_state": str(control.get("state", "running")),
                    "fast_model": self.config.fast_model,
                    "reasoning_model": self.config.reasoning_model,
                    "coding_model": self.config.coding_model,
                    "deep_model": self.config.deep_model,
                })

                while not self._shutdown.is_set():
                    command, payload = self.commands.get()
                    if command == "shutdown":
                        break
                    if command == "send":
                        prompt, model_override = payload
                        self._run_prompt(
                            agent,
                            memory,
                            conversation_id,
                            str(prompt),
                            str(model_override),
                        )
                    elif command == "new_chat":
                        conversation_id = memory.new_conversation("Desktop chat")
                        self.emit("new_chat", {"conversation_id": conversation_id})
                    elif command == "approvals":
                        self.emit("approvals", memory.list_approvals(limit=100))
                    elif command == "decide_approval":
                        approval_id, approve = payload
                        changed = memory.decide_approval(
                            approval_id,
                            approve,
                            ttl_hours=self.config.approval_ttl_hours,
                        )
                        self.emit("approval_decided", {
                            "approval_id": approval_id,
                            "approved": approve,
                            "changed": changed,
                        })
                        self.emit("approvals", memory.list_approvals(limit=100))
        except Exception as exc:
            self.emit(
                "fatal",
                f"Jarvis Desktop could not start ({type(exc).__name__}): {exc}",
            )


class ApprovalWindow(tk.Toplevel):
    def __init__(self, app: "JarvisDesktop", approvals: list[dict[str, Any]]) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Jarvis approvals")
        self.geometry("960x560")
        self.minsize(760, 440)
        self.configure(bg=app.colors["panel"])
        self.transient(app.root)

        header = tk.Frame(self, bg=app.colors["panel"], padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(
            header,
            text="Sensitive action approvals",
            bg=app.colors["panel"],
            fg=app.colors["text"],
            font=("Segoe UI Semibold", 15),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Review the exact target before approving. Approvals are one-shot and scope-bound.",
            bg=app.colors["panel"],
            fg=app.colors["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(3, 0))

        body = tk.Frame(self, bg=app.colors["panel"], padx=18, pady=0)
        body.pack(fill="both", expand=True, pady=(0, 12))
        columns = ("id", "status", "action", "scope", "reason")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", height=10)
        widths = {"id": 55, "status": 90, "action": 150, "scope": 190, "reason": 390}
        for column in columns:
            self.tree.heading(column, text=column.replace("_", " ").title())
            self.tree.column(column, width=widths[column], stretch=column in {"scope", "reason"})
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected)

        self.detail = tk.Text(
            body,
            height=9,
            wrap="word",
            bg=app.colors["surface"],
            fg=app.colors["text"],
            insertbackground=app.colors["text"],
            relief="flat",
            padx=12,
            pady=10,
            font=("Cascadia Mono", 9),
        )
        self.detail.pack(fill="both", expand=True, pady=(10, 0))
        self.detail.configure(state="disabled")

        controls = tk.Frame(self, bg=app.colors["panel"], padx=18, pady=0)
        controls.pack(fill="x", pady=(0, 18))
        self.deny_button = app.make_button(controls, "Deny", self._deny, kind="danger")
        self.deny_button.pack(side="right")
        self.approve_button = app.make_button(controls, "Approve once", self._approve)
        self.approve_button.pack(side="right", padx=(0, 10))

        self.rows: dict[str, dict[str, Any]] = {}
        self.update_rows(approvals)

    def update_rows(self, approvals: list[dict[str, Any]]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.rows.clear()
        for approval in approvals:
            item_id = str(approval.get("id", ""))
            self.rows[item_id] = approval
            self.tree.insert("", "end", iid=item_id, values=(
                item_id,
                compact_activity(approval.get("status", ""), 30),
                compact_activity(approval.get("action", ""), 60),
                compact_activity(approval.get("scope", ""), 80),
                compact_activity(approval.get("reason", ""), 150),
            ))
        if approvals:
            first = str(approvals[0].get("id", ""))
            self.tree.selection_set(first)
            self.tree.focus(first)
            self._show_selected()
        else:
            self._set_detail("No approval records.")

    def _selected(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", safe_ui_text(text, 20_000))
        self.detail.configure(state="disabled")

    def _show_selected(self, _event: Any = None) -> None:
        row = self._selected()
        if row is None:
            return
        self._set_detail(
            f"Approval #{row.get('id')}\n"
            f"Status: {row.get('status')}\n"
            f"Scope: {row.get('scope')}\n"
            f"Task: {row.get('task_id')}\n\n"
            f"Reason\n{row.get('reason', '')}\n\n"
            f"Exact sanitized resource\n{row.get('resource', '')}"
        )
        pending = row.get("status") == "pending"
        self.approve_button.configure(state="normal" if pending else "disabled")
        self.deny_button.configure(state="normal" if pending else "disabled")

    def _decide(self, approve: bool) -> None:
        row = self._selected()
        if row is None or row.get("status") != "pending":
            return
        approval_id = int(row["id"])
        verb = "approve" if approve else "deny"
        if not messagebox.askyesno(
            f"{verb.title()} approval #{approval_id}",
            f"Do you want to {verb} this exact sensitive action?",
            parent=self,
        ):
            return
        self.app.session.decide_approval(approval_id, approve)

    def _approve(self) -> None:
        self._decide(True)

    def _deny(self) -> None:
        self._decide(False)


class JarvisDesktop:
    def __init__(self, root: tk.Tk, config: Config) -> None:
        self.root = root
        self.config = config
        self.colors = {
            "background": "#0A0E14",
            "panel": "#101720",
            "surface": "#151E29",
            "surface_alt": "#1B2735",
            "border": "#263646",
            "text": "#E9F2F8",
            "muted": "#8EA2B4",
            "accent": "#43D5C4",
            "accent_hover": "#65E2D4",
            "user": "#183C48",
            "danger": "#E06C75",
            "danger_hover": "#EE8790",
            "warning": "#E5B567",
        }
        self.busy = False
        self.deep_confirmed = False
        self.approval_window: ApprovalWindow | None = None
        self._closing = False
        self._close_deadline = 0.0

        self.root.title(APP_TITLE)
        self.root.geometry("1180x780")
        self.root.minsize(900, 620)
        self.root.configure(bg=self.colors["background"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_style()
        self._build()

        self.session = JarvisSession(config)
        self.session.start()
        self.root.after(50, self._poll_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Jarvis.TCombobox",
            fieldbackground=self.colors["surface"],
            background=self.colors["surface"],
            foreground=self.colors["text"],
            arrowcolor=self.colors["accent"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            padding=8,
        )
        style.map(
            "Jarvis.TCombobox",
            fieldbackground=[("readonly", self.colors["surface"])],
            foreground=[("readonly", self.colors["text"])],
        )
        style.configure(
            "Treeview",
            background=self.colors["surface"],
            fieldbackground=self.colors["surface"],
            foreground=self.colors["text"],
            rowheight=28,
            bordercolor=self.colors["border"],
        )
        style.configure(
            "Treeview.Heading",
            background=self.colors["surface_alt"],
            foreground=self.colors["text"],
            relief="flat",
        )
        style.map("Treeview", background=[("selected", self.colors["user"])])

    def make_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Any,
        *,
        kind: str = "accent",
        width: int | None = None,
    ) -> tk.Button:
        danger = kind == "danger"
        background = self.colors["danger"] if danger else self.colors["accent"]
        hover = self.colors["danger_hover"] if danger else self.colors["accent_hover"]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg="#071015",
            activebackground=hover,
            activeforeground="#071015",
            disabledforeground=self.colors["muted"],
            relief="flat",
            borderwidth=0,
            padx=15,
            pady=9,
            cursor="hand2",
            font=("Segoe UI Semibold", 9),
            width=width,
        )
        return button

    def _build(self) -> None:
        sidebar = tk.Frame(self.root, bg=self.colors["panel"], width=250)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=self.colors["panel"], padx=20, pady=22)
        brand.pack(fill="x")
        mark = tk.Label(
            brand,
            text="J",
            width=2,
            height=1,
            bg=self.colors["accent"],
            fg="#071015",
            font=("Segoe UI Black", 18),
        )
        mark.pack(side="left")
        brand_text = tk.Frame(brand, bg=self.colors["panel"])
        brand_text.pack(side="left", padx=(11, 0))
        tk.Label(
            brand_text,
            text="JARVIS",
            bg=self.colors["panel"],
            fg=self.colors["text"],
            font=("Segoe UI Semibold", 16),
        ).pack(anchor="w")
        tk.Label(
            brand_text,
            text="LOCAL INTELLIGENCE",
            bg=self.colors["panel"],
            fg=self.colors["accent"],
            font=("Segoe UI Semibold", 7),
        ).pack(anchor="w")

        status_box = tk.Frame(
            sidebar,
            bg=self.colors["surface"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
            padx=13,
            pady=12,
        )
        status_box.pack(fill="x", padx=15, pady=(0, 14))
        status_row = tk.Frame(status_box, bg=self.colors["surface"])
        status_row.pack(fill="x")
        self.status_dot = tk.Canvas(
            status_row,
            width=12,
            height=12,
            bg=self.colors["surface"],
            highlightthickness=0,
        )
        self.status_dot.pack(side="left")
        self.status_oval = self.status_dot.create_oval(2, 2, 10, 10, fill=self.colors["warning"], outline="")
        self.status_var = tk.StringVar(value="Connecting…")
        tk.Label(
            status_row,
            textvariable=self.status_var,
            bg=self.colors["surface"],
            fg=self.colors["text"],
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(7, 0))
        self.activity_var = tk.StringVar(value="Starting model services")
        tk.Label(
            status_box,
            textvariable=self.activity_var,
            bg=self.colors["surface"],
            fg=self.colors["muted"],
            font=("Segoe UI", 8),
            wraplength=195,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self.new_button = self.make_button(sidebar, "+  New conversation", self.new_chat)
        self.new_button.pack(fill="x", padx=15, pady=(0, 16))

        tk.Label(
            sidebar,
            text="MODEL PROFILE",
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=18, pady=(2, 6))
        self.model_var = tk.StringVar(value="Auto")
        self.model_box = ttk.Combobox(
            sidebar,
            textvariable=self.model_var,
            values=MODEL_CHOICES,
            state="readonly",
            style="Jarvis.TCombobox",
        )
        self.model_box.pack(fill="x", padx=15)

        self.model_detail_var = tk.StringVar(value="Task-aware routing")
        tk.Label(
            sidebar,
            textvariable=self.model_detail_var,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=("Segoe UI", 8),
            wraplength=205,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(7, 18))
        self.model_box.bind("<<ComboboxSelected>>", self._model_changed)

        approvals = tk.Button(
            sidebar,
            text="Review approvals",
            command=self.show_approvals,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            activebackground=self.colors["surface_alt"],
            activeforeground=self.colors["text"],
            relief="flat",
            anchor="w",
            padx=18,
            pady=10,
            cursor="hand2",
            font=("Segoe UI", 9),
        )
        approvals.pack(fill="x", side="bottom", pady=(0, 8))
        self.background_var = tk.StringVar(value="Background: checking")
        tk.Label(
            sidebar,
            textvariable=self.background_var,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=("Segoe UI", 8),
        ).pack(side="bottom", anchor="w", padx=18, pady=(0, 4))

        main = tk.Frame(self.root, bg=self.colors["background"])
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=self.colors["background"], padx=28, pady=18)
        header.pack(fill="x")
        tk.Label(
            header,
            text="Conversation",
            bg=self.colors["background"],
            fg=self.colors["text"],
            font=("Segoe UI Semibold", 15),
        ).pack(side="left")
        self.stop_button = self.make_button(header, "Stop", self.stop_request, kind="danger")
        self.stop_button.configure(state="disabled")
        self.stop_button.pack(side="right")

        chat_shell = tk.Frame(
            main,
            bg=self.colors["surface"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
        )
        chat_shell.pack(fill="both", expand=True, padx=28, pady=(0, 14))
        self.chat = tk.Text(
            chat_shell,
            wrap="word",
            bg=self.colors["surface"],
            fg=self.colors["text"],
            relief="flat",
            borderwidth=0,
            padx=22,
            pady=18,
            font=("Segoe UI", 10),
            spacing1=2,
            spacing3=8,
            cursor="arrow",
        )
        scroll = ttk.Scrollbar(chat_shell, orient="vertical", command=self.chat.yview)
        self.chat.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_configure(
            "assistant_label",
            foreground=self.colors["accent"],
            font=("Segoe UI Semibold", 9),
            spacing1=8,
        )
        self.chat.tag_configure(
            "assistant",
            foreground=self.colors["text"],
            lmargin1=4,
            lmargin2=4,
            rmargin=120,
        )
        self.chat.tag_configure(
            "user_label",
            foreground="#8CC7FF",
            font=("Segoe UI Semibold", 9),
            justify="right",
            spacing1=8,
        )
        self.chat.tag_configure(
            "user",
            foreground=self.colors["text"],
            background=self.colors["user"],
            justify="right",
            lmargin1=150,
            lmargin2=150,
            rmargin=4,
        )
        self.chat.tag_configure("warning", foreground=self.colors["warning"])
        self.chat.configure(state="disabled")
        self._append_message(
            "assistant",
            "Desktop interface initializing. Your conversations and approvals remain governed by Jarvis's existing safety controls.",
        )

        composer = tk.Frame(main, bg=self.colors["background"], padx=28, pady=0)
        composer.pack(fill="x")
        input_shell = tk.Frame(
            composer,
            bg=self.colors["surface_alt"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        input_shell.pack(fill="x")
        self.input = tk.Text(
            input_shell,
            height=4,
            wrap="word",
            bg=self.colors["surface_alt"],
            fg=self.colors["text"],
            insertbackground=self.colors["accent"],
            selectbackground=self.colors["user"],
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 10),
            undo=True,
        )
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self._enter_key)
        self.input.bind("<Control-Return>", self._newline_key)
        self.send_button = self.make_button(input_shell, "Send", self.send, width=8)
        self.send_button.pack(side="right", padx=(12, 0), anchor="s")
        tk.Label(
            composer,
            text="Enter to send  •  Ctrl+Enter for a new line  •  sensitive actions still require approval",
            bg=self.colors["background"],
            fg=self.colors["muted"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(6, 0))
        # The transcript was packed first so Tk could allocate it the full
        # remaining cavity on some Windows scaling settings. Repack the fixed
        # composer from the bottom before giving the transcript expandable room.
        composer.pack_forget()
        chat_shell.pack_forget()
        composer.pack(side="bottom", fill="x", pady=(0, 22))
        chat_shell.pack(side="top", fill="both", expand=True, padx=28, pady=(0, 14))
        self.input.focus_set()

    def _append_message(self, role: str, content: Any, status: str = "complete") -> None:
        text = safe_ui_text(content).strip()
        if not text:
            return
        self.chat.configure(state="normal")
        label = "YOU" if role == "user" else "JARVIS"
        self.chat.insert("end", f"{label}\n", f"{role}_label")
        self.chat.insert("end", text + "\n\n", role)
        if status not in {"complete", "cancelled"}:
            self.chat.insert("end", f"Status: {status}\n\n", "warning")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _enter_key(self, event: tk.Event[Any]) -> str:
        if event.state & 0x0004:
            return self._newline_key(event)
        self.send()
        return "break"

    def _newline_key(self, _event: tk.Event[Any]) -> str:
        self.input.insert("insert", "\n")
        return "break"

    def send(self) -> None:
        if self.busy:
            return
        prompt = self.input.get("1.0", "end-1c").strip()
        if not prompt:
            return
        if len(prompt) > 50_000:
            messagebox.showwarning("Prompt too long", "Jarvis accepts at most 50,000 characters.", parent=self.root)
            return
        if self.model_var.get() == "Deep 30B" and not self.deep_confirmed:
            if not messagebox.askyesno(
                "Load the 30B deep model?",
                "Deep mode is higher quality but will temporarily use substantial CPU, RAM, and GPU resources. "
                "It unloads after the request. Continue?",
                parent=self.root,
            ):
                return
            self.deep_confirmed = True
        self.input.delete("1.0", "end")
        self._append_message("user", prompt)
        self.session.submit(prompt, self.model_var.get())

    def stop_request(self) -> None:
        if self.busy:
            self.activity_var.set("Stopping safely…")
            self.session.cancel()

    def new_chat(self) -> None:
        if self.busy:
            messagebox.showinfo("Jarvis is busy", "Stop or finish the current request first.", parent=self.root)
            return
        self.session.new_chat()

    def show_approvals(self) -> None:
        self.session.request_approvals()

    def _model_changed(self, _event: Any = None) -> None:
        details = {
            "Auto": "Task-aware routing",
            "Fast": f"Low latency • {self.config.fast_model}",
            "Reasoning": f"Analysis and research • {self.config.reasoning_model}",
            "Coding": f"Build and verify • {self.config.coding_model}",
            "Deep 30B": f"Manual heavy mode • {self.config.deep_model}",
        }
        self.model_detail_var.set(details.get(self.model_var.get(), "Task-aware routing"))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.send_button.configure(state="disabled" if busy else "normal")
        self.new_button.configure(state="disabled" if busy else "normal")
        self.model_box.configure(state="disabled" if busy else "readonly")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.status_var.set("Working" if busy else "Online")
        self.status_dot.itemconfigure(
            self.status_oval,
            fill=self.colors["warning"] if busy else self.colors["accent"],
        )

    def _handle_event(self, event: SessionEvent) -> None:
        if event.kind == "ready":
            self._set_busy(False)
            control = str(event.payload.get("control_state", "running"))
            self.background_var.set(f"Background: {control}")
            self.activity_var.set("Ready")
        elif event.kind == "busy":
            self._set_busy(bool(event.payload))
        elif event.kind == "activity":
            self.activity_var.set(compact_activity(event.payload))
        elif event.kind == "assistant":
            payload = event.payload
            self._append_message(
                "assistant",
                payload.get("content", ""),
                str(payload.get("status", "complete")),
            )
            if payload.get("approval_id") is not None:
                self.session.request_approvals()
        elif event.kind == "error":
            self._append_message("assistant", event.payload, "error")
        elif event.kind == "fatal":
            self.status_var.set("Offline")
            self.status_dot.itemconfigure(self.status_oval, fill=self.colors["danger"])
            messagebox.showerror("Jarvis Desktop", safe_ui_text(event.payload, 2_000), parent=self.root)
        elif event.kind == "new_chat":
            self.chat.configure(state="normal")
            self.chat.delete("1.0", "end")
            self.chat.configure(state="disabled")
            self._append_message("assistant", "New conversation started. What are we working on?")
            self.input.focus_set()
        elif event.kind == "approvals":
            rows = list(event.payload or [])
            if self.approval_window is None or not self.approval_window.winfo_exists():
                self.approval_window = ApprovalWindow(self, rows)
            else:
                self.approval_window.update_rows(rows)
                self.approval_window.lift()
        elif event.kind == "approval_decided":
            payload = event.payload
            if payload.get("changed"):
                decision = "approved" if payload.get("approved") else "denied"
                self.activity_var.set(f"Approval #{payload.get('approval_id')} {decision}")
            else:
                self.activity_var.set("Approval was already decided or expired")

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.session.events.get_nowait())
        except queue.Empty:
            pass
        if self._closing:
            if not self.session.is_alive() or time.monotonic() >= self._close_deadline:
                self.root.destroy()
                return
        self.root.after(50, self._poll_events)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._close_deadline = time.monotonic() + 3.0
        self.status_var.set("Closing…")
        self.session.shutdown()


def run_desktop_ui() -> int:
    root: tk.Tk | None = None
    try:
        config = Config.load()
        root = tk.Tk()
        JarvisDesktop(root, config)
        root.mainloop()
        return 0
    except Exception as exc:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        try:
            hidden = tk.Tk()
            hidden.withdraw()
            messagebox.showerror(
                APP_TITLE,
                safe_ui_text(f"Jarvis Desktop could not start ({type(exc).__name__}): {exc}", 2_000),
                parent=hidden,
            )
            hidden.destroy()
        except Exception:
            pass
        return 1


def main() -> int:
    from .provider_setup import ProviderSetupRequired, ensure_ready

    terminal = bool(getattr(sys.stdin, "isatty", lambda: False)())
    try:
        ensure_ready(interactive=terminal, stdin_isatty=terminal)
    except ProviderSetupRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return run_desktop_ui()


if __name__ == "__main__":
    raise SystemExit(main())
