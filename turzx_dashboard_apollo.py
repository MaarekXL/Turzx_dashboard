from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import platform
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
from PIL import Image, ImageChops, ImageDraw, ImageFont


# ============================================================
# CONFIGURATION
# ============================================================

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"


@dataclass
class Config:
    com_port: str = "AUTO"
    brightness: int = 30          # Rev A: keep <= 50% (recommended by upstream)
    refresh_seconds: float = 1.0
    gpu_index: int = 0
    orientation: str = "landscape"
    preview_file: str = "preview_apollo.png"
    background_file: str = "apollo_background.png"
    use_lhm_direct: bool = True
    show_hostname: bool = True
    accent: tuple[int, int, int] = (255, 52, 28)
    accent2: tuple[int, int, int] = (255, 132, 36)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for key, value in raw.items():
                    if hasattr(cfg, key):
                        if key in {"accent", "accent2"} and isinstance(value, list):
                            value = tuple(value)
                        setattr(cfg, key, value)
            except Exception as exc:
                print(f"[WARN] config.json invalide, valeurs par défaut utilisées: {exc}")
        return cfg

    def save(self) -> None:
        data = asdict(self)
        data["accent"] = list(self.accent)
        data["accent2"] = list(self.accent2)
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ============================================================
# SENSOR DATA
# ============================================================


@dataclass
class Snapshot:
    timestamp: datetime
    cpu_name: str = "CPU"
    cpu_usage: float = 0.0
    cpu_temp: Optional[float] = None
    cpu_power: Optional[float] = None
    cpu_clock_mhz: Optional[float] = None
    cpu_fan_rpm: Optional[float] = None

    gpu_name: str = "GPU"
    gpu_usage: Optional[float] = None
    gpu_temp: Optional[float] = None
    gpu_power: Optional[float] = None
    gpu_clock_mhz: Optional[float] = None
    gpu_vram_used_mb: Optional[float] = None
    gpu_vram_total_mb: Optional[float] = None
    gpu_fan_percent: Optional[float] = None

    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_percent: float = 0.0

    disk_percent: float = 0.0
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0

    net_down_mbps: float = 0.0
    net_up_mbps: float = 0.0

    uptime_seconds: float = 0.0
    hostname: str = "PC"
    os_name: str = "Windows"


class SensorReader:
    """Collects telemetry with graceful fallbacks.

    - CPU/RAM/disk/network: psutil
    - NVIDIA GPU: NVML (package `nvidia-ml-py`)
    - CPU temperature/power/fan/clock on Windows: LibreHardwareMonitorLib.dll
      bundled with TURZX-Dashboard (no WMI dependency).
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._prev_net = psutil.net_io_counters()
        self._prev_net_time = time.monotonic()
        self._nvml = None
        self._nvml_handle = None
        self._lhm_hw = None
        self._lhm_handle = None
        self.cpu_name = self._detect_cpu_name()
        self.hostname = socket.gethostname()
        self.os_name = self._detect_os_name()

        # Prime cpu_percent so the first real value is meaningful.
        psutil.cpu_percent(interval=None)
        self._init_nvml()
        if cfg.use_lhm_direct:
            self._init_lhm_direct()

    @staticmethod
    def _detect_cpu_name() -> str:
        # Windows registry gives a much better CPU name than platform.processor().
        if os.name == "nt":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                )
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                winreg.CloseKey(key)
                return " ".join(str(value).split())
            except Exception:
                pass
        value = platform.processor() or platform.machine() or "CPU"
        return " ".join(value.split())

    @staticmethod
    def _detect_os_name() -> str:
        if os.name == "nt":
            return f"Windows {platform.release()}"
        return f"{platform.system()} {platform.release()}"

    def _init_nvml(self) -> None:
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = pynvml
            self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(self.cfg.gpu_index)
            print("[OK] NVIDIA NVML actif")
        except Exception as exc:
            self._nvml = None
            self._nvml_handle = None
            print(f"[INFO] NVML indisponible: {exc}")

    def _init_lhm_direct(self) -> None:
        """Load the LibreHardwareMonitor DLL bundled by the upstream project.

        This deliberately does not use the WMI provider: recent LHM builds have
        had WMI regressions, while TURZX-Dashboard already ships the
        library and uses it directly through pythonnet.
        """
        if os.name != "nt":
            return

        try:
            if ctypes.windll.shell32.IsUserAnAdmin() == 0:
                print(
                    "[INFO] LibreHardwareMonitor direct désactivé: lance PyCharm en administrateur "
                    "pour CPU température/puissance/ventilateurs."
                )
                return
        except Exception:
            pass

        try:
            import clr  # pythonnet, already required by upstream

            lhm_dir = APP_DIR / "external" / "LibreHardwareMonitor"
            lhm_dll = lhm_dir / "LibreHardwareMonitorLib.dll"
            hid_dll = lhm_dir / "HidSharp.dll"
            if not lhm_dll.exists():
                raise FileNotFoundError(f"DLL introuvable: {lhm_dll}")

            clr.AddReference(str(lhm_dll))
            if hid_dll.exists():
                clr.AddReference(str(hid_dll))

            from LibreHardwareMonitor import Hardware

            handle = Hardware.Computer()
            handle.IsCpuEnabled = True
            handle.IsMotherboardEnabled = True
            handle.IsControllerEnabled = True
            # GPU stays on NVML in this dashboard, but enabling it is harmless and
            # useful when inspecting sensors during development.
            handle.IsGpuEnabled = True
            handle.Open()

            self._lhm_hw = Hardware
            self._lhm_handle = handle

            cpu_names = [
                str(hw.Name) for hw in handle.Hardware
                if hw.HardwareType == Hardware.HardwareType.Cpu
            ]
            if cpu_names:
                print(f"[OK] LibreHardwareMonitor DLL actif - CPU: {cpu_names[0]}")
            else:
                print("[WARN] LibreHardwareMonitor chargé mais aucun CPU détecté")
        except Exception as exc:
            self._lhm_hw = None
            self._lhm_handle = None
            print(f"[INFO] LibreHardwareMonitor DLL indisponible: {exc}")

    def _get_lhm_hardware(self, hw_type):
        if self._lhm_handle is None:
            return None
        try:
            for hw in self._lhm_handle.Hardware:
                if hw.HardwareType == hw_type:
                    hw.Update()
                    return hw
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _read_lhm_cpu(self) -> dict[str, Optional[float]]:
        """Read the exact sensors exposed by the user's i5-13600K / MSI B760M.

        Prefer exact sensor names from LibreHardwareMonitor 0.9.6:
        - Temperature / CPU Package
        - Power / CPU Package
        - Clock / P-Core #1..#6 and E-Core #1..#8
        - Motherboard SuperIO / Fan / CPU Fan

        SensorType is compared by string as well as enum identity because
        pythonnet enum comparisons can vary between runtime versions.
        """
        out = {"temp": None, "power": None, "fan": None, "clock": None}
        if self._lhm_handle is None or self._lhm_hw is None:
            return out

        H = self._lhm_hw
        cpu = self._get_lhm_hardware(H.HardwareType.Cpu)
        if cpu is None:
            return out

        temp_fallbacks = []
        power_fallbacks = []
        clocks = []

        try:
            for sensor in cpu.Sensors:
                value = self._safe_float(sensor.Value)
                if value is None:
                    continue

                name = str(sensor.Name).strip()
                sensor_type = str(sensor.SensorType).strip()

                if sensor_type == "Temperature":
                    # Exact value exposed by this 13600K.
                    if name == "CPU Package":
                        out["temp"] = value
                    elif name == "Core Max":
                        temp_fallbacks.append((0, value))
                    elif name == "Core Average":
                        temp_fallbacks.append((1, value))
                    elif name.startswith(("P-Core #", "E-Core #")) and "Distance to TjMax" not in name:
                        temp_fallbacks.append((2, value))

                elif sensor_type == "Power":
                    # Exact package consumption exposed by this 13600K.
                    if name == "CPU Package":
                        out["power"] = value
                    elif "Package" in name:
                        power_fallbacks.append((0, value))
                    elif name == "CPU Cores":
                        power_fallbacks.append((1, value))

                elif sensor_type == "Clock":
                    if (name.startswith("P-Core #") or name.startswith("E-Core #")) and "Distance" not in name:
                        clocks.append(value)
        except Exception as exc:
            print(f"[WARN] Lecture capteurs CPU LHM: {exc}")

        if out["temp"] is None and temp_fallbacks:
            out["temp"] = min(temp_fallbacks, key=lambda x: x[0])[1]
        if out["power"] is None and power_fallbacks:
            out["power"] = min(power_fallbacks, key=lambda x: x[0])[1]
        if clocks:
            out["clock"] = sum(clocks) / len(clocks)

        # CPU Fan is exposed by the Nuvoton SuperIO below the motherboard.
        mb = self._get_lhm_hardware(H.HardwareType.Motherboard)
        fan_fallbacks = []
        if mb is not None:
            try:
                for sub in mb.SubHardware:
                    sub.Update()
                    for sensor in sub.Sensors:
                        value = self._safe_float(sensor.Value)
                        if value is None or value <= 0:
                            continue
                        name = str(sensor.Name).strip()
                        sensor_type = str(sensor.SensorType).strip()
                        if sensor_type != "Fan":
                            continue
                        if name == "CPU Fan":
                            out["fan"] = value
                            break
                        if "cpu" in name.lower():
                            fan_fallbacks.append((0, value))
                        else:
                            fan_fallbacks.append((1, value))
                    if out["fan"] is not None:
                        break
            except Exception as exc:
                print(f"[WARN] Lecture ventilateur CPU LHM: {exc}")

        if out["fan"] is None and fan_fallbacks:
            out["fan"] = min(fan_fallbacks, key=lambda x: x[0])[1]

        return out

    def _read_gpu(self) -> dict:
        out = {
            "name": "GPU",
            "usage": None,
            "temp": None,
            "power": None,
            "clock": None,
            "vram_used": None,
            "vram_total": None,
            "fan": None,
        }
        if self._nvml is None or self._nvml_handle is None:
            return out

        n = self._nvml
        h = self._nvml_handle
        try:
            name = n.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode(errors="replace")
            out["name"] = str(name)
        except Exception:
            pass
        try:
            util = n.nvmlDeviceGetUtilizationRates(h)
            out["usage"] = float(util.gpu)
        except Exception:
            pass
        try:
            out["temp"] = float(n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU))
        except Exception:
            pass
        try:
            out["power"] = float(n.nvmlDeviceGetPowerUsage(h)) / 1000.0
        except Exception:
            pass
        try:
            out["clock"] = float(n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_GRAPHICS))
        except Exception:
            pass
        try:
            mem = n.nvmlDeviceGetMemoryInfo(h)
            out["vram_used"] = float(mem.used) / (1024 * 1024)
            out["vram_total"] = float(mem.total) / (1024 * 1024)
        except Exception:
            pass
        try:
            out["fan"] = float(n.nvmlDeviceGetFanSpeed(h))
        except Exception:
            pass
        return out

    def read(self) -> Snapshot:
        now = datetime.now()
        cpu_usage = float(psutil.cpu_percent(interval=None))
        cpu_freq = psutil.cpu_freq()
        cpu_clock = float(cpu_freq.current) if cpu_freq else None

        ram = psutil.virtual_memory()
        ram_used = (ram.total - ram.available) / (1024 ** 3)
        ram_total = ram.total / (1024 ** 3)

        root = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
        try:
            disk = psutil.disk_usage(root)
            disk_percent = float(disk.percent)
            disk_used = disk.used / (1024 ** 3)
            disk_total = disk.total / (1024 ** 3)
        except Exception:
            disk_percent = disk_used = disk_total = 0.0

        net = psutil.net_io_counters()
        t = time.monotonic()
        dt = max(t - self._prev_net_time, 0.001)
        down = max(net.bytes_recv - self._prev_net.bytes_recv, 0) / dt / (1024 ** 2)
        up = max(net.bytes_sent - self._prev_net.bytes_sent, 0) / dt / (1024 ** 2)
        self._prev_net = net
        self._prev_net_time = t

        lhm = self._read_lhm_cpu()
        if lhm.get("clock") is not None:
            cpu_clock = lhm["clock"]
        gpu = self._read_gpu()

        return Snapshot(
            timestamp=now,
            cpu_name=self.cpu_name,
            cpu_usage=cpu_usage,
            cpu_temp=lhm["temp"],
            cpu_power=lhm["power"],
            cpu_clock_mhz=cpu_clock,
            cpu_fan_rpm=lhm["fan"],
            gpu_name=gpu["name"],
            gpu_usage=gpu["usage"],
            gpu_temp=gpu["temp"],
            gpu_power=gpu["power"],
            gpu_clock_mhz=gpu["clock"],
            gpu_vram_used_mb=gpu["vram_used"],
            gpu_vram_total_mb=gpu["vram_total"],
            gpu_fan_percent=gpu["fan"],
            ram_used_gb=ram_used,
            ram_total_gb=ram_total,
            ram_percent=float(ram.percent),
            disk_percent=disk_percent,
            disk_used_gb=disk_used,
            disk_total_gb=disk_total,
            net_down_mbps=down,
            net_up_mbps=up,
            uptime_seconds=time.time() - psutil.boot_time(),
            hostname=self.hostname,
            os_name=self.os_name,
        )

    def close(self) -> None:
        if self._lhm_handle is not None:
            try:
                self._lhm_handle.Close()
            except Exception:
                pass
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass


# ============================================================
# RENDERER — APOLLO CORE
# ============================================================


class Theme:
    BG = (3, 3, 3)
    TEXT = (244, 211, 130)
    TEXT2 = (225, 188, 108)
    MUTED = (182, 152, 90)
    DIM = (88, 72, 45)
    BAR_BG = (34, 30, 22)
    WHITE = (245, 239, 230)


class DashboardRenderer:
    WIDTH = 480
    HEIGHT = 320

    DYNAMIC_ZONES = [
        (20, 68, 125, 123),   # guidance core
        (20, 142, 125, 197),  # memory bank
        (20, 218, 125, 284),  # power cells
        (356, 68, 462, 123),  # visual array
        (356, 142, 462, 197), # telemetry link
        (356, 218, 462, 284), # storage bay
        (136, 52, 344, 226),  # center display
        (136, 229, 344, 288), # mission status
        (24, 294, 458, 314),  # footer
    ]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fonts = self._load_fonts()
        self.cpu_hist = deque([0.0] * 24, maxlen=24)
        self.gpu_hist = deque([0.0] * 24, maxlen=24)
        self.net_hist = deque([0.0] * 24, maxlen=24)
        self.background = self._load_background()

    @staticmethod
    def _font_candidates() -> list[Path]:
        return [
            Path(r"C:\Windows\Fonts\consola.ttf"),
            Path(r"C:\Windows\Fonts\consolab.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]

    def _load_fonts(self) -> dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
        path = next((p for p in self._font_candidates() if p.exists()), None)
        result = {}
        for size in [7, 8, 9, 10, 11, 12, 13, 15, 18, 24]:
            if path:
                result[size] = ImageFont.truetype(str(path), size)
            else:
                result[size] = ImageFont.load_default()
        return result

    def f(self, size: int):
        return self.fonts[size]

    def _load_background(self) -> Image.Image:
        candidates = [
            APP_DIR / self.cfg.background_file,
            APP_DIR / "apollo_background.png",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            raise FileNotFoundError(
                "Fond Apollo introuvable. Place 'apollo_background.png' "
                "à côté de turzx_dashboard_apollo.py."
            )
        image = Image.open(path).convert("RGB")
        if image.size != (self.WIDTH, self.HEIGHT):
            image = image.resize((self.WIDTH, self.HEIGHT), Image.Resampling.LANCZOS)
        return image

    @staticmethod
    def _fmt(value: Optional[float], fmt: str = ".0f", suffix: str = "") -> str:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "--"
        return f"{value:{fmt}}{suffix}"

    @staticmethod
    def _uptime(seconds: float) -> str:
        seconds = max(0, int(seconds))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days:
            return f"{days}d {hours:02d}:{minutes:02d}"
        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def _short(text: str, n: int) -> str:
        text = str(text)
        return text if len(text) <= n else text[:n - 1] + "…"

    @staticmethod
    def _text_width(d: ImageDraw.ImageDraw, text: str, font) -> int:
        bb = d.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0]

    def _right(self, d, x_right, y, text, font, fill):
        d.text((x_right - self._text_width(d, text, font), y), text, font=font, fill=fill)

    @staticmethod
    def _bar(d: ImageDraw.ImageDraw, box, value: float, accent):
        x0, y0, x1, y1 = box
        value = max(0.0, min(float(value), 100.0))
        d.rectangle(box, fill=Theme.BAR_BG)
        fill_x = x0 + int((x1 - x0) * value / 100.0)
        if fill_x > x0:
            d.rectangle((x0, y0, fill_x, y1), fill=accent)

    @staticmethod
    def _spark(d, box, values, accent):
        x0, y0, x1, y1 = box
        d.rectangle(box, fill=Theme.BG)
        vals = list(values)
        if len(vals) < 2:
            return
        vmax = max(max(vals), 1.0)
        pts = []
        for i, v in enumerate(vals):
            px = x0 + int(i * (x1 - x0) / (len(vals) - 1))
            py = y1 - int(max(0.0, v) / vmax * max(1, y1 - y0))
            pts.append((px, py))
        d.line(pts, fill=accent, width=1)

    @staticmethod
    def _status(s: Snapshot) -> str:
        temps = [v for v in (s.cpu_temp, s.gpu_temp) if v is not None]
        peak_temp = max(temps) if temps else 0.0
        peak_load = max(s.cpu_usage, s.gpu_usage or 0.0)
        if peak_temp >= 88:
            return "ABORT"
        if peak_temp >= 78:
            return "WARNING"
        if peak_load >= 95:
            return "MAX LOAD"
        if peak_load >= 80:
            return "ACTIVE"
        return "NOMINAL"

    def render(self, s: Snapshot) -> Image.Image:
        cpu_u = max(0.0, min(s.cpu_usage, 100.0))
        gpu_u = max(0.0, min(s.gpu_usage or 0.0, 100.0))
        self.cpu_hist.append(cpu_u)
        self.gpu_hist.append(gpu_u)
        self.net_hist.append(min((s.net_down_mbps + s.net_up_mbps) * 5.0, 100.0))

        im = self.background.copy()
        d = ImageDraw.Draw(im)

        # Left column
        d.text((23, 75), self._short(s.cpu_name, 14), font=self.f(7), fill=Theme.WHITE)
        d.text((23, 89), "TEMP", font=self.f(7), fill=Theme.MUTED)
        self._right(d, 118, 89, self._fmt(s.cpu_temp, ".0f", "C"), self.f(8), Theme.TEXT)
        d.text((23, 101), "LOAD", font=self.f(7), fill=Theme.MUTED)
        self._right(d, 118, 101, f"{cpu_u:.0f}%", self.f(8), Theme.TEXT)
        self._bar(d, (23, 113, 118, 117), cpu_u, Theme.TEXT)

        d.text((23, 149), "RAM", font=self.f(8), fill=Theme.TEXT)
        d.text((23, 162), f"{s.ram_used_gb:.1f}/{s.ram_total_gb:.0f}GB", font=self.f(8), fill=Theme.WHITE)
        self._right(d, 118, 175, f"{s.ram_percent:.0f}%", self.f(9), Theme.TEXT)
        self._bar(d, (23, 187, 118, 191), s.ram_percent, Theme.TEXT)

        d.text((23, 225), "CPU POWER", font=self.f(8), fill=Theme.TEXT)
        d.text((23, 239), self._fmt(s.cpu_power, ".0f", "W"), font=self.f(10), fill=Theme.WHITE)
        d.text((23, 255), "CLOCK", font=self.f(7), fill=Theme.MUTED)
        cpu_ghz = (s.cpu_clock_mhz / 1000.0) if s.cpu_clock_mhz else None
        self._right(d, 118, 255, self._fmt(cpu_ghz, ".2f", "G"), self.f(8), Theme.TEXT)
        self._spark(d, (23, 267, 118, 278), self.cpu_hist, Theme.TEXT)

        # Right column
        d.text((359, 75), self._short(s.gpu_name, 14), font=self.f(7), fill=Theme.WHITE)
        d.text((359, 89), "TEMP", font=self.f(7), fill=Theme.MUTED)
        self._right(d, 455, 89, self._fmt(s.gpu_temp, ".0f", "C"), self.f(8), Theme.TEXT)
        d.text((359, 101), "LOAD", font=self.f(7), fill=Theme.MUTED)
        self._right(d, 455, 101, "--" if s.gpu_usage is None else f"{gpu_u:.0f}%", self.f(8), Theme.TEXT)
        self._bar(d, (359, 113, 455, 117), gpu_u, Theme.TEXT)

        d.text((359, 149), "DOWN", font=self.f(7), fill=Theme.MUTED)
        self._right(d, 455, 149, f"{s.net_down_mbps:.1f}", self.f(8), Theme.TEXT)
        d.text((359, 161), "UP", font=self.f(7), fill=Theme.MUTED)
        self._right(d, 455, 161, f"{s.net_up_mbps:.1f}", self.f(8), Theme.TEXT)
        self._spark(d, (359, 175, 455, 188), self.net_hist, Theme.TEXT)

        d.text((359, 225), "DISK", font=self.f(8), fill=Theme.TEXT)
        d.text((359, 239), f"{s.disk_used_gb:.0f}/{s.disk_total_gb:.0f}GB", font=self.f(8), fill=Theme.WHITE)
        self._right(d, 455, 255, f"{s.disk_percent:.0f}%", self.f(9), Theme.TEXT)
        self._bar(d, (359, 267, 455, 271), s.disk_percent, Theme.TEXT)

        # Center display
        mission_time = s.timestamp.strftime("%H:%M:%S")
        font_big = self.f(24)
        w = self._text_width(d, mission_time, font_big)
        d.text((240 - w // 2, 74), mission_time, font=font_big, fill=Theme.TEXT)

        date = s.timestamp.strftime("%d %b %Y").upper()
        dw = self._text_width(d, date, self.f(8))
        d.text((240 - dw // 2, 101), date, font=self.f(8), fill=Theme.MUTED)

        d.text((162, 126), "CPU", font=self.f(8), fill=Theme.MUTED)
        d.text((207, 126), f"{cpu_u:.0f}%", font=self.f(10), fill=Theme.WHITE)
        d.text((255, 126), "GPU", font=self.f(8), fill=Theme.MUTED)
        d.text((301, 126), f"{gpu_u:.0f}%", font=self.f(10), fill=Theme.WHITE)

        d.text((162, 145), "VRAM", font=self.f(8), fill=Theme.MUTED)
        if s.gpu_vram_used_mb is not None and s.gpu_vram_total_mb:
            used_gb = s.gpu_vram_used_mb / 1024.0
            total_gb = s.gpu_vram_total_mb / 1024.0
            d.text((206, 145), f"{used_gb:.1f}/{total_gb:.0f}G", font=self.f(8), fill=Theme.WHITE)
        else:
            d.text((206, 145), "--", font=self.f(8), fill=Theme.WHITE)

        d.text((255, 145), "UPTIME", font=self.f(8), fill=Theme.MUTED)
        d.text((304, 145), self._uptime(s.uptime_seconds), font=self.f(8), fill=Theme.WHITE)

        d.text((162, 165), "VESSEL", font=self.f(8), fill=Theme.MUTED)
        d.text((208, 165), self._short(s.hostname if self.cfg.show_hostname else "VESSEL", 12), font=self.f(8), fill=Theme.WHITE)

        d.text((162, 184), "REALM", font=self.f(8), fill=Theme.MUTED)
        d.text((208, 184), self._short(s.os_name, 14), font=self.f(8), fill=Theme.WHITE)

        d.text((162, 203), "STATUS", font=self.f(8), fill=Theme.MUTED)
        d.text((208, 203), self._status(s), font=self.f(9), fill=Theme.TEXT)

        # Mission status box
        status = self._status(s)
        d.text((155, 245), "FLIGHT", font=self.f(8), fill=Theme.MUTED)
        d.text((202, 245), status, font=self.f(10), fill=Theme.WHITE)
        d.text((155, 261), "THERMAL", font=self.f(8), fill=Theme.MUTED)
        peak_temp = max([v for v in (s.cpu_temp, s.gpu_temp) if v is not None], default=0.0)
        d.text((211, 261), self._fmt(peak_temp, ".0f", "C"), font=self.f(9), fill=Theme.WHITE)
        d.text((252, 245), "FAN", font=self.f(8), fill=Theme.MUTED)
        if s.cpu_fan_rpm is not None:
            d.text((281, 245), f"{s.cpu_fan_rpm:.0f} RPM", font=self.f(8), fill=Theme.WHITE)
        elif s.gpu_fan_percent is not None:
            d.text((281, 245), f"GPU {s.gpu_fan_percent:.0f}%", font=self.f(8), fill=Theme.WHITE)
        else:
            d.text((281, 245), "--", font=self.f(8), fill=Theme.WHITE)

        d.text((252, 261), "POWER", font=self.f(8), fill=Theme.MUTED)
        total_power = (s.cpu_power or 0.0) + (s.gpu_power or 0.0)
        d.text((295, 261), self._fmt(total_power, ".0f", "W"), font=self.f(9), fill=Theme.WHITE)

        # Footer
        footer = f"GUID:{cpu_u:02.0f}  VIS:{gpu_u:02.0f}  STAT:{status}"
        d.text((58, 300), self._short(footer, 38), font=self.f(7), fill=Theme.TEXT)

        return im

    @classmethod
    def demo_snapshot(cls) -> Snapshot:
        return Snapshot(
            timestamp=datetime.now(),
            cpu_name="Intel Core i5-13600K",
            cpu_usage=36,
            cpu_temp=52,
            cpu_power=42.1,
            cpu_clock_mhz=4650,
            cpu_fan_rpm=1298,
            gpu_name="RTX 4070",
            gpu_usage=54,
            gpu_temp=54,
            gpu_power=78.3,
            gpu_clock_mhz=1770,
            gpu_vram_used_mb=2.1 * 1024,
            gpu_vram_total_mb=12 * 1024,
            gpu_fan_percent=42,
            ram_used_gb=10.2,
            ram_total_gb=32,
            ram_percent=32,
            disk_percent=36,
            disk_used_gb=346,
            disk_total_gb=953,
            net_down_mbps=12.4,
            net_up_mbps=2.7,
            uptime_seconds=4 * 3600 + 27 * 60,
            hostname="APOLLO-CORE",
            os_name="Windows 11 Pro",
        )


# ============================================================
# LCD DRIVER WRAPPER
# ============================================================


class TurzxDisplay:
    def __init__(self, cfg: Config, do_reset: bool = False):
        self.cfg = cfg
        self.lcd = None
        self.do_reset = do_reset

    def connect(self) -> None:
        try:
            from library.lcd.lcd_comm import Orientation
            from library.lcd.lcd_comm_rev_a import LcdCommRevA
        except ImportError as exc:
            raise RuntimeError(
                "Impossible d'importer TURZX-Dashboard.\n"
                "Place ce fichier dans la RACINE du dépôt TURZX-Dashboard "
                "(au même niveau que le dossier library)."
            ) from exc

        self.lcd = LcdCommRevA(
            com_port=self.cfg.com_port,
            display_width=320,
            display_height=480,
            update_queue=None,
        )

        if self.do_reset:
            print("[LCD] Reset matériel demandé...")
            self.lcd.Reset()

        # Mandatory according to upstream API.
        self.lcd.InitializeComm()

        # Rev A can get hot at high brightness. Clamp to 50%.
        safe_brightness = max(0, min(int(self.cfg.brightness), 50))
        if safe_brightness != self.cfg.brightness:
            print(f"[WARN] Luminosité limitée à {safe_brightness}% pour la Rev A")
        self.lcd.SetBrightness(level=safe_brightness)

        if self.cfg.orientation.lower() == "landscape":
            self.lcd.SetOrientation(orientation=Orientation.LANDSCAPE)
        else:
            self.lcd.SetOrientation(orientation=Orientation.PORTRAIT)

        print(f"[OK] TURZX connecté sur {self.cfg.com_port} - {self.lcd.get_width()}x{self.lcd.get_height()}")

    def full_frame(self, image: Image.Image) -> None:
        if self.lcd is None:
            raise RuntimeError("LCD non connecté")
        self.lcd.DisplayPILImage(image, 0, 0)

    def patch(self, image: Image.Image, x: int, y: int) -> None:
        if self.lcd is None:
            raise RuntimeError("LCD non connecté")
        self.lcd.DisplayPILImage(image, x, y)

    def close(self) -> None:
        if self.lcd is not None:
            try:
                self.lcd.closeSerial()
            except Exception:
                pass


# ============================================================
# PARTIAL UPDATE ENGINE
# ============================================================


def changed_patch(previous: Image.Image, current: Image.Image, zone, padding: int = 2):
    x0, y0, x1, y1 = zone
    old = previous.crop(zone)
    new = current.crop(zone)
    diff = ImageChops.difference(old, new)
    bbox = diff.getbbox()
    if bbox is None:
        return None

    bx0, by0, bx1, by1 = bbox
    bx0 = max(0, bx0 - padding)
    by0 = max(0, by0 - padding)
    bx1 = min(x1 - x0, bx1 + padding)
    by1 = min(y1 - y0, by1 + padding)

    abs_box = (x0 + bx0, y0 + by0, x0 + bx1, y0 + by1)
    patch = current.crop(abs_box)
    return patch, abs_box[0], abs_box[1]


# ============================================================
# APP
# ============================================================


def run_preview(cfg: Config, use_live_sensors: bool = False) -> Path:
    renderer = DashboardRenderer(cfg)
    reader = None
    try:
        if use_live_sensors:
            reader = SensorReader(cfg)
            time.sleep(0.15)
            snap = reader.read()
        else:
            snap = renderer.demo_snapshot()
        image = renderer.render(snap)
        out = APP_DIR / cfg.preview_file
        image.save(out)
        print(f"[OK] Preview générée: {out}")
        return out
    finally:
        if reader:
            reader.close()


def run_dashboard(cfg: Config, do_reset: bool = False) -> None:
    reader = SensorReader(cfg)
    renderer = DashboardRenderer(cfg)
    display = TurzxDisplay(cfg, do_reset=do_reset)

    try:
        display.connect()

        # First frame: complete screen.
        first = reader.read()
        previous = renderer.render(first)
        print("[LCD] Envoi du fond complet...")
        display.full_frame(previous)
        print("[OK] Dashboard actif. Ctrl+C pour quitter.")

        next_tick = time.monotonic() + cfg.refresh_seconds
        while True:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(0.05, next_tick - now))
                continue
            next_tick = now + max(0.25, float(cfg.refresh_seconds))

            snap = reader.read()
            current = renderer.render(snap)

            updates = 0
            for zone in renderer.DYNAMIC_ZONES:
                result = changed_patch(previous, current, zone)
                if result is None:
                    continue
                patch, x, y = result
                # Skip pathological empty patches.
                if patch.width <= 0 or patch.height <= 0:
                    continue
                display.patch(patch, x, y)
                updates += 1

            previous = current
            print(
                f"\rCPU {snap.cpu_usage:5.1f}% | "
                f"GPU {('--' if snap.gpu_usage is None else f'{snap.gpu_usage:4.0f}%'):>4} | "
                f"RAM {snap.ram_percent:4.0f}% | patches {updates}",
                end="",
                flush=True,
            )

    except KeyboardInterrupt:
        print("\n[STOP] Arrêt demandé")
    finally:
        display.close()
        reader.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Apollo Core dashboard for TURZX/UsbPCMonitor 3.5 Rev A")
    parser.add_argument("--preview", action="store_true", help="Génère preview_apollo.png sans utiliser le LCD")
    parser.add_argument("--live-preview", action="store_true", help="Preview avec les capteurs de ce PC")
    parser.add_argument("--reset", action="store_true", help="Reset matériel du LCD avant démarrage (plus lent)")
    parser.add_argument("--com", default=None, help="COM manuel, ex: COM5. Sinon AUTO/config.json")
    parser.add_argument("--brightness", type=int, default=None, help="Luminosité 0-50 recommandée Rev A")
    parser.add_argument("--refresh", type=float, default=None, help="Rafraîchissement en secondes, ex: 1.0")
    parser.add_argument("--background", default=None, help="Image de fond démoniaque, ex: demon_background.png")
    parser.add_argument("--write-config", action="store_true", help="Écrit un config.json d'exemple puis quitte")
    parser.add_argument("--debug-cpu", action="store_true", help="Affiche une lecture CPU LHM puis continue")
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = Config.load()

    if args.com:
        cfg.com_port = args.com
    if args.brightness is not None:
        cfg.brightness = args.brightness
    if args.refresh is not None:
        cfg.refresh_seconds = max(0.25, args.refresh)
    if args.background:
        cfg.background_file = args.background

    if args.write_config:
        cfg.save()
        print(f"[OK] Configuration écrite: {CONFIG_FILE}")
        return

    if args.preview or args.live_preview:
        run_preview(cfg, use_live_sensors=args.live_preview)
        return

    run_dashboard(cfg, do_reset=args.reset)


if __name__ == "__main__":
    main()

