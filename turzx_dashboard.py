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
    preview_file: str = "preview.png"
    use_lhm_direct: bool = True
    show_hostname: bool = True
    accent: tuple[int, int, int] = (0, 255, 118)
    accent2: tuple[int, int, int] = (0, 220, 255)

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
# RENDERER
# ============================================================


class Theme:
    BG = (4, 8, 10)
    PANEL = (7, 14, 16)
    PANEL2 = (5, 11, 13)
    GRID = (12, 31, 31)
    TEXT = (224, 237, 236)
    MUTED = (108, 134, 132)
    DIM = (38, 61, 60)
    GOOD = (0, 255, 118)
    CYAN = (0, 220, 255)
    WARN = (255, 196, 64)
    HOT = (255, 80, 80)


class DashboardRenderer:
    WIDTH = 480
    HEIGHT = 320

    # Zones where dynamic pixels live. We compare each zone and only send its
    # changed bounding box to the LCD.
    DYNAMIC_ZONES = [
        (0, 0, 480, 32),       # header + centered clock
        (8, 36, 238, 131),     # CPU panel (crop end is exclusive)
        (243, 36, 473, 131),   # GPU panel
        (8, 136, 238, 185),    # RAM panel
        (243, 136, 473, 185),  # VRAM panel
        (8, 192, 158, 285),    # NET panel
        (165, 192, 315, 285),  # STORAGE panel
        (322, 192, 473, 285),  # SYSTEM panel
        (0, 284, 480, 320),    # footer
    ]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.accent = tuple(cfg.accent)
        self.accent2 = tuple(cfg.accent2)
        self.cpu_hist = deque([0.0] * 36, maxlen=36)
        self.gpu_hist = deque([0.0] * 36, maxlen=36)
        self.net_hist = deque([0.0] * 36, maxlen=36)
        self.fonts = self._load_fonts()

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
        for size in [9, 10, 11, 12, 13, 14, 16, 18, 23, 28, 32]:
            if path:
                result[size] = ImageFont.truetype(str(path), size)
            else:
                result[size] = ImageFont.load_default()
        return result

    def f(self, size: int):
        return self.fonts[size]

    @staticmethod
    def _short_name(name: str, max_chars: int = 18) -> str:
        cleaned = name.replace("NVIDIA GeForce ", "").replace("Intel(R) Core(TM) ", "")
        cleaned = cleaned.replace("Processor", "").strip()
        return cleaned if len(cleaned) <= max_chars else cleaned[: max_chars - 1] + "…"

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
            return f"{days}d {hours:02d}h"
        return f"{hours:02d}h {minutes:02d}m"

    def _panel(self, d: ImageDraw.ImageDraw, box, title: str, accent=None):
        accent = accent or self.accent
        x0, y0, x1, y1 = box
        d.rounded_rectangle(box, radius=5, fill=Theme.PANEL, outline=accent, width=1)
        # small cyber corners
        d.line((x0 + 1, y0 + 10, x0 + 1, y0 + 2, x0 + 9, y0 + 2), fill=accent)
        d.line((x1 - 9, y1 - 2, x1 - 1, y1 - 2, x1 - 1, y1 - 10), fill=accent)
        d.text((x0 + 8, y0 + 5), title, font=self.f(11), fill=Theme.MUTED)

    def _bar(self, d: ImageDraw.ImageDraw, box, value: float, accent=None):
        accent = accent or self.accent
        x0, y0, x1, y1 = box
        value = max(0.0, min(float(value), 100.0))
        d.rectangle(box, fill=Theme.DIM)
        fill_x = x0 + int((x1 - x0) * value / 100.0)
        if fill_x > x0:
            d.rectangle((x0, y0, fill_x, y1), fill=accent)
        # segmentation gives the visual style of the concept image
        for x in range(x0 + 12, x1, 13):
            d.line((x, y0, x, y1), fill=Theme.PANEL)

    def _sparkline(self, d: ImageDraw.ImageDraw, box, values, max_value=100.0, accent=None):
        accent = accent or self.accent
        x0, y0, x1, y1 = box
        d.rectangle(box, fill=Theme.PANEL2, outline=Theme.DIM)
        vals = list(values)
        if len(vals) < 2:
            return
        w = max(1, x1 - x0 - 2)
        h = max(1, y1 - y0 - 2)
        pts = []
        for i, v in enumerate(vals):
            px = x0 + 1 + int(i * w / (len(vals) - 1))
            py = y1 - 1 - int(max(0, min(v, max_value)) / max_value * h)
            pts.append((px, py))
        d.line(pts, fill=accent, width=1)

    def _temp_color(
        self,
        temp: Optional[float],
        normal_color: Optional[tuple[int, int, int]] = None,
    ) -> tuple[int, int, int]:
        if temp is None:
            return Theme.TEXT
        if temp >= 85:
            return Theme.HOT
        if temp >= 75:
            return Theme.WARN
        return normal_color or self.accent

    def render(self, s: Snapshot) -> Image.Image:
        cpu_u = max(0.0, min(s.cpu_usage, 100.0))
        gpu_u = max(0.0, min(s.gpu_usage or 0.0, 100.0))
        self.cpu_hist.append(cpu_u)
        self.gpu_hist.append(gpu_u)
        self.net_hist.append(min((s.net_down_mbps + s.net_up_mbps) * 4.0, 100.0))

        im = Image.new("RGB", (self.WIDTH, self.HEIGHT), Theme.BG)
        d = ImageDraw.Draw(im)

        # Subtle cyber grid, intentionally dark to remain readable on IPS.
        for x in range(0, self.WIDTH, 16):
            d.line((x, 0, x, self.HEIGHT), fill=(5, 18, 18))
        for y in range(0, self.HEIGHT, 16):
            d.line((0, y, self.WIDTH, y), fill=(5, 18, 18))

        # Header
        header_font = self.f(14)
        d.text((10, 7), "// SYSTEM DASHBOARD", font=header_font, fill=Theme.TEXT)

        # Horloge centrée horizontalement, à la même hauteur et dans le même style
        # que le titre SYSTEM DASHBOARD.
        clock_text = s.timestamp.strftime("%H:%M:%S")
        clock_bbox = d.textbbox((0, 0), clock_text, font=header_font)
        clock_w = clock_bbox[2] - clock_bbox[0]
        clock_x = (self.WIDTH - clock_w) // 2
        d.text((clock_x, 7), clock_text, font=header_font, fill=Theme.TEXT)

        d.line((10, 27, 345, 27), fill=self.accent)
        d.line((345, 27, 355, 17, 470, 17), fill=self.accent)

        # Main panels
        self._panel(d, (8, 36, 237, 130), "CPU", self.accent)
        self._panel(d, (243, 36, 472, 130), "GPU", self.accent2)
        self._panel(d, (8, 136, 237, 184), "RAM", self.accent)
        self._panel(d, (243, 136, 472, 184), "VRAM", self.accent2)
        self._panel(d, (8, 192, 157, 284), "NET", self.accent)
        self._panel(d, (165, 192, 314, 284), "STORAGE", self.accent2)
        self._panel(d, (322, 192, 472, 284), "SYSTEM", self.accent)

        # CPU panel
        d.text((50, 40), self._short_name(s.cpu_name, 22), font=self.f(11), fill=self.accent)
        cpu_temp = self._fmt(s.cpu_temp, ".0f", "°C")
        d.text((15, 59), cpu_temp, font=self.f(28), fill=self._temp_color(s.cpu_temp, self.accent))
        d.text((135, 62), f"{cpu_u:4.0f}%", font=self.f(23), fill=self.accent)
        cpu_power = self._fmt(s.cpu_power, ".0f", " W")
        cpu_clock = self._fmt((s.cpu_clock_mhz / 1000.0) if s.cpu_clock_mhz else None, ".2f", " GHz")
        d.text((16, 94), cpu_power, font=self.f(11), fill=Theme.TEXT)
        d.text((91, 94), cpu_clock, font=self.f(11), fill=Theme.MUTED)
        self._bar(d, (15, 112, 118, 121), cpu_u, self.accent)
        self._sparkline(d, (126, 105, 228, 122), self.cpu_hist, accent=self.accent)

        # GPU panel
        d.text((285, 40), self._short_name(s.gpu_name, 22), font=self.f(11), fill=self.accent2)
        gpu_temp = self._fmt(s.gpu_temp, ".0f", "°C")
        d.text((250, 59), gpu_temp, font=self.f(28), fill=self._temp_color(s.gpu_temp, self.accent2))
        gpu_usage_text = "--" if s.gpu_usage is None else f"{s.gpu_usage:4.0f}%"
        d.text((370, 62), gpu_usage_text, font=self.f(23), fill=self.accent2)
        gpu_power = self._fmt(s.gpu_power, ".0f", " W")
        gpu_clock = self._fmt(s.gpu_clock_mhz, ".0f", " MHz")
        d.text((251, 94), gpu_power, font=self.f(11), fill=Theme.TEXT)
        d.text((327, 94), gpu_clock, font=self.f(11), fill=Theme.MUTED)
        self._bar(d, (250, 112, 353, 121), gpu_u, self.accent2)
        self._sparkline(d, (361, 105, 463, 122), self.gpu_hist, accent=self.accent2)

        # RAM
        d.text((16, 150), f"{s.ram_used_gb:.1f} / {s.ram_total_gb:.0f} GB", font=self.f(14), fill=Theme.TEXT)
        d.text((190, 150), f"{s.ram_percent:.0f}%", font=self.f(13), fill=self.accent)
        self._bar(d, (16, 171, 228, 178), s.ram_percent, self.accent)

        # VRAM
        if s.gpu_vram_used_mb is not None and s.gpu_vram_total_mb:
            used_gb = s.gpu_vram_used_mb / 1024.0
            total_gb = s.gpu_vram_total_mb / 1024.0
            vram_pct = 100.0 * s.gpu_vram_used_mb / s.gpu_vram_total_mb
            vram_text = f"{used_gb:.1f} / {total_gb:.0f} GB"
        else:
            vram_pct = 0.0
            vram_text = "-- / -- GB"
        d.text((251, 150), vram_text, font=self.f(14), fill=Theme.TEXT)
        d.text((425, 150), f"{vram_pct:.0f}%" if s.gpu_vram_total_mb else "--", font=self.f(13), fill=self.accent2)
        self._bar(d, (251, 171, 463, 178), vram_pct, self.accent2)

        # Network panel
        d.text((16, 214), "↓", font=self.f(16), fill=self.accent)
        d.text((35, 215), f"{s.net_down_mbps:5.1f} MB/s", font=self.f(11), fill=Theme.TEXT)
        d.text((16, 235), "↑", font=self.f(16), fill=self.accent2)
        d.text((35, 236), f"{s.net_up_mbps:5.1f} MB/s", font=self.f(11), fill=Theme.TEXT)
        self._sparkline(d, (16, 259, 148, 276), self.net_hist, accent=self.accent)

        # Storage panel
        d.text((173, 215), f"{s.disk_percent:.0f}% USED", font=self.f(18), fill=self.accent2)
        d.text((173, 242), f"{s.disk_used_gb:.0f}/{s.disk_total_gb:.0f} GB", font=self.f(11), fill=Theme.TEXT)
        self._bar(d, (173, 264, 305, 276), s.disk_percent, self.accent2)

        # System panel
        hostname = s.hostname[:18] if self.cfg.show_hostname else "PC ONLINE"
        d.text((330, 214), hostname, font=self.f(12), fill=self.accent)
        d.text((330, 235), s.os_name[:18], font=self.f(10), fill=Theme.MUTED)
        d.text((330, 251), f"UP {self._uptime(s.uptime_seconds)}", font=self.f(10), fill=Theme.TEXT)
        if s.cpu_fan_rpm is not None:
            fan_text = f"CPU FAN {s.cpu_fan_rpm:.0f} RPM"
        elif s.gpu_fan_percent is not None:
            fan_text = f"GPU FAN {s.gpu_fan_percent:.0f}%"
        else:
            fan_text = "FAN --"
        d.text((330, 267), fan_text[:19], font=self.f(10), fill=self.accent2)

        # Footer / terminal style
        d.text((10, 292), "C:\\>", font=self.f(11), fill=self.accent)
        d.text((43, 292), "STAY COOL // STAY FOCUSED", font=self.f(10), fill=Theme.MUTED)

        date_text = s.timestamp.strftime("%d/%m/%y")
        date_font = self.f(10)
        date_bbox = d.textbbox((0, 0), date_text, font=date_font)
        date_w = date_bbox[2] - date_bbox[0]
        d.text((470 - date_w, 292), date_text, font=date_font, fill=Theme.TEXT)

        d.line((10, 311, 470, 311), fill=self.accent)

        return im

    @classmethod
    def demo_snapshot(cls) -> Snapshot:
        return Snapshot(
            timestamp=datetime.now(),
            cpu_name="Intel Core i5-13600K",
            cpu_usage=36,
            cpu_temp=47,
            cpu_power=64,
            cpu_clock_mhz=3900,
            cpu_fan_rpm=928,
            gpu_name="RTX 4070",
            gpu_usage=82,
            gpu_temp=61,
            gpu_power=186,
            gpu_clock_mhz=2715,
            gpu_vram_used_mb=7.2 * 1024,
            gpu_vram_total_mb=12 * 1024,
            gpu_fan_percent=42,
            ram_used_gb=18.4,
            ram_total_gb=32,
            ram_percent=57,
            disk_percent=41,
            disk_used_gb=820,
            disk_total_gb=2000,
            net_down_mbps=42.0,
            net_up_mbps=3.1,
            uptime_seconds=2 * 3600 + 37 * 60,
            hostname="TECHMASTER",
            os_name="Windows 11",
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
                "(au même niveau que main.py et le dossier library)."
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
    parser = argparse.ArgumentParser(description="Cyber dashboard for TURZX/UsbPCMonitor 3.5 Rev A")
    parser.add_argument("--preview", action="store_true", help="Génère preview.png sans utiliser le LCD")
    parser.add_argument("--live-preview", action="store_true", help="Preview avec les capteurs de ce PC")
    parser.add_argument("--reset", action="store_true", help="Reset matériel du LCD avant démarrage (plus lent)")
    parser.add_argument("--com", default=None, help="COM manuel, ex: COM5. Sinon AUTO/config.json")
    parser.add_argument("--brightness", type=int, default=None, help="Luminosité 0-50 recommandée Rev A")
    parser.add_argument("--refresh", type=float, default=None, help="Rafraîchissement en secondes, ex: 1.0")
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
