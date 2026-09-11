import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from main import run_app

    if __name__ == "__main__":
        run_app()
except Exception:
    import traceback
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("雨课堂助手启动失败", traceback.format_exc()[-1500:])
