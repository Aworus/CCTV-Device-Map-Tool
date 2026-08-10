from __future__ import annotations

import argparse
import ctypes
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

try:
    from PIL import Image, ImageTk
except ImportError:
    raise SystemExit("Brak biblioteki Pillow. Uruchom: py -m pip install pillow")

from device_data import (
    DEVICE_TYPE_LABELS,
    normalize_device_type,
    normalize_status,
    parse_optional_float,
    read_devices,
    short_device_label,
    sort_devices,
    write_devices,
)

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

TYPE_COLORS = {
    "camera": "#d52b1e",
    "recorder": "#1f5fbf",
    "switch": "#17803d",
    "cabinet": "#4a4a4a",
}


def numeric_ip_key(row: dict) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(part) for part in row["IP"].split("."))
        if len(parts) == 4:
            return parts
    except (TypeError, ValueError):
        pass
    return (999, 999, 999, 999)


def find_device_index(devices: list[dict], query: str) -> int | None:
    """Zwraca pierwszy wpis pasujący do IP, nazwy widocznej lub Host name."""
    needle = str(query or "").strip().casefold()
    if not needle:
        return None

    searchable_fields = ("IP", "VisibleName", "HostName")
    # Dokładne trafienie jest najwygodniejsze dla pełnego IP i nazwy.
    for index, row in enumerate(devices):
        values = [str(row.get(field) or "").casefold() for field in searchable_fields]
        if needle in values:
            return index

    # Następnie pozwalamy wpisać tylko fragment, np. "222.44" albo "ogrodzenie".
    for index, row in enumerate(devices):
        searchable = " ".join(
            str(row.get(field) or "") for field in searchable_fields
        ).casefold()
        if needle in searchable:
            return index
    return None


class DeviceMapClicker(tk.Tk):
    def __init__(
        self,
        devices_path: Path,
        image_path: Path,
        device_type: str,
        *,
        reset_skipped: bool,
    ):
        super().__init__()
        self.devices_path = devices_path
        self.image_path = image_path
        self.device_type = normalize_device_type(device_type)
        self.type_label = DEVICE_TYPE_LABELS[self.device_type]
        self.color = TYPE_COLORS[self.device_type]

        self.title(f"CCTV — lokalizacje: {self.type_label}")
        self.geometry("1720x960")
        self.minsize(1180, 700)

        self.base_image = Image.open(image_path).convert("RGB")
        self.image_width, self.image_height = self.base_image.size
        self.all_devices = read_devices(devices_path)
        self.devices = [
            row for row in self.all_devices if row["Type"] == self.device_type
        ]
        self.devices.sort(key=lambda row: (numeric_ip_key(row), row["HostName"].casefold()))

        if reset_skipped:
            for row in self.devices:
                if normalize_status(row["MapStatus"]) == "SKIPPED":
                    row["MapStatus"] = "PENDING"
                    row["X"] = ""
                    row["Y"] = ""
                    row["XPercent"] = ""
                    row["YPercent"] = ""

        self.current_index = self.find_first_pending_index()
        self.zoom = 1.0
        self.tk_image = None
        self.auto_next = tk.BooleanVar(value=True)
        self.show_numbers = tk.BooleanVar(value=True)
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.current_var = tk.StringVar()
        self.detail_var = tk.StringVar()
        self.path_var = tk.StringVar()
        self.message_var = tk.StringVar(
            value=(
                "Lewy klik ustawia urządzenie. Prawy przycisk przeciąga mapę. "
                "Kółko myszy przybliża."
            )
        )

        self.build_ui()
        self.set_initial_zoom()
        self.redraw()
        self.select_current_in_list()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-s>", lambda _event: self.save(show_message=True))
        self.bind("<Left>", lambda _event: self.previous_device())
        self.bind("<Right>", lambda _event: self.next_device())
        self.bind("<space>", lambda _event: self.skip_device())
        self.bind("<Delete>", lambda _event: self.clear_current())
        self.bind("<Control-f>", self.focus_search)

    def build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=6)
        toolbar.pack(fill="x")

        ttk.Button(toolbar, text="← Poprzedni", command=self.previous_device).pack(
            side="left", padx=3
        )
        ttk.Button(toolbar, text="Następny →", command=self.next_device).pack(
            side="left", padx=3
        )
        ttk.Button(toolbar, text="Pomiń [Spacja]", command=self.skip_device).pack(
            side="left", padx=3
        )
        ttk.Button(
            toolbar, text="Usuń punkt [Delete]", command=self.clear_current
        ).pack(side="left", padx=3)
        ttk.Button(
            toolbar,
            text="Zapisz [Ctrl+S]",
            command=lambda: self.save(show_message=True),
        ).pack(side="left", padx=8)
        ttk.Checkbutton(
            toolbar,
            text="Po kliknięciu od razu następne",
            variable=self.auto_next,
        ).pack(side="left", padx=10)
        ttk.Checkbutton(
            toolbar,
            text="Pokaż numery / skróty",
            variable=self.show_numbers,
            command=self.redraw,
        ).pack(side="left", padx=10)
        ttk.Button(toolbar, text="Dopasuj", command=self.fit_image).pack(
            side="right", padx=3
        )
        ttk.Button(
            toolbar, text="Zoom +", command=lambda: self.change_zoom(1.15)
        ).pack(side="right", padx=3)
        ttk.Button(
            toolbar, text="Zoom −", command=lambda: self.change_zoom(1 / 1.15)
        ).pack(side="right", padx=3)

        content = ttk.Panedwindow(self, orient="horizontal")
        content.pack(fill="both", expand=True)
        map_frame = ttk.Frame(content)
        sidebar = ttk.Frame(content, width=370, padding=8)
        content.add(map_frame, weight=5)
        content.add(sidebar, weight=1)

        self.canvas = tk.Canvas(
            map_frame,
            background="#1c1c1c",
            cursor="crosshair",
            highlightthickness=0,
        )
        horizontal = ttk.Scrollbar(
            map_frame, orient="horizontal", command=self.canvas.xview
        )
        vertical = ttk.Scrollbar(
            map_frame, orient="vertical", command=self.canvas.yview
        )
        self.canvas.configure(
            xscrollcommand=horizontal.set,
            yscrollcommand=vertical.set,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        map_frame.rowconfigure(0, weight=1)
        map_frame.columnconfigure(0, weight=1)

        self.canvas.bind("<Button-1>", self.place_device)
        self.canvas.bind("<ButtonPress-3>", self.start_pan)
        self.canvas.bind("<B3-Motion>", self.pan)
        self.canvas.bind("<MouseWheel>", self.mouse_wheel_zoom)
        self.canvas.bind(
            "<Button-4>", lambda event: self.change_zoom(1.12, event)
        )
        self.canvas.bind(
            "<Button-5>", lambda event: self.change_zoom(1 / 1.12, event)
        )

        ttk.Label(
            sidebar,
            text=f"Aktualne urządzenie — {self.type_label}",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            sidebar,
            textvariable=self.current_var,
            font=("Segoe UI", 16, "bold"),
            wraplength=340,
        ).pack(anchor="w", pady=(5, 2))
        ttk.Label(
            sidebar,
            textvariable=self.detail_var,
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(0, 9))

        search_label = "Przejdź do ID / nazwy [Ctrl+F]:" if self.device_type == "cabinet" else "Przejdź do IP / nazwy [Ctrl+F]:"
        ttk.Label(sidebar, text=search_label).pack(anchor="w")
        search_row = ttk.Frame(sidebar)
        search_row.pack(fill="x", pady=(3, 8))
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", self.search_device)
        ttk.Button(search_row, text="Szukaj", command=self.search_device).pack(
            side="left", padx=(5, 0)
        )

        columns = ("state", "ip", "name")
        self.tree = ttk.Treeview(
            sidebar,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=26,
        )
        self.tree.heading("state", text="")
        self.tree.heading("ip", text="ID" if self.device_type == "cabinet" else "IP")
        self.tree.heading("name", text="Nazwa")
        self.tree.column("state", width=35, anchor="center", stretch=False)
        self.tree.column("ip", width=105, anchor="center", stretch=False)
        self.tree.column("name", width=190, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(8, 4))
        self.tree.bind("<<TreeviewSelect>>", self.tree_selected)
        self.tree.tag_configure("placed", foreground="#087f23")
        self.tree.tag_configure("skipped", foreground="#c56a00")
        self.tree.tag_configure("pending", foreground="#666666")

        ttk.Label(
            sidebar,
            textvariable=self.status_var,
            font=("Segoe UI", 10, "bold"),
            wraplength=340,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            sidebar,
            textvariable=self.path_var,
            wraplength=340,
            foreground="#555555",
        ).pack(anchor="w", pady=(8, 0))

        footer = ttk.Frame(self, padding=(8, 4))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.message_var).pack(anchor="w")
        self.populate_tree()

    def set_initial_zoom(self) -> None:
        self.update_idletasks()
        canvas_width = max(self.canvas.winfo_width(), 600)
        canvas_height = max(self.canvas.winfo_height(), 500)
        self.zoom = min(
            canvas_width / self.image_width,
            canvas_height / self.image_height,
        ) * 0.96
        self.zoom = max(0.05, min(4.0, self.zoom))

    def fit_image(self) -> None:
        self.set_initial_zoom()
        self.redraw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def current_device(self) -> dict | None:
        if not self.devices:
            return None
        return self.devices[self.current_index]

    def find_first_pending_index(self) -> int:
        for index, row in enumerate(self.devices):
            if normalize_status(row["MapStatus"]) == "PENDING":
                return index
        return 0

    def focus_search(self, _event=None) -> str:
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, tk.END)
        return "break"

    def search_device(self, _event=None) -> str:
        query = self.search_var.get()
        index = find_device_index(self.devices, query)
        if index is None:
            if query.strip():
                self.message_var.set(f"Nie znaleziono urządzenia dla: {query.strip()}")
            self.focus_search()
            return "break"

        self.current_index = index
        self.select_current_in_list()
        self.redraw()
        row = self.current_device()
        self.message_var.set(
            f"Przejście do: {row['IP'] or row['HostName']} — {row['VisibleName']}"
        )
        self.focus_search()
        return "break"

    def populate_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        symbols = {"PLACED": "●", "SKIPPED": "—", "PENDING": "○"}
        tags = {"PLACED": "placed", "SKIPPED": "skipped", "PENDING": "pending"}
        for index, row in enumerate(self.devices):
            status = normalize_status(row["MapStatus"])
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(symbols[status], row["IP"] or row["HostName"], row["VisibleName"]),
                tags=(tags[status],),
            )
        self.update_sidebar()

    def update_sidebar(self) -> None:
        placed = sum(
            normalize_status(row["MapStatus"]) == "PLACED" for row in self.devices
        )
        skipped = sum(
            normalize_status(row["MapStatus"]) == "SKIPPED" for row in self.devices
        )
        pending = sum(
            normalize_status(row["MapStatus"]) == "PENDING" for row in self.devices
        )
        self.status_var.set(
            f"Razem: {len(self.devices)} | Ustawione: {placed} | "
            f"Pominięte: {skipped} | Do oznaczenia: {pending}"
        )
        row = self.current_device()
        if row is None:
            self.current_var.set("Brak urządzeń tego typu")
            self.detail_var.set("")
        else:
            self.current_var.set(f"{short_device_label(row)} — {row['VisibleName']}")
            if self.device_type == "cabinet":
                self.detail_var.set(
                    f"Identyfikator lokalny: {row['HostName']}\n"
                    f"Host Zabbixa: brak\nStan mapy: {row['MapStatus']}"
                )
            else:
                self.detail_var.set(
                    f"IP: {row['IP']}\nHost: {row['HostName']}\nStan mapy: {row['MapStatus']}"
                )
        self.path_var.set(f"Zapis automatyczny:\n{self.devices_path}")

    def select_current_in_list(self) -> None:
        if not self.devices:
            return
        iid = str(self.current_index)
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self.update_sidebar()

    def tree_selected(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.current_index = int(selection[0])
        self.update_sidebar()
        self.redraw()

    def next_device(self) -> None:
        if not self.devices:
            return
        self.current_index = (self.current_index + 1) % len(self.devices)
        self.select_current_in_list()
        self.redraw()

    def previous_device(self) -> None:
        if not self.devices:
            return
        self.current_index = (self.current_index - 1) % len(self.devices)
        self.select_current_in_list()
        self.redraw()

    def skip_device(self) -> None:
        row = self.current_device()
        if row is None:
            return
        row.update(
            {
                "MapStatus": "SKIPPED",
                "X": "",
                "Y": "",
                "XPercent": "",
                "YPercent": "",
                "UpdatedAt": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.save()
        self.populate_tree()
        if self.auto_next.get():
            self.next_device()
        else:
            self.select_current_in_list()
            self.redraw()

    def clear_current(self) -> None:
        row = self.current_device()
        if row is None:
            return
        row.update(
            {
                "MapStatus": "PENDING",
                "X": "",
                "Y": "",
                "XPercent": "",
                "YPercent": "",
                "UpdatedAt": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.save()
        self.populate_tree()
        self.select_current_in_list()
        self.redraw()

    def place_device(self, event) -> None:
        row = self.current_device()
        if row is None:
            return
        x = self.canvas.canvasx(event.x) / self.zoom
        y = self.canvas.canvasy(event.y) / self.zoom
        if not (0 <= x <= self.image_width and 0 <= y <= self.image_height):
            return
        row.update(
            {
                "MapStatus": "PLACED",
                "X": f"{x:.2f}",
                "Y": f"{y:.2f}",
                "XPercent": f"{(x / self.image_width) * 100:.6f}",
                "YPercent": f"{(y / self.image_height) * 100:.6f}",
                "UpdatedAt": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.save()
        self.populate_tree()
        if self.auto_next.get():
            self.next_device()
        else:
            self.select_current_in_list()
            self.redraw()

    def save(self, *, show_message: bool = False) -> None:
        selected = {row["HostName"]: row for row in self.devices}
        merged = []
        for row in self.all_devices:
            if row["Type"] == self.device_type and row["HostName"] in selected:
                merged.append(selected[row["HostName"]])
            else:
                merged.append(row)
        self.all_devices = sort_devices(merged)
        write_devices(self.devices_path, self.all_devices)
        if show_message:
            messagebox.showinfo("Zapisano", f"Zapisano:\n{self.devices_path}")

    def redraw(self) -> None:
        self.canvas.delete("all")
        scaled_width = max(1, round(self.image_width * self.zoom))
        scaled_height = max(1, round(self.image_height * self.zoom))
        resized = self.base_image.resize(
            (scaled_width, scaled_height), Image.Resampling.LANCZOS
        )
        self.tk_image = ImageTk.PhotoImage(resized)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)

        current = self.current_device()
        current_host = current["HostName"] if current else None
        for row in self.devices:
            if normalize_status(row["MapStatus"]) != "PLACED":
                continue
            xp = parse_optional_float(row.get("XPercent"))
            yp = parse_optional_float(row.get("YPercent"))
            if xp is not None and yp is not None:
                x = (xp / 100.0) * self.image_width
                y = (yp / 100.0) * self.image_height
            else:
                x = parse_optional_float(row.get("X"))
                y = parse_optional_float(row.get("Y"))
            if x is None or y is None:
                continue
            cx = x * self.zoom
            cy = y * self.zoom
            is_current = row["HostName"] == current_host
            if self.device_type == "cabinet":
                half_width = 7 if is_current else 6
                half_height = 10 if is_current else 8
                self.canvas.create_rectangle(
                    cx - half_width,
                    cy - half_height,
                    cx + half_width,
                    cy + half_height,
                    fill=self.color,
                    outline="#ffffff" if is_current else "#202020",
                    width=2 if is_current else 1,
                )
                for offset in (-4, 1, 6):
                    self.canvas.create_line(
                        cx - half_width + 2,
                        cy + offset,
                        cx + half_width - 2,
                        cy + offset,
                        fill="#b8b8b8",
                        width=1,
                    )
            else:
                radius = 6 if is_current else 4
                self.canvas.create_oval(
                    cx - radius,
                    cy - radius,
                    cx + radius,
                    cy + radius,
                    fill=self.color,
                    outline="#ffffff" if is_current else self.color,
                    width=2 if is_current else 1,
                )
            if self.show_numbers.get():
                label = short_device_label(row)
                self.canvas.create_text(
                    cx + 7,
                    cy - 7,
                    text=label,
                    anchor="sw",
                    fill="#111111",
                    font=("Segoe UI", 8, "bold"),
                )
                self.canvas.create_text(
                    cx + 6,
                    cy - 8,
                    text=label,
                    anchor="sw",
                    fill="#ffffff",
                    font=("Segoe UI", 8, "bold"),
                )

        # Niebieska obwódka musi być rysowana po wszystkich punktach i etykietach.
        # Dzięki temu aktualnie zaznaczone urządzenie jest widoczne także wtedy,
        # gdy leży blisko innego punktu lub tekstu etykiety.
        if current and normalize_status(current["MapStatus"]) == "PLACED":
            xp = parse_optional_float(current.get("XPercent"))
            yp = parse_optional_float(current.get("YPercent"))
            if xp is not None and yp is not None:
                x = (xp / 100.0) * self.image_width
                y = (yp / 100.0) * self.image_height
            else:
                x = parse_optional_float(current.get("X"))
                y = parse_optional_float(current.get("Y"))
            if x is not None and y is not None:
                cx = x * self.zoom
                cy = y * self.zoom
                selection_radius = max(7, round(10 * min(self.zoom, 1.5)))
                selection_width = max(2, round(3 * min(self.zoom, 1.5)))
                self.canvas.create_oval(
                    cx - selection_radius,
                    cy - selection_radius,
                    cx + selection_radius,
                    cy + selection_radius,
                    outline="#006eff",
                    width=selection_width,
                )
        self.canvas.configure(scrollregion=(0, 0, scaled_width, scaled_height))

    def change_zoom(self, factor: float, event=None) -> None:
        old_zoom = self.zoom
        new_zoom = max(0.05, min(5.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 0.0001:
            return
        if event is not None:
            before_x = self.canvas.canvasx(event.x) / old_zoom
            before_y = self.canvas.canvasy(event.y) / old_zoom
        else:
            before_x = before_y = None
        self.zoom = new_zoom
        self.redraw()
        if event is not None and before_x is not None and before_y is not None:
            target_x = before_x * new_zoom - event.x
            target_y = before_y * new_zoom - event.y
            region = self.canvas.cget("scrollregion").split()
            if len(region) == 4:
                total_width = max(float(region[2]), 1)
                total_height = max(float(region[3]), 1)
                self.canvas.xview_moveto(max(0, target_x / total_width))
                self.canvas.yview_moveto(max(0, target_y / total_height))

    def mouse_wheel_zoom(self, event) -> None:
        self.change_zoom(1.12 if event.delta > 0 else 1 / 1.12, event)

    def start_pan(self, event) -> None:
        self.canvas.scan_mark(event.x, event.y)

    def pan(self, event) -> None:
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def on_close(self) -> None:
        try:
            self.save()
        finally:
            self.destroy()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--type", required=True, choices=["camera", "recorder", "switch", "cabinet"])
    parser.add_argument("--reset-skipped", action="store_true")
    args = parser.parse_args()

    devices_path = Path(args.devices).resolve()
    image_path = Path(args.map).resolve()
    if not devices_path.is_file():
        raise SystemExit(f"Nie znaleziono pliku:\n{devices_path}")
    if not image_path.is_file():
        raise SystemExit(f"Nie znaleziono mapy:\n{image_path}")

    app = DeviceMapClicker(
        devices_path,
        image_path,
        args.type,
        reset_skipped=args.reset_skipped,
    )
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
