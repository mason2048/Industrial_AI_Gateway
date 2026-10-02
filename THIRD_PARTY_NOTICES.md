# 第三方软件与许可证

Industrial AI Gateway 自有代码使用根目录 `LICENSE` 中的 MIT 许可。
第三方组件仍使用各自的许可，MIT 不覆盖或替代它们的版权与条款。

Windows 发布包包含 `licenses/` 中的完整文本和 `LICENSE_MANIFEST.json`。
清单记录构建时已安装的组件版本、来源和逐文件 SHA-256；构建脚本从
发行包元数据复制原始许可文件，不将许可名称替代完整文本。源码仓库中
`packaging/licenses/SOURCE_MANIFEST.json` 记录额外许可资料的上游来源和校验值。
不同平台的依赖标记可能使清单有差异。

OPC UA 通信使用未经修改的 FreeOpcUa `opcua` 0.98.13，遵循 LGPL v3 或更高版本。
完整 LGPL v3 和其引用的 GPL v3 文本随包提供。
`third_party_sources/opcua-0.98.13.tar.gz` 是对应的原始源码发行包，
SHA-256 为 `3352f30b5fed863146a82778aaf09faa5feafcb9dd446a4f49ff34c0c3ebbde6`。
软件允许为调试上述 LGPL 组件的修改而进行逆向工程；此权利不受本项目条款限制。
修改、替换组件并重新构建 EXE 的步骤见 `third_party_sources/REBUILD.md`。
本项目的完整应用源码和构建材料在
[公开仓库](https://github.com/mason2048/Industrial_AI_Gateway) 提供。

lxml 及其原生依赖保留安装包中的 BSD/MIT/Zlib 等版权声明和
`LICENSES.txt`，附带其中引用的完整 LGPL 2.1 文本及 lxml 的源码发行包。
Windows 轮子构建使用的 `libiconv` 1.17.1 对应源码一并提供，来源为
官方 lxml Windows 原生构建仓库 `2026.05.17` 引用的 winlibs/libiconv 提交
`880a1fa8b5581e37e136a7b051947d3ea39097b6`。certifi 所引用的完整 MPL 2.0
以及该版本源码发行包也一并提供。
Python 运行时保留 PSF 及历史许可和 `INCORPORATED-SOFTWARE-3.12.rst`
中的内置组件声明。OpenSSL、Tcl 和前端 Vue 的完整许可也随包提供；
部分辅助运行时资料作为额外声明包含，不表示这些组件都被实际导入。

PyInstaller 用于构建 EXE，其 bootloader 例外允许按应用自身的条款分发
生成的程序，前提是继续遵守依赖许可。随包保留 PyInstaller 原始 `COPYING.txt`。
详见 [PyInstaller 官方许可说明](https://pyinstaller.org/en/stable/license.html)。

发布包中的第三方源码不包含项目现场配置、PLC 用户名/密码或采集历史。
所有软件按其各自许可中的保证与责任条款提供。
