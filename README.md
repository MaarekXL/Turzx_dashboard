# TURZX Dashboard

A lightweight **480×320 cyber-style hardware monitor** for the **UsbPCMonitor / TURZX-style 3.5" display (Hardware Revision A)**.

This project is intentionally small: it keeps only the parts required to drive the 3.5" serial display and collect system telemetry on Windows.

> [!IMPORTANT]
> This project is **not affiliated with TURZX, Turing, XuanFang, UsbPCMonitor, or their manufacturers/sellers**.

## Preview

The dashboard is designed for a 3.5" display in landscape mode:

- CPU usage, package temperature, package power and clock
- NVIDIA GPU usage, temperature, power and clock
- RAM and VRAM usage
- Network throughput
- Storage usage
- Hostname, Windows version and uptime
- CPU fan speed when exposed by LibreHardwareMonitor
- Live centered clock
- Partial LCD updates to reduce unnecessary serial traffic

## Hardware tested

- **Display:** UsbPCMonitor 3.5"
- **Hardware revision:** Rev A / `USBMONITOR_3_5`
- **Native resolution:** 320×480
- **Dashboard orientation:** 480×320 landscape
- **Connection:** USB serial / COM port

The display port can be detected automatically or specified manually.

## Requirements

Windows and Python 3.10+ are recommended.

```text
pyserial~=3.5
psutil~=7.2.2
Pillow~=12.3.0
numpy~=2.4.4
pythonnet~=3.0.5
nvidia-ml-py
```

NVIDIA telemetry is read through NVML.

CPU temperature, package power and fan telemetry are read through **LibreHardwareMonitor**. On Windows, running the program with administrator privileges may be required for hardware sensor access.

## Installation

Clone the repository and create a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Close the original `UsbPCMonitor.exe` application before starting the dashboard so it does not keep the COM port open.

## Usage

Automatic COM-port detection:

```powershell
python turzx_dashboard.py
```

Specify the display port manually:

```powershell
python turzx_dashboard.py --com COM3
```

Generate a preview without writing to the LCD:

```powershell
python turzx_dashboard.py --preview
```

Generate a preview using live system sensors:

```powershell
python turzx_dashboard.py --live-preview
```

Set brightness:

```powershell
python turzx_dashboard.py --brightness 30
```

Set refresh period:

```powershell
python turzx_dashboard.py --refresh 1.0
```

## Project structure

```text
TURZX-Dashboard/
├── external/
│   ├── LibreHardwareMonitor/
│   └── PawnIO/
├── library/
│   ├── lcd/
│   │   ├── color.py
│   │   ├── lcd_comm.py
│   │   ├── lcd_comm_rev_a.py
│   │   └── serialize.py
│   ├── LICENSE
│   └── log.py
├── .gitignore
├── LICENSE
├── requirements.txt
└── turzx_dashboard.py
```

## Design

The dashboard is rendered directly with Pillow at **480×320**.

The LCD is not used as a Windows secondary monitor. Frames are sent through the display protocol over USB serial.

After the initial full frame, the program compares dynamic regions against the previous frame and only sends changed image patches. This keeps USB/serial traffic lower than continuously transmitting the complete screen.

## Credits and upstream project

This project is based on and reuses portions of:

**[turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python)**

Original project by **Matthieu Houdebine ([@mathoudebine](https://github.com/mathoudebine)) and contributors**.

`turing-smart-screen-python` provides the open-source display communication layer and hardware abstraction used as the foundation for this project.

This repository is a specialized, stripped-down dashboard for the **UsbPCMonitor 3.5" Rev A** and is not intended to replace the full upstream project.

For support for other display revisions, models, themes, operating systems and upstream functionality, use the original project.

## Third-party components

This repository may include components from:

- `turing-smart-screen-python`
- LibreHardwareMonitor
- HidSharp / related LibreHardwareMonitor dependencies
- PawnIO

Their respective notices and licenses must be preserved where applicable.

## License

The portions derived from **turing-smart-screen-python** remain subject to its **GNU General Public License v3.0** terms.

See [`LICENSE`](LICENSE) and the retained upstream license notices for details.

## Acknowledgements

Thanks to **Matthieu Houdebine and all contributors to turing-smart-screen-python** for reverse-engineering, maintaining and documenting support for these inexpensive USB smart displays.
