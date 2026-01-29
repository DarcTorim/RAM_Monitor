#!/usr/bin/env python3
"""
Монитор ОЗУ: отслеживает использование памяти и отправляет уведомления при превышении порога
"""

import psutil
import time
import argparse
import logging
from datetime import datetime

# Импорт системных уведомлений в зависимости от ОС
try:
    import plyer
    NOTIFICATION_BACKEND = "plyer"
except ImportError:
    try:
        from win10toast import ToastNotifier
        NOTIFICATION_BACKEND = "win10toast"
    except (ImportError, ModuleNotFoundError):
        NOTIFICATION_BACKEND = "fallback"


def setup_logging():
    """Настройка логирования"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        handlers=[
            logging.FileHandler('ram_monitor.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )


def send_notification(title, message):
    """Отправка системного уведомления"""
    try:
        if NOTIFICATION_BACKEND == "plyer":
            plyer.notification.notify(
                title=title,
                message=message,
                app_name="RAM Monitor",
                timeout=10
            )
        elif NOTIFICATION_BACKEND == "win10toast":
            toaster = ToastNotifier()
            toaster.show_toast(title, message, duration=10)
        else:
            # Fallback: вывод в консоль с выделением
            print(f"\n{'='*50}")
            print(f"🔔 {title}")
            print(f"   {message}")
            print(f"{'='*50}\n")
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления: {e}")


def check_memory(threshold=90):
    """Проверка использования памяти"""
    mem = psutil.virtual_memory()
    used_percent = mem.percent
    used_gb = mem.used / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)
    
    status = {
        'percent': used_percent,
        'used_gb': used_gb,
        'total_gb': total_gb,
        'available_gb': mem.available / (1024 ** 3)
    }
    
    logging.info(f"ОЗУ: {used_percent:.1f}% | Использовано: {used_gb:.2f} ГБ / {total_gb:.2f} ГБ")
    
    if used_percent > threshold:
        message = (
            f"Использование ОЗУ: {used_percent:.1f}%\n"
            f"Занято: {used_gb:.2f} ГБ из {total_gb:.2f} ГБ\n"
            f"Доступно: {status['available_gb']:.2f} ГБ"
        )
        send_notification("⚠️ Критическое использование ОЗУ", message)
        logging.warning(f"КРИТИЧЕСКОЕ ИСПОЛЬЗОВАНИЕ ПАМЯТИ: {used_percent:.1f}%")
    
    return status


def main():
    parser = argparse.ArgumentParser(description='Монитор использования оперативной памяти')
    parser.add_argument('--threshold', type=float, default=90.0,
                        help='Порог срабатывания уведомления (по умолчанию: 90%%)')
    parser.add_argument('--interval', type=int, default=30,
                        help='Интервал проверки в секундах (по умолчанию: 30)')
    parser.add_argument('--oneshot', action='store_true',
                        help='Проверить один раз и выйти')
    
    args = parser.parse_args()
    setup_logging()
    
    logging.info(f"Запуск монитора ОЗУ (порог: {args.threshold}%, интервал: {args.interval}с)")
    
    try:
        if args.oneshot:
            check_memory(args.threshold)
            return
        
        print(f"Монитор ОЗУ запущен. Проверка каждые {args.interval} секунд...")
        print(f"Для остановки нажмите Ctrl+C\n")
        
        while True:
            check_memory(args.threshold)
            time.sleep(args.interval)
    
    except KeyboardInterrupt:
        logging.info("Монитор остановлен пользователем")
        print("\nМонитор ОЗУ остановлен.")


if __name__ == "__main__":
    main()