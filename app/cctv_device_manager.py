from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from device_data import (
    DEVICE_TYPE_LABELS,
    DEVICE_TYPES,
    HOST_DEVICE_TYPES,
    TYPE_DEFAULTS,
    SUPPORTED_SPREADSHEET_SUFFIXES,
    cabinet_local_id,
    default_row,
    migrate_camera_points,
    parse_map_element_ids,
    read_cameras_from_spreadsheet,
    read_devices,
    serialize_map_element_ids,
    summarize_devices,
    technical_host_name,
    upsert_devices,
    valid_ip,
    write_devices,
)

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
BACKUP_DIR = ROOT_DIR / "backups"
LOG_DIR = ROOT_DIR / "logs"
ICON_DIR = ROOT_DIR / "icons"
CONFIG_PATH = ROOT_DIR / "USTAWIENIA.json"
CONFIG_TEMPLATE_PATH = ROOT_DIR / "USTAWIENIA_V2_TEMPLATE.json"
DEVICES_PATH = DATA_DIR / "devices.csv"
OLD_CAMERA_POINTS_PATH = DATA_DIR / "camera_points.csv"
ODS_PATH = DATA_DIR / "camera_inventory.ods"
MAP_IMAGE_PATH = DATA_DIR / "camera_map.png"
INFRASTRUCTURE_MAP_IMAGE_PATH = DATA_DIR / "infrastructure_map.png"
CLICKER_PATH = APP_DIR / "device_map_clicker.py"
MAP_SYNC_PATH = ROOT_DIR / "tools" / "sync_zabbix_device_map.ps1"

DEFAULT_CONFIG = {
    # Wersja publiczna nie zawiera ustawień ani poświadczeń żadnej organizacji.
    # Dopóki te pola są puste, aplikacja działa wyłącznie lokalnie i nie próbuje
    # łączyć się z Zabbixem.
    "zabbix_url": "",
    "zabbix_user": "",
    "zabbix_password": "",
    "zabbix_map_name": "",
    "map_targets": {
        "camera": "",
        "recorder": "",
        "switch": "",
        "cabinet": "",
    },
    "camera_spreadsheet_path": "",
    "types": TYPE_DEFAULTS,
}

for directory in (DATA_DIR, BACKUP_DIR, LOG_DIR, ICON_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_TEMPLATE_PATH.is_file():
        try:
            with CONFIG_TEMPLATE_PATH.open("r", encoding="utf-8-sig") as file:
                config = deep_merge(config, json.load(file))
        except Exception:
            pass
    if CONFIG_PATH.is_file():
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as file:
            config = deep_merge(config, json.load(file))
    # Migracja ustawień ze starszych wersji:
    # dla kamer używamy jawnej nazwy obrazu Zabbixa CAM.
    camera_config = config.setdefault("types", {}).setdefault("camera", {})
    if not str(camera_config.get("icon") or "").strip():
        camera_config["icon"] = "CAM"
    targets = config.setdefault("map_targets", {})
    targets.setdefault("camera", str(config.get("zabbix_map_name") or ""))
    for device_type in ("recorder", "switch", "cabinet"):
        targets.setdefault(device_type, "")

    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )


def backup_file(path: Path, category: str) -> Path | None:
    if not path.is_file():
        return None
    destination_dir = BACKUP_DIR / "files" / category
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{path.stem}__backup_{timestamp()}{path.suffix}"
    shutil.copy2(path, destination)
    return destination


def build_camera_name_import_plan(
    existing_rows: list[dict],
    incoming_cameras: list[dict],
    *,
    preserve_existing_names_for_ambiguous_ips: set[str] | None = None,
) -> dict:
    """Zmienia wyłącznie VisibleName kamer odczytanych z aktualnego arkusza."""
    rows = [dict(row) for row in existing_rows]
    ambiguous_ips = preserve_existing_names_for_ambiguous_ips or set()
    existing_by_ip = {
        row["IP"]: row for row in rows if row.get("Type") == "camera" and row.get("IP")
    }
    updates: list[dict] = []
    additions: list[dict] = []
    preserved_ambiguous: list[dict] = []
    for incoming in incoming_cameras:
        ip = incoming["IP"]
        current = existing_by_ip.get(ip)
        if current is None:
            added = dict(incoming)
            added["Source"] = "ARKUSZ — oczekuje Zabbixa"
            added["Origin"] = "ARKUSZ"
            rows.append(added)
            existing_by_ip[ip] = added
            additions.append({"ip": ip, "desired": added["VisibleName"]})
            continue
        desired = incoming["VisibleName"]
        if current["VisibleName"] != desired:
            if ip in ambiguous_ips:
                # W arkuszu są dwie różne, potwierdzone nazwy dla tego IP.
                # Obecna nazwa została już wcześniej zaakceptowana i jest
                # bezpieczniejsza niż automatyczne skrócenie jej do 222.xxx.
                preserved_ambiguous.append(
                    {
                        "ip": ip,
                        "current": current["VisibleName"],
                        "ignored": desired,
                    }
                )
                continue
            updates.append(
                {
                    "ip": ip,
                    "current": current["VisibleName"],
                    "desired": desired,
                }
            )
            current["VisibleName"] = desired
            # Od tej chwili nazwa jest lokalną propozycją z arkusza. Po
            # potwierdzonym zapisie w Zabbixie źródło wróci do ZABBIX.
            current["Source"] = "ARKUSZ — oczekuje Zabbixa"
    return {
        "rows": rows,
        "updates": updates,
        "additions": additions,
        "preserved_ambiguous": preserved_ambiguous,
    }


def write_camera_name_import_report(plan: dict, diagnostics: dict) -> Path:
    report_dir = BACKUP_DIR / "imports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"camera_names_from_f_j_{timestamp()}.txt"
    lines = [
        "SYNCHRONIZACJA NAZW KAMER Z KOLUMN F/J",
        "",
        "Regula:",
        "- F zgodne z koncowka IP + J: uzyj dokladnej nazwy z F.",
        "- sama koncowka IP, IPCamera albo inna niepotwierdzona nazwa: uzyj 222.xxx.",
        "",
        f"Potwierdzone nazwy F/J: {diagnostics['confirmed_names']}",
        f"Nazwy skracane do IP: {diagnostics['short_names']}",
        f"IP odczytane z początku F: {len(diagnostics.get('ip_recovered_from_name', []))}",
        f"Zmiany w devices.csv: {len(plan['updates'])}",
        f"Nowe kamery: {len(plan['additions'])}",
        f"Zachowane przy konflikcie w arkuszu: {len(plan['preserved_ambiguous'])}",
        "",
        "ZMIANY:",
    ]
    for item in plan["updates"]:
        lines.append(f"{item['ip']}: {item['current']!r} -> {item['desired']!r}")
    if plan["preserved_ambiguous"]:
        lines.extend(["", "ZACHOWANE PRZY KONFLIKCIE:"])
        for item in plan["preserved_ambiguous"]:
            lines.append(
                f"{item['ip']}: pozostawiono {item['current']!r}; "
                f"nie użyto {item['ignored']!r}"
            )
    if plan["additions"]:
        lines.extend(["", "NOWE KAMERY:"])
        for item in plan["additions"]:
            lines.append(f"{item['ip']}: {item['desired']!r}")
    if diagnostics["warnings"]:
        lines.extend(["", "UWAGI DO ARKUSZA:", *diagnostics["warnings"]])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


class ZabbixApi:
    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.user = user
        self.password = password
        self.token: str | None = None
        self.request_id = 1

    def call(self, method: str, params: dict, *, token: bool = True):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self.request_id,
        }
        self.request_id += 1
        headers = {"Content-Type": "application/json-rpc; charset=utf-8"}
        if token and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Nie można połączyć się z Zabbix API: {exc}") from exc
        if "error" in result:
            error = result["error"]
            raise RuntimeError(
                f"Zabbix API [{method}]: {error.get('message', 'błąd')} / "
                f"{error.get('data', '')}"
            )
        return result.get("result")

    def login(self) -> None:
        try:
            self.token = self.call(
                "user.login",
                {"username": self.user, "password": self.password},
                token=False,
            )
        except RuntimeError:
            self.token = self.call(
                "user.login",
                {"user": self.user, "password": self.password},
                token=False,
            )


class AddDeviceDialog(tk.Toplevel):
    def __init__(self, parent, initial_type: str, config: dict):
        super().__init__(parent)
        self.title("Dodaj urządzenie")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.config_data = config

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Typ:").grid(row=0, column=0, sticky="w", pady=4)
        self.type_var = tk.StringVar(value=initial_type)
        type_combo = ttk.Combobox(
            frame,
            textvariable=self.type_var,
            values=list(DEVICE_TYPES),
            state="readonly",
            width=26,
        )
        type_combo.grid(row=0, column=1, sticky="ew", pady=4)
        type_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_defaults())

        self.ip_label = ttk.Label(frame, text="Adres IP:")
        self.ip_label.grid(row=1, column=0, sticky="w", pady=4)
        self.ip_var = tk.StringVar()
        self.ip_entry = ttk.Entry(frame, textvariable=self.ip_var, width=36)
        self.ip_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.ip_entry.bind("<KeyRelease>", lambda _event: self.refresh_defaults())

        self.name_label = ttk.Label(frame, text="Widoczna nazwa:")
        self.name_label.grid(row=2, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.name_var, width=36).grid(
            row=2, column=1, sticky="ew", pady=4
        )

        self.host_label = ttk.Label(frame, text="Techniczny Host name:")
        self.host_label.grid(
            row=3, column=0, sticky="w", pady=4
        )
        self.host_var = tk.StringVar()
        self.host_entry = ttk.Entry(frame, textvariable=self.host_var, width=36)
        self.host_entry.grid(
            row=3, column=1, sticky="ew", pady=4
        )

        self.create_host_var = tk.BooleanVar(value=True)
        self.create_host_check = ttk.Checkbutton(
            frame,
            text="Utwórz host w Zabbixie, jeżeli nie istnieje",
            variable=self.create_host_var,
        )
        self.create_host_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 4))

        self.details_var = tk.StringVar()
        ttk.Label(
            frame,
            textvariable=self.details_var,
            wraplength=440,
            foreground="#555555",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(3, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Anuluj", command=self.destroy).pack(
            side="right", padx=4
        )
        ttk.Button(buttons, text="Dodaj", command=self.accept).pack(
            side="right", padx=4
        )
        frame.columnconfigure(1, weight=1)
        self.refresh_defaults()
        self.ip_entry.focus_set()
        self.wait_window()

    def refresh_defaults(self) -> None:
        device_type = self.type_var.get()
        is_cabinet = device_type == "cabinet"
        ip = self.ip_var.get().strip()
        if not is_cabinet and ip and valid_ip(ip):
            self.host_var.set(technical_host_name(device_type, ip))
        type_config = self.config_data["types"][device_type]
        if is_cabinet:
            self.ip_var.set("")
            self.host_var.set("")
            self.ip_label.configure(text="Adres IP: — nie dotyczy")
            self.name_label.configure(text="Nazwa szafy:")
            self.host_label.configure(text="Identyfikator lokalny: — automatyczny")
            self.ip_entry.configure(state="disabled")
            self.host_entry.configure(state="disabled")
            self.create_host_var.set(False)
            self.create_host_check.configure(state="disabled")
            self.details_var.set(
                "Szafa jest tylko ikoną na mapie infrastruktury. Program nie utworzy "
                "hosta, IP, pingu, grupy ani taga w Zabbixie."
            )
        else:
            self.ip_label.configure(text="Adres IP:")
            self.name_label.configure(text="Widoczna nazwa:")
            self.host_label.configure(text="Techniczny Host name:")
            self.ip_entry.configure(state="normal")
            self.host_entry.configure(state="normal")
            self.create_host_check.configure(state="normal")
            self.details_var.set(
                f"Grupa: {type_config['group']} | Template: {type_config['template']} | "
                f"Tag type={type_config['tag']} | "
                f"Ikona: {type_config.get('icon') or 'wzorzec kamery'}"
            )

    def accept(self) -> None:
        device_type = self.type_var.get()
        ip = self.ip_var.get().strip()
        visible_name = self.name_var.get().strip()
        host_name = self.host_var.get().strip()
        if device_type != "cabinet" and not valid_ip(ip):
            messagebox.showerror("Niepoprawny IP", "Podaj poprawny adres IP.", parent=self)
            return
        if not visible_name:
            messagebox.showerror("Brak nazwy", "Podaj nazwę.", parent=self)
            return
        if device_type != "cabinet" and not host_name:
            messagebox.showerror("Brak Host name", "Podaj techniczną nazwę hosta.", parent=self)
            return
        self.result = {
            "type": device_type,
            "ip": ip,
            "visible_name": visible_name,
            "host_name": host_name,
            "create_host": self.create_host_var.get(),
        }
        self.destroy()


class MapSelectionDialog(tk.Toplevel):
    """Wybór rzeczywiście istniejącej mapy pobranej z Zabbixa."""

    def __init__(self, parent, maps: list[str], preferred: str, device_type: str):
        super().__init__(parent)
        self.title("Wybierz mapę Zabbixa")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: str | None = None

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=(
                f"Wybierz docelową mapę dla: {DEVICE_TYPE_LABELS[device_type]}.\n"
                "Lista pochodzi bezpośrednio z aktualnych map w Zabbixie."
            ),
            justify="left",
        ).pack(anchor="w")
        self.value = tk.StringVar(value=preferred if preferred in maps else maps[0])
        combo = ttk.Combobox(
            frame,
            textvariable=self.value,
            values=maps,
            state="readonly",
            width=46,
        )
        combo.pack(fill="x", pady=(10, 8))
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e")
        ttk.Button(buttons, text="Anuluj", command=self.destroy).pack(
            side="right", padx=4
        )
        ttk.Button(buttons, text="Wybierz", command=self.accept).pack(
            side="right", padx=4
        )
        combo.focus_set()
        self.wait_window()

    def accept(self) -> None:
        value = self.value.get().strip()
        if value:
            self.result = value
        self.destroy()


class DeviceManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CCTV Device Map Tool")
        self.geometry("1180x840")
        self.minsize(1000, 720)

        self.config_data = load_config()
        migrate_camera_points(OLD_CAMERA_POINTS_PATH, DEVICES_PATH)
        self.busy = False
        self.selected_type = tk.StringVar(value="camera")
        self.status_var = tk.StringVar(value="Gotowe.")
        self.build_ui()
        self.refresh_view()
        if all(
            str(self.config_data.get(key) or "").strip()
            for key in ("zabbix_url", "zabbix_user", "zabbix_password")
        ):
            self.after(
                250,
                lambda: self.start_background(
                    lambda: self.refresh_devices_from_zabbix(startup=True)
                ),
            )
        else:
            self.log("Offline mode — Zabbix connection is not configured.")

    def build_ui(self) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)
        ttk.Label(
            main,
            text="CCTV Device Map Tool",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            main,
            text=(
                "Offline mode — Zabbix connection is not configured."
            ),
        ).pack(anchor="w", pady=(2, 10))

        selector = ttk.LabelFrame(main, text="Aktualnie edytowany typ", padding=10)
        selector.pack(fill="x")
        for value in DEVICE_TYPES:
            ttk.Radiobutton(
                selector,
                text=DEVICE_TYPE_LABELS[value],
                value=value,
                variable=self.selected_type,
                command=self.refresh_view,
            ).pack(side="left", padx=10)
        self.summary_label = ttk.Label(selector, font=("Segoe UI", 10, "bold"))
        self.summary_label.pack(side="right", padx=8)

        source_frame = ttk.LabelFrame(main, text="Źródła danych", padding=10)
        source_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(
            source_frame,
            text="Synchronizuj nazwy kamer z arkusza",
            command=self.choose_spreadsheet,
        ).pack(side="left", padx=4)
        ttk.Button(
            source_frame,
            text="Pobierz teraz urządzenia z Zabbixa",
            command=lambda: self.start_background(self.refresh_devices_from_zabbix),
        ).pack(side="left", padx=4)
        ttk.Button(
            source_frame,
            text="Dodaj urządzenie ręcznie",
            command=self.add_device_manually,
        ).pack(side="left", padx=4)
        ttk.Button(
            source_frame,
            text="Edytuj nazwę zaznaczonego urządzenia",
            command=self.edit_selected_device_name,
        ).pack(side="left", padx=4)
        ttk.Button(
            source_frame,
            text="Usuń zaznaczone ręcznie dodane urządzenie",
            command=self.confirm_delete_selected_manual_device,
        ).pack(side="left", padx=4)
        ttk.Button(
            source_frame,
            text="Sprawdź / wgraj ikony",
            command=lambda: self.start_background(self.ensure_zabbix_icons),
        ).pack(side="left", padx=4)
        actions = ttk.LabelFrame(main, text="Lokalizacja i synchronizacja", padding=10)
        actions.pack(fill="x", pady=(10, 0))
        row1 = ttk.Frame(actions)
        row1.pack(fill="x")
        ttk.Button(
            row1,
            text="1. Oznacz / popraw lokalizacje wybranego typu",
            command=self.launch_clicker,
        ).pack(side="left", padx=4, pady=3)
        ttk.Button(
            row1,
            text="2. Sprawdź mapę wybranego typu",
            command=self.check_selected_map,
        ).pack(side="left", padx=4, pady=3)
        ttk.Button(
            row1,
            text="3. Importuj wybrany typ do mapy",
            command=self.confirm_selected_map_sync,
        ).pack(side="left", padx=4, pady=3)

        row2 = ttk.Frame(actions)
        row2.pack(fill="x")
        ttk.Button(
            row2,
            text="4. Sprawdź nazwy wybranego typu",
            command=self.check_selected_names,
        ).pack(side="left", padx=4, pady=3)
        ttk.Button(
            row2,
            text="5. Zaktualizuj nazwy wybranego typu",
            command=self.confirm_selected_names_sync,
        ).pack(side="left", padx=4, pady=3)
        ttk.Button(
            row2,
            text="6. Pełna synchronizacja wszystkich typów",
            command=self.confirm_full_sync,
        ).pack(side="left", padx=4, pady=3)

        table_frame = ttk.LabelFrame(main, text="Urządzenia wybranego typu", padding=8)
        table_frame.pack(fill="both", expand=True, pady=(10, 0))
        columns = ("status", "ip", "host", "name", "source")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        for column, title in zip(
            columns,
            ("Mapa", "IP", "Host name", "Widoczna nazwa", "Źródło"),
        ):
            self.tree.heading(column, text=title)
        self.tree.column("status", width=80, anchor="center")
        self.tree.column("ip", width=125, anchor="center")
        self.tree.column("host", width=210)
        self.tree.column("name", width=310)
        self.tree.column("source", width=130)
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected_device_name())

        lower = ttk.Frame(main)
        lower.pack(fill="x", pady=(8, 0))
        ttk.Button(lower, text="Odśwież widok", command=self.refresh_view).pack(
            side="left", padx=4
        )
        ttk.Button(
            lower, text="Otwórz folder data", command=lambda: self.open_folder(DATA_DIR)
        ).pack(side="left", padx=4)
        ttk.Button(
            lower,
            text="Otwórz backupy",
            command=lambda: self.open_folder(BACKUP_DIR),
        ).pack(side="left", padx=4)
        ttk.Button(lower, text="Ustawienia Zabbixa", command=self.edit_settings).pack(
            side="left", padx=4
        )
        ttk.Label(lower, textvariable=self.status_var).pack(side="right", padx=4)

        output_frame = ttk.LabelFrame(main, text="Log działania", padding=8)
        output_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.output = tk.Text(
            output_frame,
            wrap="word",
            height=9,
            font=("Consolas", 9),
            state="disabled",
        )
        output_scroll = ttk.Scrollbar(
            output_frame, orient="vertical", command=self.output.yview
        )
        self.output.configure(yscrollcommand=output_scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        output_scroll.pack(side="right", fill="y")

    def log(self, text: str) -> None:
        text = text.rstrip()

        def append():
            self.output.configure(state="normal")
            self.output.insert("end", text + "\n")
            self.output.see("end")
            self.output.configure(state="disabled")

        self.after(0, append)
        path = LOG_DIR / f"cctv_devices_{datetime.now():%Y-%m-%d}.log"
        with path.open("a", encoding="utf-8") as file:
            file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")

    def start_background(self, function) -> None:
        if self.busy:
            messagebox.showwarning("Program pracuje", "Poczekaj na zakończenie działania.")
            return

        def runner():
            self.busy = True
            self.after(0, lambda: self.status_var.set("Trwa działanie…"))
            try:
                function()
            except Exception as exc:
                error_message = str(exc)
                error_traceback = traceback.format_exc()

                self.log(f"BŁĄD: {error_message}")
                self.log(error_traceback)

                self.after(
                    0,
                    lambda message=error_message: messagebox.showerror(
                        "Błąd",
                        message,
                    ),
                )
            finally:
                self.busy = False
                self.after(0, lambda: self.status_var.set("Gotowe."))
                self.after(0, self.refresh_view)

        threading.Thread(target=runner, daemon=True).start()

    def check_selected_map(self) -> None:
        device_type = self.selected_type.get()
        map_name = self.choose_target_map(device_type)
        if not map_name:
            return

        self.start_background(
            lambda selected=device_type, selected_map=map_name: self.map_sync(
                selected,
                selected_map,
                apply=False,
            )
        )

    def check_selected_names(self) -> None:
        device_type = self.selected_type.get()
        if device_type not in HOST_DEVICE_TYPES:
            messagebox.showinfo(
                "Szafy bez hostów",
                "Szafy nie mają hostów ani nazw do aktualizacji w Zabbixie. "
                "Możesz jedynie oznaczyć ich pozycję i zaimportować ikonę na mapę.",
            )
            return

        self.start_background(
            lambda selected=device_type: self.names_sync(
                [selected],
                apply=False,
            )
        )

    def api(self) -> ZabbixApi:
        if not all(
            str(self.config_data.get(key) or "").strip()
            for key in ("zabbix_url", "zabbix_user", "zabbix_password")
        ):
            raise RuntimeError(
                "Offline mode — Zabbix connection is not configured."
            )
        api = ZabbixApi(
            self.config_data["zabbix_url"],
            self.config_data["zabbix_user"],
            self.config_data["zabbix_password"],
        )
        api.login()
        return api

    def preferred_map_name(self, device_type: str) -> str:
        return str(
            self.config_data.get("map_targets", {}).get(
                device_type,
                self.config_data.get("zabbix_map_name", ""),
            )
        ).strip()

    def choose_target_map(self, device_type: str) -> str | None:
        """Pobiera listę aktualnych map i zwraca świadomie wybraną nazwę."""
        try:
            api = self.api()
            maps = api.call(
                "map.get",
                {"output": ["sysmapid", "name"], "sortfield": "name"},
            ) or []
        except Exception as exc:
            messagebox.showerror("Nie można pobrać map", str(exc))
            return None
        names = sorted(
            {str(item.get("name") or "").strip() for item in maps if item.get("name")},
            key=str.casefold,
        )
        if not names:
            messagebox.showerror("Brak map", "W Zabbixie nie ma żadnych dostępnych map.")
            return None
        dialog = MapSelectionDialog(
            self,
            names,
            self.preferred_map_name(device_type),
            device_type,
        )
        if dialog.result:
            self.config_data.setdefault("map_targets", {})[device_type] = dialog.result
            save_config(self.config_data)
        return dialog.result

    def map_image_path_for_type(self, device_type: str) -> Path:
        if device_type == "camera":
            return MAP_IMAGE_PATH
        if not INFRASTRUCTURE_MAP_IMAGE_PATH.is_file() and MAP_IMAGE_PATH.is_file():
            shutil.copy2(MAP_IMAGE_PATH, INFRASTRUCTURE_MAP_IMAGE_PATH)
        return INFRASTRUCTURE_MAP_IMAGE_PATH

    def refresh_view(self) -> None:
        rows = read_devices(DEVICES_PATH)
        selected = self.selected_type.get()
        summary = summarize_devices(rows)[selected]
        sources = {
            str(row.get("Source") or "UNKNOWN").upper()
            for row in rows
            if row["Type"] == selected
        }
        source_note = ", ".join(sorted(sources)) if sources else "brak"
        self.summary_label.configure(
            text=(
                f"{DEVICE_TYPE_LABELS[selected]}: {summary['total']} | "
                f"PLACED={summary['PLACED']} | SKIPPED={summary['SKIPPED']} | "
                f"PENDING={summary['PENDING']} | źródło: {source_note}"
            )
        )
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tree_rows_by_iid: dict[str, dict] = {}
        for index, row in enumerate(row for row in rows if row["Type"] == selected):
            item_id = str(index)
            self.tree_rows_by_iid[item_id] = dict(row)
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    row["MapStatus"],
                    row["IP"],
                    row["HostName"],
                    row["VisibleName"],
                    row["Source"],
                ),
            )

    def current_spreadsheet_path(self) -> Path:
        configured = str(
            self.config_data.get("camera_spreadsheet_path")
            or ""
        ).strip()
        if not configured:
            return DATA_DIR / "camera_inventory.ods"
        path = Path(configured)
        return path if path.is_absolute() else ROOT_DIR / path

    def choose_spreadsheet(self) -> None:
        selected = filedialog.askopenfilename(
            title="Wybierz arkusz kamer",
            filetypes=[
                ("Obsługiwane arkusze", "*.ods *.xlsx *.xls *.csv"),
                ("OpenDocument Spreadsheet", "*.ods"),
                ("Excel", "*.xlsx *.xls"),
                ("CSV", "*.csv"),
                ("Wszystkie pliki", "*.*"),
            ],
        )
        if not selected:
            return
        selected_path = Path(selected)
        suffix = selected_path.suffix.lower()
        if suffix not in SUPPORTED_SPREADSHEET_SUFFIXES:
            messagebox.showerror(
                "Nieobsługiwany format",
                "Wybierz plik ODS, XLSX, XLS albo CSV.",
            )
            return
        try:
            cameras, diagnostics = read_cameras_from_spreadsheet(
                selected_path, return_diagnostics=True
            )
            plan = build_camera_name_import_plan(
                read_devices(DEVICES_PATH),
                cameras,
                preserve_existing_names_for_ambiguous_ips=set(
                    diagnostics["ambiguous_name_ips"]
                ),
            )
        except Exception as exc:
            self.log(f"BŁĄD ODCZYTU ARKUSZA: {exc}")
            messagebox.showerror("Nie udało się odczytać arkusza", str(exc))
            return

        warning_note = ""
        if diagnostics["warnings"]:
            warning_note = "\n\nUwagi do arkusza: " + str(len(diagnostics["warnings"]))
        preview = (
            "Plan aktualizacji nazw z kolumn F/J:\n\n"
            f"Kamery w arkuszu: {len(cameras)}\n"
            f"Potwierdzone F/J: {diagnostics['confirmed_names']}\n"
            f"Skracane do 222.xxx: {diagnostics['short_names']}\n"
            f"IP wykryte z początku F: {len(diagnostics.get('ip_recovered_from_name', []))}\n"
            f"Nazwy do zmiany lokalnie: {len(plan['updates'])}\n"
            f"Nowe kamery: {len(plan['additions'])}\n"
            f"Zachowane przy konflikcie: {len(plan['preserved_ambiguous'])}"
            f"{warning_note}\n\n"
            "Teraz zmieni się tylko devices.csv. IP, techniczne Host name, "
            "statusy i pozycje na mapie pozostaną bez zmian.\n\n"
            "Zastosować ten plan?"
        )
        if not messagebox.askyesno("Potwierdź plan nazw", preview):
            return

        previous_spreadsheet = self.current_spreadsheet_path()
        spreadsheet_backup = backup_file(previous_spreadsheet, "spreadsheets")
        devices_backup = backup_file(DEVICES_PATH, "devices")
        destination = DATA_DIR / f"camera_inventory{suffix}"
        if destination != previous_spreadsheet:
            backup_file(destination, "spreadsheets")
        if selected_path.resolve() != destination.resolve():
            shutil.copy2(selected_path, destination)
        self.config_data["camera_spreadsheet_path"] = str(
            destination.relative_to(ROOT_DIR)
        ).replace("\\", "/")
        save_config(self.config_data)
        write_devices(DEVICES_PATH, plan["rows"])
        report_path = write_camera_name_import_report(plan, diagnostics)
        self.log(f"Zsynchronizowano nazwy z arkusza {suffix.upper().lstrip('.')}: {selected_path}")
        self.log(
            f"Kamery={len(cameras)}, nowe={len(plan['additions'])}, "
            f"nazwy_zmienione={len(plan['updates'])}, "
            f"zachowane_przy_konflikcie={len(plan['preserved_ambiguous'])}"
        )
        self.log(f"Raport nazw: {report_path}")
        for warning in diagnostics["warnings"]:
            self.log(f"[UWAGA ARKUSZ] {warning}")
        if spreadsheet_backup:
            self.log(f"Backup poprzedniego arkusza: {spreadsheet_backup}")
        if devices_backup:
            self.log(f"Backup devices.csv: {devices_backup}")
        messagebox.showinfo(
            "Nazwy zapisane lokalnie",
            f"Kamery: {len(cameras)}\nNowe: {len(plan['additions'])}\n"
            f"Nazwy zmienione: {len(plan['updates'])}\n"
            f"Zachowane przy konflikcie: {len(plan['preserved_ambiguous'])}\n\n"
            "Teraz wybierz Kamery i najpierw kliknij\n"
            "„4. Sprawdź nazwy wybranego typu”.\n"
            "Mapa nie została zmieniona.",
        )
        self.refresh_view()

    def choose_ods(self) -> None:
        """Zgodność z wywołaniami ze starszych wersji."""
        self.choose_spreadsheet()

    def find_group_id(self, api: ZabbixApi, group_name: str) -> str:
        groups = api.call(
            "hostgroup.get",
            {"output": ["groupid", "name"], "filter": {"name": [group_name]}},
        ) or []
        if len(groups) != 1:
            raise RuntimeError(f"Nie znaleziono dokładnie jednej grupy: {group_name}")
        return str(groups[0]["groupid"])

    def find_template_id(self, api: ZabbixApi, template_name: str) -> str:
        templates = api.call(
            "template.get",
            {
                "output": ["templateid", "host", "name"],
                "filter": {"host": [template_name]},
            },
        ) or []
        if not templates:
            templates = api.call(
                "template.get",
                {
                    "output": ["templateid", "host", "name"],
                    "filter": {"name": [template_name]},
                },
            ) or []
        if len(templates) != 1:
            raise RuntimeError(
                f"Nie znaleziono dokładnie jednego template '{template_name}'."
            )
        return str(templates[0]["templateid"])

    @staticmethod
    def interface_ip(host: dict) -> str:
        interfaces = host.get("interfaces") or []
        for interface in interfaces:
            if str(interface.get("main")) == "1":
                value = (
                    interface.get("ip")
                    if str(interface.get("useip")) == "1"
                    else interface.get("dns")
                )
                if value and valid_ip(str(value)):
                    return str(value)
        for interface in interfaces:
            value = interface.get("ip")
            if value and valid_ip(str(value)):
                return str(value)
        for text in (str(host.get("host") or ""), str(host.get("name") or "")):
            match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
            if match and valid_ip(match.group(0)):
                return match.group(0)
        return ""

    def refresh_devices_from_zabbix(self, *, startup: bool = False) -> None:
        self.log(
            "=== START: ODCZYT URZĄDZEŃ Z ZABBIxa ==="
            if startup
            else "=== ODSWIEŻANIE URZĄDZEŃ Z ZABBIxa ==="
        )
        try:
            api = self.api()
        except Exception as exc:
            if startup:
                self.log(
                    f"[START] Nie udało się odczytać Zabbixa: {exc}. "
                    "Pokazano ostatnią lokalną listę; szafy i pozycje mapy nie zostały zmienione."
                )
                return
            raise
        incoming = []
        for device_type in HOST_DEVICE_TYPES:
            type_config = self.config_data["types"][device_type]
            group_id = self.find_group_id(api, type_config["group"])
            hosts = api.call(
                "host.get",
                {
                    "output": ["hostid", "host", "name", "status"],
                    "groupids": [group_id],
                    "selectInterfaces": [
                        "interfaceid",
                        "main",
                        "type",
                        "useip",
                        "ip",
                        "dns",
                        "port",
                    ],
                    "selectTags": "extend",
                },
            ) or []
            imported = 0
            for host in hosts:
                ip = self.interface_ip(host)
                if not ip:
                    self.log(f"[POMINIĘTO] Brak IP: {host.get('host')}")
                    continue
                row = default_row(
                    device_type,
                    ip,
                    str(host["host"]),
                    str(host.get("name") or host["host"]),
                    source="ZABBIX",
                )
                row["GroupName"] = type_config["group"]
                incoming.append(row)
                imported += 1
            self.log(f"{DEVICE_TYPE_LABELS[device_type]}: pobrano {imported} hostów.")

        backup = backup_file(DEVICES_PATH, "devices")
        merged = upsert_devices(
            read_devices(DEVICES_PATH),
            incoming,
            preserve_ods_camera_names=True,
        )
        write_devices(DEVICES_PATH, merged["rows"])
        if backup:
            self.log(f"Backup devices.csv: {backup}")
        local_pending = sum(
            1
            for row in merged["rows"]
            if row["Type"] in HOST_DEVICE_TYPES
            and (
                str(row.get("Source") or "").upper().startswith("ARKUSZ — OCZEKUJE")
                or str(row.get("Source") or "").upper().startswith("EDYCJA LOKALNA")
            )
        )
        self.log(
            f"Nowe={merged['added']}, zaktualizowane={merged['updated']}, "
            f"lokalne oczekujące={local_pending}."
        )
        if not startup:
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Odświeżono z Zabbixa",
                    f"Nowe: {merged['added']}\nZaktualizowane: {merged['updated']}\n"
                    f"Lokalne zmiany oczekujące na wysłanie: {local_pending}\n\n"
                    "Pozycje mapy zostały zachowane. Szafy pozostają lokalne.",
                ),
            )

    def add_device_manually(self) -> None:
        dialog = AddDeviceDialog(self, self.selected_type.get(), self.config_data)
        if dialog.result is None:
            return
        result = dialog.result

        def task():
            is_cabinet = result["type"] == "cabinet"
            if result["create_host"] and not is_cabinet:
                self.ensure_host_in_zabbix(result)
            type_config = self.config_data["types"][result["type"]]
            host_name = result["host_name"]
            if is_cabinet:
                host_name = cabinet_local_id(
                    read_devices(DEVICES_PATH), result["visible_name"]
                )
            row = default_row(
                result["type"],
                result["ip"],
                host_name,
                result["visible_name"],
                source=(
                    "SZAFA LOKALNA"
                    if is_cabinet
                    else "EDYCJA LOKALNA — oczekuje Zabbixa"
                    if not result["create_host"]
                    else "ZABBIX"
                ),
                origin="CABINET" if is_cabinet else "MANUAL",
            )
            row["GroupName"] = type_config["group"]
            row["TemplateName"] = type_config["template"]
            row["TagType"] = type_config["tag"]
            row["IconName"] = type_config.get("icon", "")
            backup = backup_file(DEVICES_PATH, "devices")
            merged = upsert_devices(read_devices(DEVICES_PATH), [row])
            write_devices(DEVICES_PATH, merged["rows"])
            if backup:
                self.log(f"Backup devices.csv: {backup}")
            if is_cabinet:
                self.log(
                    f"Dodano szafę: {row['HostName']} / {row['VisibleName']} "
                    "(bez hosta Zabbixa)"
                )
            else:
                self.log(f"Dodano: {row['HostName']} ({row['IP']})")
            self.after(0, lambda: self.selected_type.set(result["type"]))
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Dodano szafę" if is_cabinet else "Dodano urządzenie",
                    (
                        f"{row['VisibleName']}\n\n"
                        "Szafa ma status PENDING. Teraz oznacz jej lokalizację.\n"
                        "Nie utworzono hosta, IP ani pingu w Zabbixie."
                        if is_cabinet
                        else f"{row['VisibleName']}\n{row['IP']}\n\n"
                        "Urządzenie ma status PENDING. Teraz oznacz jego lokalizację."
                    ),
                ),
            )

        self.start_background(task)

    def selected_manual_device_row(self) -> dict | None:
        """Zwraca dokładnie jeden ręcznie dodany wpis zaznaczony w tabeli."""
        selected = self.tree.selection()
        if len(selected) != 1:
            messagebox.showwarning(
                "Wybierz urządzenie",
                "Najpierw zaznacz w tabeli jedno ręcznie dodane urządzenie.",
            )
            return None
        row = getattr(self, "tree_rows_by_iid", {}).get(selected[0])
        if row is None:
            messagebox.showwarning(
                "Brak zaznaczenia",
                "Odśwież widok i zaznacz urządzenie ponownie.",
            )
            return None
        if (
            str(row.get("Origin") or "").strip().upper() != "MANUAL"
            and str(row.get("Source") or "").strip().upper() != "MANUAL"
        ):
            messagebox.showwarning(
                "Tylko wpis ręczny",
                "Można usuwać tylko urządzenia dodane przyciskiem\n"
                "„Dodaj urządzenie ręcznie”.\n\n"
                "Urządzenia odczytane z arkusza lub Zabbixa pozostają zabezpieczone.",
            )
            return None
        return dict(row)

    def selected_device_row(self) -> dict | None:
        """Zwraca dokładnie jeden zaznaczony wpis, niezależnie od źródła."""
        selected = self.tree.selection()
        if len(selected) != 1:
            messagebox.showwarning(
                "Wybierz urządzenie",
                "Najpierw zaznacz w tabeli dokładnie jedno urządzenie.",
            )
            return None
        row = getattr(self, "tree_rows_by_iid", {}).get(selected[0])
        if row is None:
            messagebox.showwarning(
                "Brak zaznaczenia",
                "Odśwież widok i zaznacz urządzenie ponownie.",
            )
            return None
        return dict(row)

    @staticmethod
    def same_device_identity(left: dict, right: dict) -> bool:
        return (
            left.get("Type") == right.get("Type")
            and str(left.get("HostName") or "").strip()
            == str(right.get("HostName") or "").strip()
        )

    def edit_selected_device_name(self) -> None:
        """Zapisuje wyłącznie widoczną nazwę; host/IP/mapa zostają bez zmian."""
        row = self.selected_device_row()
        if row is None:
            return
        old_name = str(row.get("VisibleName") or "").strip()
        new_name = simpledialog.askstring(
            "Edytuj widoczną nazwę",
            "Nowa widoczna nazwa:",
            initialvalue=old_name,
            parent=self,
        )
        if new_name is None:
            return
        new_name = " ".join(new_name.split())
        if not new_name:
            messagebox.showerror("Brak nazwy", "Nazwa nie może być pusta.", parent=self)
            return
        if new_name == old_name:
            return

        is_cabinet = row["Type"] == "cabinet"
        destination = "lokalnie (szafa nie ma hosta w Zabbixie)" if is_cabinet else "lokalnie; potem wyślij ją krokiem 5 do Zabbixa"
        if not messagebox.askyesno(
            "Potwierdź zmianę nazwy",
            f"Zmienić widoczną nazwę?\n\n"
            f"Stara: {old_name}\nNowa: {new_name}\n\n"
            f"IP: {row.get('IP') or '—'}\n"
            f"Techniczny Host name: {row.get('HostName') or '—'}\n\n"
            f"Zmiana zostanie zapisana {destination}.\n"
            "IP, Host name, status i pozycja na mapie nie zostaną zmienione.",
            parent=self,
        ):
            return

        rows = read_devices(DEVICES_PATH)
        matching_rows = [item for item in rows if self.same_device_identity(item, row)]
        if len(matching_rows) != 1:
            messagebox.showerror(
                "Nie znaleziono wpisu",
                "Nie udało się jednoznacznie odnaleźć urządzenia w devices.csv. "
                "Odśwież widok i spróbuj ponownie.",
                parent=self,
            )
            return

        backup = backup_file(DEVICES_PATH, "devices")
        for item in rows:
            if self.same_device_identity(item, row):
                item["VisibleName"] = new_name
                item["UpdatedAt"] = datetime.now().isoformat(timespec="seconds")
                if not is_cabinet:
                    item["Source"] = "EDYCJA LOKALNA — oczekuje Zabbixa"
        write_devices(DEVICES_PATH, rows)
        if backup:
            self.log(f"Backup devices.csv przed ręczną zmianą nazwy: {backup}")
        self.log(
            f"[EDYCJA NAZWY] {row['Type']} {row.get('HostName')}: "
            f"{old_name!r} -> {new_name!r}"
        )
        self.refresh_view()
        if is_cabinet:
            messagebox.showinfo(
                "Nazwa szafy zapisana",
                "Zmieniono lokalną nazwę szafy. Szafa nie ma hosta w Zabbixie.",
                parent=self,
            )
        else:
            messagebox.showinfo(
                "Nazwa zapisana lokalnie",
                "Zmiana jest oznaczona jako „EDYCJA LOKALNA — oczekuje Zabbixa”.\n\n"
                "Aby wysłać ją do Zabbixa, kliknij:\n"
                "4. Sprawdź nazwy wybranego typu\n"
                "5. Zaktualizuj nazwy wybranego typu",
                parent=self,
            )

    def deletion_target_maps(self, row: dict) -> list[str] | None:
        """Ustala mapy, z których ma zniknąć ręcznie dodany punkt.

        Szafy pamiętają dokładne mapy w lokalnym rejestrze. Dla hostów nie
        przechowujemy takiej metadanej, więc operator świadomie wybiera mapę.
        """
        if row["Type"] == "cabinet":
            registry = parse_map_element_ids(row.get("MapElementIds"))
            remembered_maps = sorted(
                {
                    str(map_name).strip()
                    for map_name, element_id in registry.items()
                    if str(map_name).strip() and str(element_id).strip()
                },
                key=str.casefold,
            )
            if remembered_maps:
                return remembered_maps

        selected_map = self.choose_target_map(row["Type"])
        return [selected_map] if selected_map else None

    def confirm_delete_selected_manual_device(self) -> None:
        row = self.selected_manual_device_row()
        if row is None:
            return

        target_maps = self.deletion_target_maps(row)
        if not target_maps:
            return

        label = DEVICE_TYPE_LABELS[row["Type"]]
        name = str(row.get("VisibleName") or row.get("HostName") or "(bez nazwy)")
        maps_text = "\n".join(f"• {map_name}" for map_name in target_maps)
        if row["Type"] == "cabinet":
            scope = (
                "Zostanie usunięty wpis lokalny oraz ikona szafy z mapy/map:\n"
                f"{maps_text}\n\n"
                "Szafa nie ma hosta, IP ani pingu w Zabbixie."
            )
        else:
            scope = (
                "Zostanie usunięty wpis lokalny oraz punkt z mapy:\n"
                f"{maps_text}\n\n"
                "Host Zabbixa pozostanie bez zmian. Narzędzie nie usuwa go automatycznie, "
                "bo nie może bezpiecznie ustalić, czy nie istniał przed dodaniem wpisu."
            )

        if not messagebox.askyesno(
            "Usuń ręcznie dodane urządzenie",
            f"Usunąć urządzenie typu „{label}”:\n{name}?\n\n"
            f"{scope}\n\n"
            "Operacja utworzy backup przed usunięciem.",
        ):
            return

        self.start_background(
            lambda selected_row=row, selected_maps=target_maps: self.delete_manual_device(
                selected_row, selected_maps
            )
        )

    @staticmethod
    def same_device_row(left: dict, right: dict) -> bool:
        return (
            left.get("Type") == right.get("Type")
            and str(left.get("HostName") or "").strip()
            == str(right.get("HostName") or "").strip()
            and (
                str(left.get("Origin") or "").strip().upper() == "MANUAL"
                or str(left.get("Source") or "").strip().upper() == "MANUAL"
            )
        )

    def create_deletion_staging_csv(self, row: dict, rows: list[dict]) -> Path:
        """Tworzy jednorazowy plan, który usuwa punkt z mapy bez kasowania go
        z prawdziwego devices.csv, dopóki Zabbix nie potwierdzi zapisu."""
        staged_rows: list[dict] = []
        found = 0
        for item in rows:
            staged = dict(item)
            if self.same_device_row(staged, row):
                staged["MapStatus"] = "SKIPPED"
                staged["UpdatedAt"] = datetime.now().isoformat(timespec="seconds")
                found += 1
            staged_rows.append(staged)
        if found != 1:
            raise RuntimeError(
                "Nie znaleziono jednoznacznie ręcznie dodanego urządzenia w devices.csv. "
                "Odśwież widok i spróbuj ponownie."
            )
        staging_dir = BACKUP_DIR / "deletion_staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / f"devices_before_delete_{timestamp()}.csv"
        write_devices(staging_path, staged_rows)
        return staging_path

    def delete_manual_device(self, row: dict, target_maps: list[str]) -> None:
        """Najpierw usuwa punkt z mapy, a dopiero potem wpis lokalny.

        Jeżeli synchronizacja którejkolwiek mapy się nie powiedzie, devices.csv
        pozostaje nietknięty. Dzięki temu można bezpiecznie powtórzyć operację.
        """
        rows = read_devices(DEVICES_PATH)
        staging_path = self.create_deletion_staging_csv(row, rows)
        self.log(f"Plan usunięcia zapisano: {staging_path}")

        for map_name in target_maps:
            self.map_sync(
                row["Type"],
                map_name,
                apply=False,
                notify=False,
                csv_path=staging_path,
                persist_cabinet_ids=False,
            )
            self.map_sync(
                row["Type"],
                map_name,
                apply=True,
                notify=False,
                csv_path=staging_path,
                persist_cabinet_ids=False,
            )

        remaining_rows = [
            item for item in read_devices(DEVICES_PATH) if not self.same_device_row(item, row)
        ]
        if len(remaining_rows) != len(rows) - 1:
            raise RuntimeError(
                "Po synchronizacji mapy nie udało się bezpiecznie potwierdzić wpisu "
                "do usunięcia. devices.csv nie został zmieniony."
            )
        backup = backup_file(DEVICES_PATH, "manual_deletions")
        write_devices(DEVICES_PATH, remaining_rows)
        report_dir = BACKUP_DIR / "manual_deletions"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"deleted_{row['Type']}_{timestamp()}.txt"
        report_path.write_text(
            "USUNIĘCIE RĘCZNIE DODANEGO URZĄDZENIA\n\n"
            f"Data: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Typ: {row['Type']}\n"
            f"Nazwa: {row.get('VisibleName', '')}\n"
            f"Host name / identyfikator lokalny: {row.get('HostName', '')}\n"
            f"IP: {row.get('IP', '')}\n"
            f"Usunięto z map: {', '.join(target_maps)}\n"
            "Host Zabbixa: pozostawiony bez zmian\n",
            encoding="utf-8",
        )
        if backup:
            self.log(f"Backup devices.csv przed usunięciem: {backup}")
        self.log(
            f"Usunięto ręcznie dodane urządzenie: {row['VisibleName']} / "
            f"mapy: {', '.join(target_maps)}"
        )
        self.log(f"Raport usunięcia: {report_path}")
        self.after(
            0,
            lambda: messagebox.showinfo(
                "Urządzenie usunięte",
                f"Usunięto: {row['VisibleName']}\n"
                f"Mapy: {', '.join(target_maps)}\n\n"
                "Punkt zniknął z wybranych map, a wpis lokalny został usunięty.\n"
                "Host Zabbixa nie został zmieniony.",
            ),
        )

    def ensure_host_in_zabbix(self, data: dict) -> None:
        if data["type"] not in HOST_DEVICE_TYPES:
            raise RuntimeError("Szafa nie może zostać utworzona jako host w Zabbixie.")
        api = self.api()
        existing = api.call(
            "host.get",
            {
                "output": ["hostid", "host", "name"],
                "filter": {"host": [data["host_name"]]},
            },
        ) or []
        if existing:
            self.log(f"Host już istnieje: {data['host_name']}")
            return
        type_config = self.config_data["types"][data["type"]]
        group_id = self.find_group_id(api, type_config["group"])
        template_id = self.find_template_id(api, type_config["template"])
        result = api.call(
            "host.create",
            {
                "host": data["host_name"],
                "name": data["visible_name"],
                "interfaces": [
                    {
                        "type": 1,
                        "main": 1,
                        "useip": 1,
                        "ip": data["ip"],
                        "dns": "",
                        "port": "10050",
                    }
                ],
                "groups": [{"groupid": group_id}],
                "templates": [{"templateid": template_id}],
                "tags": [{"tag": "type", "value": type_config["tag"]}],
            },
        )
        self.log(
            f"Utworzono host: {data['host_name']} / hostids={result.get('hostids')}"
        )

    def ensure_zabbix_icons(self) -> None:
        self.log("=== KONTROLA IKON ZABBIxa ===")
        api = self.api()
        for device_type in DEVICE_TYPES:
            icon_name = self.config_data["types"][device_type]["icon"]
            icon_path = ICON_DIR / f"{icon_name}.png"
            images = api.call(
                "image.get",
                {
                    "output": ["imageid", "name", "imagetype"],
                    "filter": {"name": [icon_name]},
                },
            ) or []
            if images:
                self.log(f"[OK] {icon_name}: imageid={images[0]['imageid']}")
                continue
            if not icon_path.is_file():
                raise RuntimeError(f"Brakuje lokalnego pliku ikony: {icon_path}")
            encoded = base64.b64encode(icon_path.read_bytes()).decode("ascii")
            result = api.call(
                "image.create",
                {"name": icon_name, "imagetype": 1, "image": encoded},
            )
            self.log(f"Wgrano ikonę {icon_name}: {result.get('imageids')}")
        self.after(
            0,
            lambda: messagebox.showinfo(
                "Ikony gotowe",
                "Ikony CAM, RECORDER, SWITCH i CABINET są dostępne w Zabbixie.",
            ),
        )

    def ensure_zabbix_icon_if_needed(self, device_type: str) -> None:
        icon_name = self.config_data["types"][device_type]["icon"]
        api = self.api()
        images = api.call(
            "image.get",
            {"output": ["imageid", "name"], "filter": {"name": [icon_name]}},
        ) or []
        if images:
            return
        icon_path = ICON_DIR / f"{icon_name}.png"
        if not icon_path.is_file():
            raise RuntimeError(f"Brakuje ikony: {icon_name}")
        encoded = base64.b64encode(icon_path.read_bytes()).decode("ascii")
        api.call(
            "image.create",
            {"name": icon_name, "imagetype": 1, "image": encoded},
        )
        self.log(f"Automatycznie wgrano ikonę: {icon_name}")

    def launch_clicker(self) -> None:
        device_type = self.selected_type.get()
        rows = [row for row in read_devices(DEVICES_PATH) if row["Type"] == device_type]
        if not rows:
            messagebox.showwarning(
                "Brak urządzeń", "Najpierw dodaj lub odśwież urządzenia tego typu."
            )
            return
        image_path = self.map_image_path_for_type(device_type)
        if not image_path.is_file():
            messagebox.showerror("Brak mapy", f"Nie znaleziono:\n{image_path}")
            return
        reset = messagebox.askyesnocancel(
            "Urządzenia pominięte",
            "Czy wcześniejsze SKIPPED przywrócić do przeklikania?\n\n"
            "TAK — staną się PENDING.\nNIE — pozostaną pominięte.",
        )
        if reset is None:
            return
        backup = backup_file(DEVICES_PATH, "devices")
        if backup:
            self.log(f"Backup przed edycją: {backup}")
        command = [
            sys.executable,
            str(CLICKER_PATH),
            "--devices",
            str(DEVICES_PATH),
            "--map",
            str(image_path),
            "--type",
            device_type,
        ]
        if reset:
            command.append("--reset-skipped")
        subprocess.Popen(command, cwd=str(APP_DIR))
        messagebox.showinfo(
            "Edytor uruchomiony",
            f"Wyświetlany typ: {DEVICE_TYPE_LABELS[device_type]}\n"
            + (
                "Szafa pozostaje obiektem graficznym bez hosta w Zabbixie.\n\n"
                if device_type == "cabinet"
                else "Pozostałe typy nie są widoczne w edytorze, ale pozostają w "
                "devices.csv i na mapie Zabbixa.\n\n"
            )
            + f"Zapis automatyczny:\n{DEVICES_PATH}",
        )

    @staticmethod
    def decode_output(raw: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "cp852", "cp1250"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def cabinet_ids_from_sync_output(process_output: str) -> dict[str, str] | None:
        """Odbiera mapowanie selementid zwrócone po zapisie mapy.

        Zamiast technicznego URL-a na ikonie, Zabbixowy identyfikator elementu
        jest od tej wersji przechowywany lokalnie w devices.csv.
        """
        matches = re.findall(
            r"(?m)^CCTV_CABINET_ELEMENT_IDS_JSON=(\{.*\})\s*$",
            process_output,
        )
        if not matches:
            return None
        try:
            raw_ids = json.loads(matches[-1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw_ids, dict):
            return None
        return {
            str(local_id).strip(): str(element_id).strip()
            for local_id, element_id in raw_ids.items()
            if str(local_id).strip() and str(element_id).strip()
        }

    def save_cabinet_element_ids(self, map_name: str, element_ids: dict[str, str]) -> None:
        """Zapisuje lokalne powiązanie szaf z elementami wybranej mapy."""
        rows = read_devices(DEVICES_PATH)
        changed = 0
        for row in rows:
            if row["Type"] != "cabinet":
                continue
            registry = parse_map_element_ids(row.get("MapElementIds"))
            local_id = str(row.get("HostName") or "").strip()
            new_element_id = element_ids.get(local_id, "")
            if new_element_id:
                if registry.get(map_name) != new_element_id:
                    registry[map_name] = new_element_id
                    changed += 1
            elif map_name in registry:
                # Szafa ma SKIPPED/PENDING albo została usunięta z tej mapy.
                registry.pop(map_name, None)
                changed += 1
            row["MapElementIds"] = serialize_map_element_ids(registry)

        if not changed:
            return
        backup = backup_file(DEVICES_PATH, "cabinet_map_ids")
        write_devices(DEVICES_PATH, rows)
        if backup:
            self.log(f"Backup powiązań szaf: {backup}")
        self.log(
            "Zapisano lokalne powiązania ikon szaf z mapą "
            f"„{map_name}”: {len(element_ids)}."
        )

    def map_sync(
        self,
        device_type: str,
        map_name: str,
        *,
        apply: bool,
        notify: bool = True,
        csv_path: Path | None = None,
        persist_cabinet_ids: bool = True,
    ) -> None:
        source_csv = csv_path or DEVICES_PATH
        summary = summarize_devices(read_devices(source_csv))[device_type]
        if summary["total"] == 0:
            raise RuntimeError(f"Brak urządzeń typu {DEVICE_TYPE_LABELS[device_type]}.")
        if summary["PENDING"] > 0:
            self.log(
                f"UWAGA: {summary['PENDING']} urządzeń typu "
                f"{DEVICE_TYPE_LABELS[device_type]} ma status PENDING. "
                "Nie będą widoczne na mapie."
            )
        if summary["PLACED"] > 0:
            self.ensure_zabbix_icon_if_needed(device_type)
        type_config = self.config_data["types"][device_type]
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(MAP_SYNC_PATH),
            "-ZabbixUrl",
            self.config_data["zabbix_url"],
            "-User",
            self.config_data["zabbix_user"],
            "-Password",
            self.config_data["zabbix_password"],
            "-MapName",
            map_name,
            "-CsvPath",
            str(source_csv),
            "-DeviceType",
            device_type,
            "-IconName",
            type_config.get("icon", ""),
            "-BackupDir",
            str(BACKUP_DIR / "maps"),
        ]
        if apply:
            command.append("-Apply")
        self.log(
            f"=== MAPA: {DEVICE_TYPE_LABELS[device_type]} → {map_name} / "
            f"{'APPLY' if apply else 'DRY RUN'} ==="
        )
        process = subprocess.run(
            command,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
        )
        process_output = self.decode_output(process.stdout or b"")
        # Nie zaśmiecamy dziennika technicznym JSON-em, który służy wyłącznie
        # do zapisu lokalnej metadanej narzędzia.
        display_output = re.sub(
            r"(?m)^CCTV_CABINET_ELEMENT_IDS_JSON=\{.*\}\s*$",
            "Zapisano lokalne identyfikatory ikon szaf.",
            process_output,
        )
        self.log(display_output)

        if process.returncode != 0:
            output_lines = [
                line
                for line in process_output.strip().splitlines()
                if line.strip()
            ]
            output_tail = "\n".join(output_lines[-10:])

            message = (
                "Synchronizacja mapy zakończyła się błędem "
                f"(kod {process.returncode})."
            )

            if output_tail:
                message += f"\n\nKońcówka komunikatu:\n{output_tail}"

            raise RuntimeError(message)
        if apply and device_type == "cabinet" and persist_cabinet_ids:
            element_ids = self.cabinet_ids_from_sync_output(process_output)
            if element_ids is None:
                self.log(
                    "UWAGA: Mapa została zapisana, ale narzędzie nie otrzymało "
                    "lokalnych identyfikatorów szaf. Przy kolejnym imporcie "
                    "spróbuje je odtworzyć po nazwie i pozycji."
                )
            else:
                self.save_cabinet_element_ids(map_name, element_ids)
        if notify and not apply:
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Dry run zakończony",
                    f"Plan dla typu {DEVICE_TYPE_LABELS[device_type]} na mapie\n"
                    f"„{map_name}” jest poprawny.\n"
                    "Mapa nie została zmieniona.",
                ),
            )
        elif notify:
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Mapa zaktualizowana",
                    f"Zsynchronizowano: {DEVICE_TYPE_LABELS[device_type]}.\n"
                    f"Mapa: {map_name}\n\n"
                    "Na mapie pozostawiono tylko urządzenia PLACED tego typu.\n"
                    "SKIPPED i PENDING zostały usunięte z mapy.\n"
                    "Inne typy i pozostałe elementy zostały zachowane.",
                ),
            )

    def confirm_selected_map_sync(self) -> None:
        device_type = self.selected_type.get()
        map_name = self.choose_target_map(device_type)
        if not map_name:
            return
        summary = summarize_devices(
            read_devices(DEVICES_PATH)
        )[device_type]

        pending_note = ""
        if summary["PENDING"] > 0:
            pending_note = (
                f"\n\nUWAGA: {summary['PENDING']} urządzeń ma status PENDING. "
                "Jeśli były wcześniej na mapie, zostaną z niej usunięte."
            )

        if not messagebox.askyesno(
            "Synchronizacja mapy",
            (
                f"Zsynchronizować typ: "
                f"{DEVICE_TYPE_LABELS[device_type]}?\n"
                f"Docelowa mapa: {map_name}\n\n"
                f"PLACED: {summary['PLACED']}\n"
                f"SKIPPED: {summary['SKIPPED']}\n"
                f"PENDING: {summary['PENDING']}\n\n"
                "Program wykona dry run, a potem zapis. "
                "Na mapie pozostaną wyłącznie urządzenia PLACED tego typu. "
                "Inne typy pozostaną na mapie."
                f"{pending_note}"
            ),
        ):
            return

        def task():
            self.map_sync(device_type, map_name, apply=False, notify=False)
            self.map_sync(device_type, map_name, apply=True, notify=True)

        self.start_background(task)

    def names_plan(self, device_types: list[str]) -> dict:
        rows = [
            row for row in read_devices(DEVICES_PATH) if row["Type"] in device_types
        ]
        if not rows:
            raise RuntimeError("Brak urządzeń wybranych typów.")
        api = self.api()
        host_names = sorted({row["HostName"] for row in rows})
        hosts = api.call(
            "host.get",
            {
                "output": ["hostid", "host", "name", "status"],
                "filter": {"host": host_names},
            },
        ) or []
        by_host = {str(host["host"]): host for host in hosts}
        missing = [host for host in host_names if host not in by_host]
        plan = []
        for row in rows:
            if row["HostName"] not in by_host:
                continue
            current = str(by_host[row["HostName"]]["name"]).strip()
            desired = row["VisibleName"].strip()
            plan.append(
                {
                    "hostid": str(by_host[row["HostName"]]["hostid"]),
                    "host": row["HostName"],
                    "type": row["Type"],
                    "current": current,
                    "desired": desired,
                    "action": "UPDATE" if current != desired else "UNCHANGED",
                }
            )
        return {"api": api, "missing": missing, "plan": plan}

    def names_sync(self, device_types: list[str], *, apply: bool, notify: bool = True) -> None:
        self.log(
            "=== NAZWY: "
            + ", ".join(DEVICE_TYPE_LABELS[value] for value in device_types)
            + f" / {'APPLY' if apply else 'DRY RUN'} ==="
        )
        result = self.names_plan(device_types)
        updates = [item for item in result["plan"] if item["action"] == "UPDATE"]
        unchanged = [
            item for item in result["plan"] if item["action"] == "UNCHANGED"
        ]
        backup_dir = BACKUP_DIR / "names"
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / f"names_plan_{timestamp()}.json").write_text(
            json.dumps(
                {"missing": result["missing"], "plan": result["plan"]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.log(f"Do zmiany: {len(updates)}")
        self.log(f"Już zgodne: {len(unchanged)}")
        self.log(f"Brakujące hosty: {len(result['missing'])}")
        for item in updates:
            self.log(
                f"[ZMIANA] {item['host']}: '{item['current']}' -> '{item['desired']}'"
            )
        if result["missing"]:
            raise RuntimeError("Brak hostów w Zabbixie: " + ", ".join(result["missing"]))
        if not apply:
            if notify:
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Sprawdzenie nazw",
                        f"Do zmiany: {len(updates)}\nJuż zgodne: {len(unchanged)}\n"
                        "Nic nie zostało zmienione.",
                    ),
                )
            return
        for item in updates:
            result["api"].call(
                "host.update",
                {"hostid": item["hostid"], "name": item["desired"]},
            )
        verify = self.names_plan(device_types)
        remaining = [item for item in verify["plan"] if item["action"] == "UPDATE"]
        if verify["missing"] or remaining:
            raise RuntimeError("Weryfikacja nazw nie powiodła się.")
        # Po weryfikacji Zabbix jest znów źródłem prawdy dla tych hostów.
        verified_hosts = {item["host"] for item in verify["plan"]}
        rows = read_devices(DEVICES_PATH)
        changed_source = 0
        for row in rows:
            if row["Type"] in device_types and row["HostName"] in verified_hosts:
                if str(row.get("Source") or "").upper() != "ZABBIX":
                    row["Source"] = "ZABBIX"
                    changed_source += 1
        if changed_source:
            backup = backup_file(DEVICES_PATH, "devices")
            write_devices(DEVICES_PATH, rows)
            if backup:
                self.log(f"Backup devices.csv przed potwierdzeniem źródła: {backup}")
            self.log(f"Źródło potwierdzone przez Zabbixa: {changed_source} wpisów.")
        self.log("Weryfikacja nazw: OK.")
        if notify:
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Nazwy zaktualizowane", f"Zmieniono nazw: {len(updates)}."
                ),
            )

    def confirm_selected_names_sync(self) -> None:
        device_type = self.selected_type.get()
        if device_type not in HOST_DEVICE_TYPES:
            messagebox.showinfo(
                "Szafy bez hostów",
                "Szafy nie mają hostów ani nazw do aktualizacji w Zabbixie.",
            )
            return
        if not messagebox.askyesno(
            "Aktualizacja nazw",
            f"Zaktualizować nazwy: {DEVICE_TYPE_LABELS[device_type]}?\n\n"
            "IP, techniczne Host name, grupy, tagi i pozycje mapy pozostaną bez zmian.",
        ):
            return
        self.start_background(lambda: self.names_sync([device_type], apply=True))

    def confirm_full_sync(self) -> None:
        summary = summarize_devices(read_devices(DEVICES_PATH))
        pending_total = sum(summary[device_type]["PENDING"] for device_type in DEVICE_TYPES)
        pending_note = ""
        if pending_total > 0:
            details = ", ".join(
                f"{DEVICE_TYPE_LABELS[device_type]}={summary[device_type]['PENDING']}"
                for device_type in DEVICE_TYPES
                if summary[device_type]["PENDING"] > 0
            )
            pending_note = (
                f"\n\nPENDING: {pending_total} ({details}). "
                "Te urządzenia nie będą widoczne na mapie."
            )
        if not messagebox.askyesno(
            "Pełna synchronizacja",
            "Program zaktualizuje nazwy, a następnie kolejno zsynchronizuje kamery, "
            "rejestratory, switche i szafy. Dla każdego typu na mapie pozostaną tylko "
            "urządzenia PLACED; SKIPPED i PENDING zostaną usunięte.\n\nKontynuować?"
            f"{pending_note}",
        ):
            return

        def task():
            active_types = [
                device_type
                for device_type in DEVICE_TYPES
                if summary[device_type]["total"] > 0
            ]
            host_types = [
                device_type for device_type in active_types if device_type in HOST_DEVICE_TYPES
            ]
            if host_types:
                self.names_sync(host_types, apply=True, notify=False)
            for device_type in active_types:
                map_name = self.preferred_map_name(device_type)
                self.map_sync(device_type, map_name, apply=False, notify=False)
                self.map_sync(device_type, map_name, apply=True, notify=False)
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Pełna synchronizacja",
                    "Nazwy i mapa wszystkich dostępnych typów zostały zsynchronizowane.",
                ),
            )

        self.start_background(task)

    def edit_settings(self) -> None:
        url = simpledialog.askstring(
            "Zabbix API",
            "Adres API:",
            initialvalue=self.config_data["zabbix_url"],
            parent=self,
        )
        if url is None:
            return
        user = simpledialog.askstring(
            "Login",
            "Login Zabbixa:",
            initialvalue=self.config_data["zabbix_user"],
            parent=self,
        )
        if user is None:
            return
        password = simpledialog.askstring(
            "Hasło",
            "Hasło Zabbixa:",
            initialvalue=self.config_data["zabbix_password"],
            show="*",
            parent=self,
        )
        if password is None:
            return
        map_name = simpledialog.askstring(
            "Mapa",
            "Domyślna mapa kamer:",
            initialvalue=self.preferred_map_name("camera"),
            parent=self,
        )
        if map_name is None:
            return
        self.config_data.update(
            {
                "zabbix_url": url.strip(),
                "zabbix_user": user.strip(),
                "zabbix_password": password,
                "zabbix_map_name": map_name.strip(),
            }
        )
        self.config_data.setdefault("map_targets", {})["camera"] = map_name.strip()
        save_config(self.config_data)
        messagebox.showinfo("Ustawienia", "Zapisano ustawienia.")

    @staticmethod
    def open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])


def self_test() -> int:
    migrate_camera_points(OLD_CAMERA_POINTS_PATH, DEVICES_PATH)
    rows = read_devices(DEVICES_PATH)
    summary = summarize_devices(rows)
    print(f"ROOT={ROOT_DIR}")
    print(f"DEVICES={DEVICES_PATH}")
    print(f"TOTAL={len(rows)}")
    for device_type in DEVICE_TYPES:
        data = summary[device_type]
        print(
            f"{device_type.upper()}=TOTAL={data['total']},PLACED={data['PLACED']},"
            f"SKIPPED={data['SKIPPED']},PENDING={data['PENDING']},"
            f"DUPLICATES={data['duplicates']}"
        )
    if ODS_PATH.is_file():
        cameras = read_cameras_from_spreadsheet(ODS_PATH)
        print(f"SPREADSHEET_ODS_CAMERAS={len(cameras)}")
    if any(summary[item]["duplicates"] for item in DEVICE_TYPES):
        raise RuntimeError("W devices.csv są duplikaty.")
    print("SELF_TEST=OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    app = DeviceManager()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
