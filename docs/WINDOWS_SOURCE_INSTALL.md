# Windows 源码安装与服务

适用对象：在 Windows 10 / 11、64 位 Python 3.12 环境中部署本机工业数据网关。运行软件不需要 Node.js、npm 或 Excel 安装；编辑点表可使用 Excel 或兼容的表格软件。

本文用于源码安装和 Windows 服务。普通用户请下载 EXE 便携版，见 [Windows 部署说明](WINDOWS_DEPLOYMENT.md)。Windows 服务和真实 PLC 仍需现场验收。

本文命令块按 Windows 命令提示符（`cmd.exe`）编写。如果使用 PowerShell，请在本目录脚本名前加 `./`，例如 `./install.bat`、`./service.bat start`。

## 一、第一次部署

1. 安装 64 位 **Python 3.12**，安装时启用 Python 启动器；也可勾选 `Add python.exe to PATH`。安装来源：[Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)。安装后重新打开终端。
2. 解压发行 ZIP 到本机可写目录，例如 `C:\Industrial_AI_Gateway`。从 ZIP 解压后运行，勿在压缩文件预览里运行。建议不要放在 `Program Files`、同步盘或网络共享中。
3. 在项目目录运行 `install.bat`。它建立本目录 `.venv`，按锁定版本安装依赖并检查依赖关系；第一次需要联网。安装失败时不会标记成功，修复网络后可重复运行。
4. 运行 `check.bat`。它检查 Python、依赖与本地配置，不初始化数据库，不连接 PLC。检查通过说明软件安装具备启动条件。
5. 运行 `start.bat`。浏览器自动打开 [本机页面](http://127.0.0.1:8080)。终端窗口应保持打开；前台运行时按 `Ctrl+C` 停止，也可另外运行 `stop.bat`。

双击脚本发生错误时会停留显示错误；自动化执行可先设置环境变量 `GATEWAY_NO_PAUSE=1` 关闭错误后的按键等待。已有 `.venv` 会优先使用；不要复制其他电脑的 `.venv`，每台电脑应独立安装。

发行包提供全新 OPC UA 配置和空 `data` 目录，**没有现场点位、历史数据库、管理口令或证书**。服务器地址 `opc.tcp://127.0.0.1:4840` 是占位值。软件第一次启动会生成本机数据库和管理口令；没有连接实际服务器或导入点位前，界面提示未就绪属于正常状态。

## 二、在界面完成基础配置

1. 用记事本打开 `data\operator_pin.txt`，复制管理口令。在页面的管理口令框填写；口令是本机生成的，发行包没有统一默认口令。
2. 打开 **PLC连接**，选择“真实 PLC · OPC UA 只读”，填写 OPC UA 服务器地址。这里可设置读取周期、每批读取点数、默认定时保存间隔和最近几天历史。默认读取周期 1 秒、每批 100 点、保存间隔 1800 秒、保留最近 7 天。
3. 若服务器有账号或安全策略，在“证书与账号配置”填写用户名、密码环境变量名称、证书及私钥绝对路径。程序读取密码环境变量，不在网页里填写密码。客户端证书需要得到服务器信任。
4. 在 **点位管理** 下载空 Excel 模板，或者使用发行包 `templates\PLC点位导入模板.xlsx`。填好后导入，先核对差异预览，再确认应用。导入是整表替换；修改已有配置应先导出当前点表。
5. 回到连接页测试连接，校验点表。保存连接后，在 **实时监控** 检查各点数值、采集时间和质量；在 **运行诊断** 查看就绪状态和异常信息。

点表最多 1000 点。支持 BOOL、WORD（UInt16）、DWORD（UInt32）、FLOAT（Float32）；必须填写服务器实际发布的 `NodeId`，例如 `ns=2;s=Pressure`。PLC 地址 `DB1.DBD20` 是工程地址说明，无法直接替代 OPC UA NodeId。

点位可逐个设置：名称、设备、单位、AI描述、是否保存历史、是否额外记录变化、变化阈值、定时保存间隔、小数位数。定时保存间隔留空时继承连接页默认值；保存关闭的点仅提供当前值。新模板中的“记录变化”设置为 `NO` 时按定时策略记录，设置为 `YES` 时额外记录超过阈值的变化和 BOOL 状态变化；阈值比较相对上次已保存基准。默认显示 5 位小数，显示位数不增加传感器或 OPC UA 数据源实际精度。

当前版本对真实 PLC 始终只读，点表 `WRITE` 和 AI描述都不能解除写入限制。服务器端心跳握手若要求客户端周期写入，需要另行实现并验收；连接页设置的是 OPC UA 连接地址及轮询读取方式。读取方式当前为周期轮询，不含 OPC UA 订阅切换。

“按需数据查询”只在用户提交问题时调用当前数据或历史，本版提供本地规则统计及只读数据接口。AI 历史接口默认仅返回变化事件，可用 `changed_only=false` 获取全部保存样本。外部大模型连接、云端上传和 PLC 控制尚未包含在此次基础版部署中。

## 三、Windows 后台服务与开机启动（可选）

先完成前台连接和点位验收，然后运行 `stop.bat` 停止前台。服务与前台不能同时使用同一目录。

右键以管理员身份打开终端，切换到项目目录后执行：

```bat
service.bat install
service.bat start
service.bat status
```

服务名为 `IndustrialAIGateway`，安装时设置自动启动和 5 / 15 / 30 秒故障恢复。也可在 Windows“服务”管理器中找到它。后台服务不会自动打开浏览器；在同一台电脑打开 `http://127.0.0.1:8080` 即可。

```bat
service.bat stop
service.bat remove
```

移除服务会保留现场配置和历史。安装/启动/停止/移除服务需要管理员权限；`status` 用于查看 Windows 服务状态，它不能证明 PLC 数据正常，仍需检查网页实时值与运行诊断。

默认服务账户是 LocalSystem。现场应配置适合的服务账户及目录写权限、证书读取权限。PLC 密码环境变量必须对运行服务的账户可见；只在个人终端中设置的临时变量不会传给 Windows 服务。修改系统环境变量后，现有 Windows 服务管理器可能仍保留旧环境，需要按现场维护流程安排重启或重新配置服务账户。项目路径变化后需停止、移除旧服务，在新路径重新安装。

服务默认使用本机 8080 端口。前台可用 `start.bat --port 8081` 改端口；当前服务入口没有端口设置项，安装服务前请保证 8080 可用。软件仅允许本机访问，其他电脑无法通过局域网打开该页面。

## 四、没有互联网的现场

先在联网的 Windows x64 机器安装 Python 3.12，并解压同一个版本的软件。运行：

```bat
prepare-offline.bat
```

如果现场需要后台服务：

```bat
prepare-offline.bat --with-service
```

工具根据锁文件下载依赖并构建完整 wheels，默认输出到 `wheels` 目录。将软件目录、`wheels` 目录和 Python 3.12 的离线安装程序一起带到现场。联网准备与现场机器须使用相同的 Windows x64 / Python 3.12 平台；不要将 macOS 依赖包复制给 Windows。

在现场安装 Python 后：

```bat
install.bat --offline --wheelhouse wheels
check.bat
start.bat
```

安装服务时在管理员终端运行：

```bat
service.bat install --offline --wheelhouse wheels
service.bat start
```

`--offline` 禁用在线包索引，缺包或版本不符会明确失败。依赖安装成功后正常启动不重复安装。更新软件并改变依赖锁文件时，应重新准备匹配版本的 wheels。

## 五、停止、备份与升级

运行时软件默认每天自动备份一次，保留最近 7 份，备份位于 `data\backups`。手工执行：

```bat
.venv\Scripts\python.exe -m scripts.manage backup
.venv\Scripts\python.exe -m scripts.manage verify-backup "C:\Industrial_AI_Gateway\data\backups\具体备份目录"
```

升级前停止前台或服务，确认正常停止，保存完整备份，再替换代码。不要把发行包里的全新 `config` 覆盖到现有现场，也不要删除现场 `data`。恢复需要程序已停止：

```bat
.venv\Scripts\python.exe -m scripts.manage restore "C:\Industrial_AI_Gateway\data\backups\具体备份目录"
```

备份不包含管理口令、证书私钥或密码环境变量；迁移电脑时这些内容需要单独配置。不要在运行中仅复制 `history.db` 主文件而遗漏 WAL。`stop.bat` 会请求本目录实例正常退出，不按 PID 强杀；超时会报告未停止，先查看日志再处理。

## 六、常见问题与现场验收

| 现象 | 检查位置和操作 |
| --- | --- |
| 找不到 Python 3.12 | 在新终端执行 `py -3.12 --version`；没有启动器时执行 `python --version`。其他 Python 版本不能替代本版本要求。 |
| 依赖安装失败 | 检查网络、公司代理或包镜像，或准备离线 wheels 后使用 `--offline`。安装错误上方显示具体包名。 |
| 页面没有自动打开 | 手工打开 `http://127.0.0.1:8080`，确认启动窗口没有退出。 |
| 8080 端口占用 | 停止同目录旧实例；前台可改用 `start.bat --port 8081`。 |
| 连接失败或质量不是 Good | 核对服务器地址、现场网络、实际 NodeId、用户权限、证书信任；检查运行诊断和 `data\logs\gateway.log`。 |
| 服务启动失败 | 查看 Windows 事件查看器及网关日志；核对前台是否已停止、服务账户、Python环境和配置。 |
| 口令无效 | 使用当前安装目录的 `data\operator_pin.txt`；不要使用其他安装目录的口令。 |
| 导入报错 | 使用本版模板，逐行修正；注意 ID、设备内名称和地址不可重复。失败不会部分替换原点表。 |
| 修改冲突 | 重新加载当前配置，复核草稿再提交；页面不会直接覆盖其他修改。 |

部署后至少确认：Windows 前台能够启动和正常停止；实际 PLC 连接和 NodeId 校验通过；断线/恢复能显示质量变化；每点历史开关、阈值和保存间隔符合现场要求；导入导出能保留注释和配置；按需查询返回相应当前/历史数据；需要服务时再验证服务安装、开机启动、账户权限与正常停止。1000 点的实际读取周期和性能需在现场服务器上确认。

## 七、重新生成发行包（交付人员）

在已安装依赖的项目目录执行：

```bat
.venv\Scripts\python.exe scripts\package_release.py --output "C:\交付\Industrial_AI_Gateway_Windows.zip"
```

打包工具按允许列表收集代码和文档，构造新配置和空 Excel 模板，拒绝包含符号链接，不复制原有 `config`、`data`、`.venv`、日志或备份。ZIP 内附文件清单和 SHA-256；ZIP 旁输出 `.sha256` 校验文件。构建后仍需要 Windows 现场验收。
