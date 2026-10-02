"""Windows SCM adapter. Install using .venv Python and an elevated terminal.

python -m scripts.bootstrap service-init
python -m backend.windows_service install
python -m backend.windows_service start|stop|remove
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    if os.name != "nt":
        raise SystemExit("Windows services are only available on Windows.")
    from launch import check_python, run_gateway
    check_python()
    try:
        import servicemanager
        import win32service
        import win32serviceutil
    except ImportError as exc:
        raise SystemExit("Run python -m scripts.bootstrap service-init before installing a service.") from exc

    class GatewayService(win32serviceutil.ServiceFramework):
        _svc_name_ = "IndustrialAIGateway"
        _svc_display_name_ = "Industrial AI Gateway (Read Only)"
        _svc_description_ = "Single-PLC read-only data acquisition and local history."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = threading.Event()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=35000)
            self.stop_event.set()

        def SvcShutdown(self):
            self.SvcStop()

        def SvcDoRun(self):
            os.chdir(ROOT)
            servicemanager.LogInfoMsg("Industrial AI Gateway starting")
            try:
                run_gateway(ROOT, no_browser=True, stop_event=self.stop_event)
            except BaseException as exc:
                # Exception text may include connection credentials. Keep the SCM
                # event generic; application diagnostics use the redacting logger.
                servicemanager.LogErrorMsg(f"Gateway stopped after {type(exc).__name__}; inspect the local gateway logs.")
                raise

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--service-host"]:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(GatewayService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    if len(arguments) != 1 or arguments[0] not in ("install", "start", "stop", "remove", "status"):
        raise SystemExit("Usage: python -m backend.windows_service install|start|stop|remove|status")
    command = arguments[0]
    name = GatewayService._svc_name_
    if command == "install":
        script = str(Path(__file__).resolve())
        win32serviceutil.InstallService(
            pythonClassString=None, serviceName=name,
            displayName=GatewayService._svc_display_name_, description=GatewayService._svc_description_,
            exeName=sys.executable, exeArgs=f'"{script}" --service-host',
            startType=win32service.SERVICE_AUTO_START)
        # Apply bounded recovery delays; a clean SCM stop never triggers recovery.
        subprocess.run(["sc.exe", "failure", name, "reset=", "86400", "actions=", "restart/5000/restart/15000/restart/30000"], check=True)
        subprocess.run(["sc.exe", "failureflag", name, "1"], check=True)
        print("Service installed with automatic startup and crash recovery.")
    elif command == "start":
        win32serviceutil.StartService(name)
        win32serviceutil.WaitForServiceStatus(name, win32service.SERVICE_RUNNING, waitSecs=35)
        print("Gateway service is running. Open http://127.0.0.1:8080 on this computer.")
    elif command == "stop":
        win32serviceutil.StopServiceWithDeps(name, waitSecs=35)
        print("Gateway service stopped.")
    elif command == "remove":
        status = win32serviceutil.QueryServiceStatus(name)[1]
        if status != win32service.SERVICE_STOPPED:
            win32serviceutil.StopServiceWithDeps(name, waitSecs=35)
        win32serviceutil.RemoveService(name)
        print("Gateway service removed. Configuration and history are retained.")
    else:
        state = win32serviceutil.QueryServiceStatus(name)[1]
        names = {win32service.SERVICE_STOPPED: "STOPPED", win32service.SERVICE_RUNNING: "RUNNING",
                 win32service.SERVICE_START_PENDING: "START_PENDING", win32service.SERVICE_STOP_PENDING: "STOP_PENDING",
                 win32service.SERVICE_PAUSED: "PAUSED", win32service.SERVICE_PAUSE_PENDING: "PAUSE_PENDING",
                 win32service.SERVICE_CONTINUE_PENDING: "CONTINUE_PENDING"}
        print(f"{name}: {names.get(state, state)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
