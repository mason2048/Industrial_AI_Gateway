# 从源码运行

以下方式用于开发或需要 Windows 服务的安装。普通 Windows 用户优先下载 README 中的 EXE 发行包。

## 开发版桌面入口

源码仓库不携带现场 `config/config.json` 或数据库。先创建环境，再运行与 EXE 共用的桌面入口，会在独立 `gateway-data` 中初始化安全模拟配置与 6 点样本：

```sh
python3.12 -m scripts.bootstrap init
.venv/bin/python desktop.py
```

Windows 使用 `py -3.12 -m scripts.bootstrap init` 和 `.venv\Scripts\python.exe desktop.py`。该入口不会预填历史。需要固定安装根目录时添加 `--root`。

以下传统 `launch.py` / start 脚本用于已经准备好配置的安装或服务部署。首次真实现场安装步骤见 [Windows 源码安装](WINDOWS_SOURCE_INSTALL.md)。

## 环境与启动

需要 **Python 3.12**。首次安装依赖需要联网，之后主界面和采集可离线使用；运行程序不需要 Node.js 或 npm。

Windows：

```powershell
install.bat
check.bat
start.bat
start.bat --no-browser
start.bat --check
test.bat -q
```

macOS，在项目目录执行：

```sh
./start.command
./start.command --no-browser
./start.command --check
./test.command -q
```

已有环境直接使用：

```sh
.venv/bin/python launch.py --no-browser
```

默认地址为 http://127.0.0.1:8080，仅监听回环地址。可用 `--port 8081` 选择其他本机端口。停止前台运行按 Ctrl+C；后台实例使用 `stop.bat` 或 `python -m scripts.manage stop`。停止命令验证本目录的运行锁与随机令牌，不按 PID 杀进程；超过停机期限会报告未完成。

启动器根据依赖锁文件的 SHA-256 决定是否重新安装；安装或依赖检查失败不会写成功标记。不要搬运其他机器上的 `.venv`，应在新机器重新创建。

从 V1.0 升级前请先停止旧版程序；旧版没有 V1.1 的运行锁，不能与新版共用同一个数据目录同时运行。

### 演示与现场初始化

普通启动**不生成演示历史**。现有目录包含原版模拟数据，它们升级后归入 `legacy` 来源，可在历史变量目录中查询。

仅在需要新建演示安装时使用：

```sh
python launch.py --root /absolute/path/to/demo --demo-init --no-browser
```

该目录需要有本项目 `frontend` 文件夹；默认在项目目录运行 `--demo-init` 也可以。演示初始化只补齐缺失模板、配置和全新演示数据库；已有数据库不会被补种或覆盖。新建演示历史关联当前连接标识。

现场部署准备 `config/config.json` 和真实点表。没有点表时可启动界面导入，但就绪检查不会通过。缺少配置时启动失败并提示配置，不自动切换演示。
