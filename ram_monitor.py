import os
import sys
import json
import threading
import time
import psutil
from pathlib import Path

if os.name != 'nt':
    sys.exit("Только Windows")

import win32event
import win32api
import win32con
import gc

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
MUTEX_NAME = "RAMMonitor_v15_2026"
g_mutex = None
stop_monitoring = threading.Event()
monitor_thread = None

_window_lock = threading.Lock()
_window_open = False

_last_notification_time = 0
_NOTIFICATION_COOLDOWN = 10  # 10 секунд между уведомлениями

# === ЗАЩИТА КОНФИГУРАЦИИ И СОСТОЯНИЙ ===
config_lock = threading.RLock()

config = {
    "threshold_medium": 70,
    "threshold_critical": 90,
    "check_interval": 3,
    "enable_notifications": True
}
prev_thresholds = {"medium": 70, "critical": 90}
notification_state = {"medium": False, "critical": False}

CONFIG_FILE = Path.home() / ".ram_monitor_config.json"

def load_config():
    with config_lock:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    tm = max(10, min(95, int(data.get("threshold_medium", 70))))
                    tc = max(tm + 1, min(100, int(data.get("threshold_critical", 90))))
                    iv = max(1, min(30, int(data.get("check_interval", 3))))
                    en = bool(data.get("enable_notifications", True))
                    config.update({
                        "threshold_medium": tm,
                        "threshold_critical": tc,
                        "check_interval": iv,
                        "enable_notifications": en
                    })
                    prev_thresholds["medium"] = tm
                    prev_thresholds["critical"] = tc
            except Exception:
                pass

def save_config():
    with config_lock:
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            prev_thresholds["medium"] = config["threshold_medium"]
            prev_thresholds["critical"] = config["threshold_critical"]
        except Exception:
            pass

def get_config_copy():
    with config_lock:
        return config.copy()

def get_notification_state():
    with config_lock:
        return notification_state.copy()

def set_notification_state(medium, critical):
    with config_lock:
        notification_state["medium"] = medium
        notification_state["critical"] = critical

def reset_notification_flags():
    with config_lock:
        notification_state["medium"] = False
        notification_state["critical"] = False

def thresholds_changed():
    with config_lock:
        return (
            config["threshold_medium"] != prev_thresholds["medium"] or
            config["threshold_critical"] != prev_thresholds["critical"]
        )

def update_prev_thresholds():
    with config_lock:
        prev_thresholds["medium"] = config["threshold_medium"]
        prev_thresholds["critical"] = config["threshold_critical"]

# === СОЗДАНИЕ ИКОНОК ===
def create_tray_icon(color_rgb):
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size, size), fill=(18, 18, 18, 240))
    margin = 6
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color_rgb)
    for i in range(2):
        draw.ellipse(
            (margin + 2 - i, margin + 2 - i, size - margin - 2 + i, size - margin - 2 + i),
            outline=(*color_rgb, 100),
            width=1
        )
    return img

ICONS = {
    "normal": create_tray_icon((50, 190, 90)),
    "medium": create_tray_icon((235, 175, 60)),
    "critical": create_tray_icon((235, 70, 80))
}

# === МОДАЛЬНОЕ УВЕДОМЛЕНИЕ (НЕБЛОКИРУЮЩЕЕ) ===
def show_modal_notification(title, message):
    global _window_open, _last_notification_time

    cfg = get_config_copy()
    if not cfg.get("enable_notifications", True):
        return

    now = time.time()
    if now - _last_notification_time < _NOTIFICATION_COOLDOWN:
        return

    with _window_lock:
        if _window_open:
            return
        _window_open = True

    def run_dialog():
        global _window_open, _last_notification_time
        root = None
        try:
            import tkinter as tk
            from tkinter import ttk
            root = tk.Tk()
            root.withdraw()

            dialog = tk.Toplevel(root)
            dialog.title(title)
            dialog.geometry("420x180")
            dialog.resizable(False, False)
            dialog.attributes("-topmost", True)
            dialog.configure(bg="#121212")
            dialog.focus_force()
            dialog.grab_set()

            x = (dialog.winfo_screenwidth() - 420) // 2
            y = (dialog.winfo_screenheight() - 180) // 2
            dialog.geometry(f"+{x}+{y}")

            style = ttk.Style()
            style.theme_use('clam')
            style.configure("TLabel", background="#121212", foreground="#e0e0e0", font=("Segoe UI Variable", 11))
            style.configure("TButton",
                background="#2d2d2d",
                foreground="#ffffff",
                font=("Segoe UI Variable", 10, "bold"),
                padding=(12, 6)
            )
            style.map("TButton", background=[('active', '#3a3a3a')])

            frame = ttk.Frame(dialog, padding=20)
            frame.pack(fill="both", expand=True)

            label = ttk.Label(frame, text=message, wraplength=380, justify="center")
            label.pack(pady=15)

            def on_ok():
                dialog.destroy()
                root.destroy()

            btn = ttk.Button(frame, text="OK", command=on_ok, width=10)
            btn.pack(pady=10)
            btn.focus_set()

            dialog.bind("<Escape>", lambda e: on_ok())
            dialog.protocol("WM_DELETE_WINDOW", on_ok)

            root.mainloop()

        except Exception:
            pass
        finally:
            with _window_lock:
                _window_open = False
            _last_notification_time = time.time()
            if root:
                try:
                    root.destroy()
                except:
                    pass
            gc.collect()

    # Запуск БЕЗ блокировки основного потока
    th = threading.Thread(target=run_dialog)
    th.start()
    # ВАЖНО: НЕ вызываем th.join() — это было причиной зависаний!

# === МОНИТОРИНГ ОЗУ ===
def monitor_ram_loop(tray_ref):
    last_status = "normal"

    while not stop_monitoring.is_set():
        try:
            mem = psutil.virtual_memory()
            usage = mem.percent

            cfg = get_config_copy()
            if usage >= cfg["threshold_critical"]:
                status = "critical"
            elif usage >= cfg["threshold_medium"]:
                status = "medium"
            else:
                status = "normal"

            icon_obj = tray_ref()
            if icon_obj:
                try:
                    tooltip = f"RAM: {usage:.1f}%\nИспользовано: {mem.used // (1024**2)} MB"
                    icon_obj.title = tooltip
                    if status != last_status:
                        icon_obj.icon = ICONS[status]
                        last_status = status
                except Exception:
                    pass

            if thresholds_changed():
                update_prev_thresholds()
                reset_notification_flags()

            nstate = get_notification_state()
            should_notify = False
            msg_title = ""
            msg_text = ""

            if status == "critical":
                if not nstate["critical"]:
                    set_notification_state(True, True)
                    should_notify = True
                    msg_title = "⚠️ Критическая нагрузка ОЗУ"
                    msg_text = f"Использование оперативной памяти достигло {usage:.1f}%.\n\nРекомендуется закрыть неиспользуемые приложения."
            elif status == "medium":
                if not nstate["medium"]:
                    set_notification_state(True, nstate["critical"])
                    should_notify = True
                    msg_title = "ℹ️ Средняя нагрузка ОЗУ"
                    msg_text = f"Использование оперативной памяти: {usage:.1f}%."
            else:
                reset_notification_flags()

            if should_notify:
                show_modal_notification(msg_title, msg_text)

            stop_monitoring.wait(timeout=cfg["check_interval"])

        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            stop_monitoring.wait(timeout=2)
        except Exception:
            stop_monitoring.wait(timeout=2)

# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ОКON ===
def _open_window(window_func):
    global _window_open
    with _window_lock:
        if _window_open:
            return
        _window_open = True

    def wrapper():
        global _window_open
        try:
            window_func()
        except Exception:
            pass
        finally:
            with _window_lock:
                _window_open = False
            gc.collect()

    th = threading.Thread(target=wrapper)
    th.start()
    # НЕТ join() — неблокирующий запуск

# === ОКНО "О ПРОГРАММЕ" ===
def show_about():
    def run():
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.title("О программе")
        root.geometry("380x240")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.configure(bg="#121212")

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TLabel", background="#121212", foreground="#e0e0e0", font=("Segoe UI Variable", 11))
        style.configure("TButton",
            background="#2d2d2d",
            foreground="#ffffff",
            font=("Segoe UI Variable", 10, "bold"),
            padding=(10, 5)
        )
        style.map("TButton", background=[('active', '#3a3a3a')])

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)

        info = (
            "RAM Monitor v2.9\n\n"
            "📧 korobchenko.artjom@yandex.ru\n"
            "📱 Telegram: @Darctorim\n\n"
            "© 2026 Все права защищены."
        )
        label = ttk.Label(frame, text=info, justify="center")
        label.pack(pady=15)

        def close():
            root.destroy()

        btn = ttk.Button(frame, text="Закрыть", command=close, width=14)
        btn.pack(pady=10)

        root.protocol("WM_DELETE_WINDOW", close)
        root.mainloop()
        del root

    _open_window(run)

# === ОКНО НАСТРОЕК ===
def open_settings(icon, item):
    def run():
        import tkinter as tk
        from tkinter import ttk, messagebox
        root = tk.Tk()
        root.title("Настройки")
        root.geometry("400x280")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.configure(bg="#121212")

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TFrame", background="#121212")
        style.configure("TLabel", background="#121212", foreground="#e0e0e0", font=("Segoe UI Variable", 11))
        style.configure("TEntry",
            fieldbackground="#1e1e1e",
            foreground="#ffffff",
            insertcolor="#ffffff",
            font=("Segoe UI", 10)
        )
        style.configure("TButton",
            background="#2d2d2d",
            foreground="#ffffff",
            font=("Segoe UI Variable", 10, "bold"),
            padding=(10, 5)
        )
        style.map("TButton", background=[('active', '#3a3a3a')])
        style.configure("TCheckbutton", background="#121212", foreground="#e0e0e0", font=("Segoe UI", 11))

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)

        tm_var = tk.StringVar(value=str(config["threshold_medium"]))
        tc_var = tk.StringVar(value=str(config["threshold_critical"]))
        iv_var = tk.StringVar(value=str(config["check_interval"]))
        notify_var = tk.BooleanVar(value=config["enable_notifications"])

        ttk.Label(frame, text="Порог средней нагрузки (%):").grid(row=0, column=0, sticky="w", pady=8)
        ttk.Entry(frame, textvariable=tm_var, width=10).grid(row=0, column=1, padx=10, sticky="w")

        ttk.Label(frame, text="Порог критической нагрузки (%):").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Entry(frame, textvariable=tc_var, width=10).grid(row=1, column=1, padx=10, sticky="w")

        ttk.Label(frame, text="Интервал проверки (сек):").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Entry(frame, textvariable=iv_var, width=10).grid(row=2, column=1, padx=10, sticky="w")

        cb = ttk.Checkbutton(frame, text="Включить уведомления", variable=notify_var)
        cb.grid(row=3, column=0, columnspan=2, sticky="w", pady=12)

        def save_and_close():
            try:
                tm = int(tm_var.get())
                tc = int(tc_var.get())
                iv = int(iv_var.get())
                if not (10 <= tm < tc <= 100) or not (1 <= iv <= 30):
                    messagebox.showerror("Ошибка", "Проверьте диапазоны значений.")
                    return

                with config_lock:
                    config.update({
                        "threshold_medium": tm,
                        "threshold_critical": tc,
                        "check_interval": iv,
                        "enable_notifications": notify_var.get()
                    })
                save_config()
                root.destroy()
            except ValueError:
                messagebox.showerror("Ошибка", "Введите целые числа.")

        def cancel():
            root.destroy()

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=15)
        ttk.Button(btn_frame, text="Сохранить", command=save_and_close, width=12).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="Отмена", command=cancel, width=12).pack(side="left", padx=8)

        root.protocol("WM_DELETE_WINDOW", cancel)
        root.mainloop()
        del root

    _open_window(run)

# === МЕНЮ ТРЕЯ ===
def on_about(icon, item):
    show_about()

def on_quit(icon, item):
    stop_monitoring.set()
    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=2)
    icon.stop()
    if g_mutex:
        win32api.CloseHandle(g_mutex)
    gc.collect()

# === ГЛАВНЫЙ ЗАПУСК ===
def main():
    global g_mutex, monitor_thread

    # Убиваем старые копии
    current_pid = os.getpid()
    current_name = Path(sys.executable).name.lower()
    if current_name == "python.exe":
        current_name = Path(__file__).name.lower()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['pid'] == current_pid:
                continue
            name = proc.info['name'].lower()
            cmdline = [x.lower() for x in (proc.info['cmdline'] or [])]
            if current_name in name or any(current_name in c for c in cmdline):
                proc.kill()
                proc.wait(timeout=1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Мьютекс для одиночного экземпляра
    try:
        g_mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
        if win32api.GetLastError() == win32con.ERROR_ALREADY_EXISTS:
            win32api.CloseHandle(g_mutex)
            sys.exit(0)
    except Exception:
        pass

    load_config()

    from pystray import Icon, Menu, MenuItem

    # Инициализация tooltip
    try:
        mem = psutil.virtual_memory()
        initial_tooltip = f"RAM: {mem.percent:.1f}%\nИспользовано: {mem.used // (1024**2)} MB"
    except:
        initial_tooltip = "RAM Monitor\nГотов к работе..."

    menu = Menu(
        MenuItem("Настройки", open_settings),
        MenuItem("О программе", on_about),
        Menu.SEPARATOR,
        MenuItem("Выход", on_quit)
    )

    icon = Icon("RAMMonitor", ICONS["normal"], initial_tooltip, menu)

    import weakref
    icon_ref = weakref.ref(icon)

    monitor_thread = threading.Thread(target=monitor_ram_loop, args=(icon_ref,))
    monitor_thread.start()

    try:
        icon.run()
    finally:
        stop_monitoring.set()
        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=2)
        if g_mutex:
            win32api.CloseHandle(g_mutex)
        gc.collect()

if __name__ == "__main__":
    main()
