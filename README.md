# Industrial AI Gateway

本地工业数据网关，通过 OPC UA 读取 PLC 数据，提供实时监控、SQLite 历史、Excel 点位管理和按需数据查询。Windows 免安装版自带运行环境，双击 EXE 后在本机浏览器打开操作界面。

[下载 Windows 版](https://github.com/mason2048/Industrial_AI_Gateway/releases/latest) · [Windows 使用说明](docs/WINDOWS_DEPLOYMENT.md) · [模型配置说明](docs/MODEL_SETUP.md) · [源码运行说明](docs/SOURCE_DEPLOYMENT.md)

## Windows 快速开始

1. 在 Releases 下载最新版本的 `Industrial_AI_Gateway-v<版本号>-windows-x64.zip`，解压到本机可写文件夹。
2. 双击 `IndustrialAIGateway.exe`。不需要安装 Python、Node.js、Docker 或 Excel；界面和采集可在本机离线运行。
3. 首次启动为**模拟模式**，包含 6 个明确标注的样本点位，没有预置历史。历史从启动后开始产生。模拟数据用于检查软件流程。
4. 双击 `打开数据目录.cmd`，在 `data\operator_pin.txt` 找到管理口令，填写到页面的管理口令框。
5. 在 **点位管理** 下载 Excel 模板，填写真实 NodeId 并导入；在 **PLC连接** 选择真实 OPC UA 模式，填写服务器地址，测试后保存。
6. 查看实时数据的质量与采集时间。退出时点击页面右上角 **退出软件**，或双击 `停止软件.cmd`。关闭浏览器页面后采集仍继续。

也可只下载 `IndustrialAIGateway.exe` 直接运行。运行数据默认位于 EXE 旁的 `gateway-data`；如果首次安装位置不可写，则使用 `%LOCALAPPDATA%\IndustrialAIGateway`。已存在的安装不会因权限变化自动换目录。重复双击会打开同一实例；默认 8080 端口被占用时自动选择其他本机端口。

## 基础功能与操作入口

| 功能 | 页面入口与行为 |
| --- | --- |
| 通信与读取 | **PLC连接**：OPC UA 地址、账号/证书、读取周期、每批点数；支持周期批量轮询 |
| 最多 1000 点 | **点位管理**：BOOL、WORD、DWORD、FLOAT，填写实际 OPC UA NodeId |
| 读写限制 | 点位权限和 AI 注释分开配置；真实 PLC 在接口和驱动层都禁止写入 |
| 实时值 | **实时监控**：从当前轮询快照取值，附质量和时间；关闭保存的点不写历史 |
| 历史保存 | 默认每 1800 秒补存，逐点可设置保存开关、间隔、变化阈值和是否额外记录变化 |
| 最近 7 天 | SQLite 自动清理过期采样；**历史曲线**查看已保存值与数据缺口 |
| 小数精度 | 点位编辑的阈值旁可设置 0–10 位，真空参数可填 6；数据库保留采集值，曲线与统计使用原历史定义精度 |
| Excel | **点位管理**：空模板、整表导入预检/确认、导出当前点表，保留固定点位 ID、AI 注释和历史策略 |
| 按需查询 | **按需数据查询**：用户提出问题后查询当前/已存数据，平时不进行 AI 分析 |
| 模型接入 | **模型设置**：Ollama、LM Studio / OpenAI兼容接口，配置地址、模型名、Key、超时和输出上限；默认离线规则 |
| AI历史取数 | **点位管理 → 新增/编辑**：按变化阈值或按时间间隔筛选已有记录，仅提问时发送；独立于本地历史保存 |
| 本地诊断 | **运行诊断**：采集、存储、缓冲、磁盘、备份和历史缺口 |

OPC UA 服务器地址与 PLC 程序的心跳握手点位是两个不同概念。本版可设置通信端点与读取方式，尚未实现自动写入 PLC 心跳握手或 OPC UA 订阅切换。使用 KEPServerEX 时，PLC 驱动与原始地址映射在 KEPServerEX 维护，本软件读取其发布的 NodeId。

新增点位默认关闭“记录变化事件”，保存首样本、按间隔补存的样本和质量变化。开启后，数值与**上次已保存值**的变化超过该点阈值时额外保存，BOOL 翻转也会保存。阈值为 0 表示每次变化都保存。这样的策略兼顾定时记录和变化事件；不会把“上传给 AI 的筛选”当成“所有历史只剩阈值事件”。

AI 数据接口仅在请求时返回数据，GET /api/ai/history默认按点位的AI取数方式筛选（旧点位默认变化），保留区间基准和质量变化；按时间模式选择达到间隔的已有样本，不插值、不自动发送。changed_only=false返回原始保存记录，use_point_settings=false且changed_only=true保持统一阈值筛选。筛选附分页和完整性说明，扫描超过50万条原始记录会标记不完整；模型问答的扫描上限为10000条。当前数据从最近轮询快照返回；断线或过期不会视为有效当前值。

点位AI策略用于发送给已配置模型的历史证据。默认“本地规则统计”使用所选范围的全部原始保存样本计算统计，历史曲线也保持原有查询；它们不调用模型。

默认“AI”使用离线本地规则查询和统计。1.3.0 起可在 **模型设置** 接入本地或远程文本聊天模型，先测试再保存，用户提问时才发送有限证据；模型没有 PLC 写入或命令执行权限。Ollama 每次请求完成后请求卸载模型，其他服务的驻留由运行器管理。模型软件和权重需另行安装，建议先试 Qwen3 4B / 8B，详见 [配置说明](docs/MODEL_SETUP.md)。设备健康状态仍为 unknown；模型解释与真实 PLC 性能需现场评价。

## 使用流程

1. 在连接页输入数据目录中的 `data/operator_pin.txt` 中的管理口令。口令只留在页面内存，接口不会返回它。
2. 配置服务器地址和真实 NodeId。可先测试候选连接和点表；预检使用单独只读会话，不切换运行连接。
3. 在点位页导入 Excel，确认新增、删除、修改摘要后应用；或者逐点编辑。页面新增/复制会自动选择未使用过的点位 ID，无需手填；删除后列表序号重新连续排列。配置冲突保留草稿，需载入最新版本后重新确认。
4. 在总览选择设备、趋势变量和关键参数。实时页面显示质量、采集时间和只读状态。
5. 历史页面按来源、设备、连接和定义版本查询，包含删除或改名后的历史变量。曲线覆盖整个时间范围，明细独立分页。
6. 在运行诊断页查看采集、存储、缓冲、磁盘、备份和缺口。程序存活、PLC 通讯正常和设备健康是不同概念。

### 真实 OPC UA 配置

实际服务器必须提供已发布的 NodeId；逻辑地址 `DB1.DBD20` 不能直接替代它。支持 Boolean、UInt16、UInt32、Float，对应 BOOL、WORD、DWORD、FLOAT，PLC 应发布可直接使用的工程值。

可在停止程序后修改配置，或在连接页填写候选安全配置：

```json
{
  "security_string": "Basic256Sha256,SignAndEncrypt,C:/cert/client.der,C:/cert/client-key.pem,C:/cert/server.der",
  "username": "gateway_reader",
  "password_env": "PLC_OPCUA_PASSWORD"
}
```

这些字段合并到完整配置。证书和私钥使用绝对路径，PLC 密码通过运行账户的环境变量提供；服务账户也必须能读取证书并获得该变量。服务器需信任客户端证书。本轮自动化使用本机 OPC UA 服务器，具体 PLC 的证书策略和账号仍须现场验证。

### 点表字段

保留旧版中文 Excel 字段：ID、地址、名称、类型、单位、权限、AI描述、保存、阈值；设备、NodeId、保存间隔秒、小数位数、记录变化为可选列。支持 0–1000 点、5 MB 文件，整表导入失败保持原表；允许清空点表，重启不恢复已删除点。新模板“记录变化”留空按NO处理；没有此列的旧模板保持原来的变化保存行为。

- 页面首列“序号”仅表示当前列表顺序，删除后会连续排列；变量下方的“点位 ID”是 Excel 和历史关联的固定标识，两者不同。例如删除 ID 8、9 后新增点位可获得 ID 10，但页面序号仍为 1、2、3……连续显示。
- ID 永久绑定设备、地址、类型和 NodeId，历史清理后也不可复用为其他变量。页面新增和“复制为新点位”自动选择安全的新 ID，已有点位 ID 不随删除、排序或显示序号变化。
- 使用 Excel 修改已有点位时保留导出的 ID；新增行需填写未使用过的 ID，不能把页面序号当作 ID。手动导入重复或已绑定其他变量的 ID 会被拒绝，原点表保持不变。
- 修改名称、单位、描述、保存策略等会建立新定义版本，由数据库分配；客户端不能指定存储版本。
- `WRITE` 仅用于显式模拟测试，不能启用真实 PLC 写入。
- 点表的权威来源为 SQLite。Excel 是导入/导出文件，不会随在线修改自动覆盖。
- 总览可以选择任意设备和变量，不依赖特定中文名称。

## 数据与可靠性约定

默认采样 1 秒、每批读取 100 点、最多 1000 点、缓冲 120 个完整扫描批次、历史保留 7 天。采集线程不进行数据库写入；接口从内存快照读取状态。

保存策略：首样本、质量变化，以及配置间隔补存；开启记录变化时额外保存相对上次已保存值的阈值变化和BOOL翻转。默认全局1800秒，点位可独立设置。保存关闭的点不落历史。定义或连接身份改变后记录新版本首样本。重启后保留旧基准并建立新运行的计时基准。

- 数据库暂时不可用时保留当前批次并按顺序重试；提交与进度原子保存，重试不会重复插入同一采样。
- 缓冲满时固定保留正在重试的批次，丢弃最旧待写批次，接纳新批次并记录丢弃区间。
- 这是内存缓冲，不承诺断电零丢失；异常重启记录推定缺口，不编造精确丢失数量。断网期间未采到的值也无法凭空补回。
- 历史缺口事件保存失败会在诊断中显示；数据库恢复后补记。
- 网关接收时间继续使用 `timestamp`，另存可空的 `source_timestamp/server_timestamp`；输出均为带时区的 ISO 8601。
- 失鲜、重连、补存计时使用单调时钟。检测到墙时钟跳变超过 5 秒后暂停历史清理，校准系统时间并重启后恢复。
- 源 `simulation/opcua`、`connection_id` 和 `tag_revision` 共同区分历史；旧库无法确认的来源标记为 `legacy`。
- 分桶曲线保留首末值和极值，坏质量或已知缺口断开绘制。未采样桶显示无数据，不推造连续值。
- 全范围统计基于已保存样本；样本均值不是时间加权工艺均值。

SQLite 继续使用 WAL、外键和事务。保留时长可以修改，是否更换数据库应依据实际数据量与查询性能决定。

## 接口与管理授权

交互文档位于 `/docs`，离线可访问 `/openapi.json`；Swagger 自身静态资源可能需要联网。主应用静态资源全部随项目提供。

| 接口 | 用途 |
| --- | --- |
| GET /api/health | Web 程序存活，保留原版语义 |
| GET /api/ready | 采集、PLC、存储、缓冲、磁盘检查，不就绪返回 503 |
| GET /api/diagnostics | 缓冲、重试、保存进度、事件和备份 |
| GET /api/current | 当前值、质量、时间、连接与定义版本 |
| GET /api/config、GET /api/tags | 公开配置、当前点表；响应包含 ETag |
| GET /api/tags/next-id | 获取未使用过的点位 ID，考虑当前点表和已删除点位的身份记录 |
| POST /api/connection | 修改连接，需管理口令和 If-Match |
| POST /api/connection/test | 只测试候选连接，需管理口令 |
| PUT /api/tags | 原子替换点表，需管理口令和 If-Match |
| POST /api/tags/validate | 验证候选点表及节点，需管理口令 |
| POST /api/tags/import?dry_run=true | Excel 差异预检，返回应用所需 ETag |
| POST /api/tags/import | 应用 Excel，需管理口令和预检 If-Match |
| GET /api/tags/export、template | 导出当前点表、下载空模板 |
| GET /api/history | 历史明细、分页与全范围统计 |
| GET /api/history/series | 单点全时段分桶曲线，默认 500 桶，最多 2000 |
| GET /api/history/variables | 包含退役/改名定义的历史目录 |
| GET /api/ai/current、history、status | AI 只读数据接口 |
| GET、POST /api/ai/config | 读取/修改模型设置，修改需管理口令和模型 ETag |
| POST /api/ai/test | 仅测试候选模型，需口令，不保存、不发工业数据 |
| POST /api/ai/query | 本地规则或配置的模型；模型需口令和 If-Match，默认当前连接 |
| POST /api/shutdown | 本机管理口令授权后正常退出 |
| POST /api/operator/write-request、write-confirm | 仅显式模拟测试，不允许真实 PLC 写入 |

管理请求头为 `X-Operator-Pin`。点表 ETag 形如 `"tags-2"`，连接 ETag 形如 `"config-1"`；修改时原样放入 `If-Match`。缺版本返回 428，版本冲突返回 412，不支持无条件覆盖。

历史支持 device、variable、tag_id、start、end、source、connection_id、tag_revision；明细另有 limit、offset。单次时间范围最多 31 天，未带时区按 Asia/Shanghai 解释。曲线按来源和定义分组返回 `series`，仅单分组时也提供顶层 `items`。

要测试模拟写入，停机后设置 `simulation_write_enabled=true`，将点位权限设为 WRITE，再使用原有口令和一次性确认流程。真实模式下该开关无效。

## 备份、恢复与 Windows 服务（源码安装）

运行期间默认每天备份一次，保留最近 7 份完整备份。位置为 `data/backups/<时间>-<标识>/`，包含 SQLite 一致性备份、配置、SHA-256 清单。不会复制口令、私钥文件或密码环境变量；恢复到其他机器时应单独配置这些凭据。

```sh
python -m scripts.manage backup
python -m scripts.manage verify-backup /absolute/path/to/backup
python -m scripts.manage stop
python -m scripts.manage restore /absolute/path/to/backup
```

恢复要求应用已停止，持有运行锁和维护锁后校验并备份当前状态；失败自动回滚。恢复工具不擅自迁移旧库，下次启动再按版本迁移。升级前会另建 `data/history.pre-v2-*.sqlite3` 迁移备份。不要在运行中只复制主 `.db` 而遗漏 WAL。

Windows 管理员终端：

```powershell
.venv\Scripts\python.exe -m scripts.bootstrap service-init
.venv\Scripts\python.exe -m backend.windows_service install
.venv\Scripts\python.exe -m backend.windows_service start
.venv\Scripts\python.exe -m backend.windows_service status
.venv\Scripts\python.exe -m backend.windows_service stop
.venv\Scripts\python.exe -m backend.windows_service remove
```

服务依赖 pywin32 仅在 Windows 安装。服务自动启动，故障恢复延迟 5、15、30 秒。现场检查服务账户权限、证书、密码环境变量和磁盘。服务与前台入口不能同时运行同一目录。

日志位于 `data/logs/gateway.log`，每份 10 MB、最多 10 份；凭据脱敏，持续相同故障只记录状态变化。停机总预算为 30 秒，超时会报告未完成；只要后台线程仍可能写入，就继续持有目录锁，阻止恢复或第二实例。

## 开发与验证

```sh
python3.12 -m scripts.bootstrap init
.venv/bin/python -m pytest -q --disable-warnings tests
node --test tests/frontend_v11.test.cjs tests/frontend_basic.test.cjs tests/frontend_model.test.cjs
```

Node 仅用于前端开发测试，不是运行依赖。浏览器端到端测试使用可选 Playwright，见 `tests/browser_v11.cjs`；生产安装没有 npm 步骤。

测试使用临时安装目录、本机 OPC UA 服务器和故障注入，不操作正式数据或实体 PLC。自动化结果、已知边界和现场验收清单见 [TEST_REPORT.md](TEST_REPORT.md)，已实现任务与后续路线见 [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)。

节点浏览、工程量转换、设备诊断、多 PLC、局域网身份授权和真实 PLC 控制属于后续范围。模型接入为有限数据证据的按需问答，尚未提供知识库、多轮记忆或自动工具调用。

## 构建与许可证

Windows EXE 由 GitHub Actions 在 Windows x64 / Python 3.12 环境构建并执行启动、数据、模板与退出检查。开发者可按 [构建说明](docs/WINDOWS_DEPLOYMENT.md#从源码构建-exe) 重建。源码采用 [MIT](LICENSE)；第三方组件保留各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)，发行包包含许可文本及 OPC UA 依赖源码。
