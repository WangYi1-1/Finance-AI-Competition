# 第三方组件与使用范围

本项目调用第三方库和 DeepSeek 官方服务。提交比赛材料时，说明各组件负责的部分，并将团队实现的数据契约、数字核验、可比性检查、证据关联及信号规则写清楚。

以下许可证依据项目维护方的官方来源核对。实际安装版本见 `requirements.lock.txt`；该文件还包含间接依赖，分发完整环境时也需要保留这些包随附的许可证及版权声明。

| 组件 | 当前安装版本 | 本项目使用范围 | 许可证 | 官方来源 |
| --- | --- | --- | --- | --- |
| PyMuPDF / MuPDF | PyMuPDF 1.28.2 | PDF 文本、表格、坐标、页面图片及虚构样本 PDF 生成 | GNU AGPL v3，或 Artifex 商业授权 | [官方仓库说明](https://github.com/pymupdf/PyMuPDF#licensing)、[授权说明](https://pymupdf.io/licensing) |
| Streamlit | 1.64.0 | 上传、指标表、信号卡片、页面查看和结果下载 | Apache-2.0 | [LICENSE](https://github.com/streamlit/streamlit/blob/develop/LICENSE) |
| Pydantic | 2.13.5 | 数据字段、类型及结构校验 | MIT | [LICENSE](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| HTTPX | 0.28.1 | 发起 DeepSeek HTTPS 请求 | BSD-3-Clause | [LICENSE](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| python-dotenv | 1.2.4 | 读取本地 `.env` 配置 | BSD-3-Clause | [LICENSE](https://github.com/theskumar/python-dotenv/blob/main/LICENSE) |
| pytest | 9.1.1 | 财务边界条件与流程测试，仅用于开发验证 | MIT | [LICENSE](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |

PyMuPDF 采用 AGPL v3 或商业授权两条路径。分发或部署应用时，应按照选择的授权方式处理对应源码提供和版权保留要求；一张组件清单不能替代许可证本身。本原型保留源码供团队学习和比赛审查，尚未选择整个项目对外发布的许可证。

DeepSeek 是通过官方 API 使用的模型服务，本项目没有分发模型权重。接口地址、模型名称和密钥通过本地配置提供；当前默认 `deepseek-flash`，依据 [官方模型说明](https://api-docs.deepseek.com/quick_start/pricing/)。真实分析将检索到的财报片段发送至该服务。

项目内演示财报由 `finance_agent/demo.py` 生成，企业与金额均为虚构，仅用于流程演示。团队后续补充真实样本时，应分别记录公司、报告期、原始披露链接与下载日期，并将人工答案与模型输出分开。

核对日期：2026 年 10 月 2 日。
