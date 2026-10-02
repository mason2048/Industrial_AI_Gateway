# 修改第三方组件并重新构建

发布 EXE 使用 FreeOpcUa `opcua` 0.98.13 的原始源码，未修改该库。
完整源码为同目录 `opcua-0.98.13.tar.gz`；LGPL v3 和 GPL v3 正文在
`licenses/packages/opcua/`，源码仓库对应位置为 `packaging/licenses/opcua/0.98.13/`。
你可以修改该库、为调试其修改而逆向工程，并用修改后的版本重新构建应用。
本项目 MIT 许可不限制 LGPL 赋予的权利。

1. 从 https://github.com/mason2048/Industrial_AI_Gateway 下载与你的发布版本
   对应的源码（release 的 tag），在 Windows x64 上安装 Python 3.12。
2. 在项目根目录打开终端，创建独立环境并安装锁定的依赖：

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements-build-lock.txt
   ```

3. 解开 `packaging/third_party_sources/opcua-0.98.13.tar.gz`，编辑需要修改的
   Python 文件，保留原有版权/许可声明，为你修改的文件添加显著修改说明。
   在开发目录保留 `0.98.13` 版本号，安装修改后的库：

   ```powershell
   tar -xf packaging/third_party_sources/opcua-0.98.13.tar.gz
   .\.venv\Scripts\python.exe -m pip install --no-deps --force-reinstall .\opcua-0.98.13
   ```

4. 执行测试和 Windows 打包脚本：

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   .\.venv\Scripts\python.exe scripts/build_windows.py
   ```

   `scripts/build_windows.py` 和 `packaging/IndustrialAIGateway.spec` 是完整
   打包材料；构建结果位于 `dist/`。它们不依赖发布者的私有配置、账号或密钥。
   若你改变组件版本，还应更新依赖锁及 `SOURCE_MANIFEST.json` 的对应许可、
   源码和 SHA-256，然后再构建。

5. 如果再分发修改后的组件，保留原有许可，提供对应修改后的源码和修改说明，
   继续按对应 LGPL 条款分发组件。生成的应用 EXE 可以安装并运行修改后的
   组件版本，不需要发布者签名、专用密钥或服务器授权。

lxml 使用 PyPI 的原始二进制发行包；原始源码也在同目录提供。
其完整原生依赖声明位于 `licenses/packages/lxml/LICENSES.txt`。
Windows 轮子的 `libiconv` 1.17.1 原生库源码位于
`libiconv-windows-1.17.1.tar.gz`，对应 winlibs/libiconv 提交
`880a1fa8b5581e37e136a7b051947d3ea39097b6`，保留了其 Visual Studio
构建材料、原始 `source/COPYING.LIB` 和 `source/COPYING`。
该版本出自 https://github.com/lxml/libxml2-win-binaries 的
`2026.05.17` 标签。使用该仓库的 `build.ps1` 可以重建该组 Windows
原生库；构建前按该标签初始化子模块，然后替换其中的 libiconv 为你修改
的对应源码。安装 Visual C++ Build Tools，并依该仓库 README 执行构建。
如果修改或替换 lxml/原生依赖，在源码环境先安装修改的发行包，再运行同一
打包脚本；lxml 的 `buildlibxml.py` 与 `setup.py` 提供原生库的构建入口。
涉及 LGPL 原生组件时，保留相应许可和再分发源码材料。

certifi 的源码归档 `certifi-2026.7.22.tar.gz` 包含对应根证书数据的源码形式，
其 MPL 2.0 原文位于 `licenses/runtime/certifi/MPL-2.0.txt`。

所有归档校验值和上游下载地址都记录于 `LICENSE_MANIFEST.json` 以及源码仓库
`packaging/licenses/SOURCE_MANIFEST.json`。重构过程中只应使用你自己的
测试配置和测试 PLC，避免将现场数据加入公开发行包。
