# 07 开发 Bug 总结（面试素材）

> 用途：秋招面试讲"你解决过什么难题"。每个 bug 按 **现象 → 根因 → 修复 → 一句话启示** 组织，可直接背。
> 标注：🔴 = 用户（我）在验收时发现；🛠️ = 开发过程中排查发现。

---

## 一、安全 / 权限类（重点，最能体现系统思维）

### 🔴 Bug 1：越权审批 —— 不填直属上级，直接填总经理也能批过

- **级别**：严重（安全漏洞，报销系统第一风险）
- **现象**：报销单挂在"审核人员复核"步骤时，`decide` 接口传 `actor=4001`（总经理）直接批准，流程照样推进到"已批准"；而且**审计记录被伪造**——系统记的是"直属上级: approve"，实际操作人是总经理。
- **根因**：
  1. `decide` 只校验了 **动作合法性**（`approve/return/void` 是否匹配当前步骤），完全没校验 **决策人是谁**。`actor` 是自由填写的字符串。
  2. 审计记录 `_record()` 直接写"当前审批链角色"，不写真实操作人，掩盖了越权。
  3. 底层设计也不一致：审批链末位写的是 `财务`，但业务上"领导决策"的"领导"应该是总经理——链条本身和产品概念对不上。
- **修复**：
  1. 在 `app/api/service.py` 加 `_authorize()`：从已落库状态取 `approval_chain`，按 `current_step` 确定唯一审批人——`review` 步 = 链条第 0 位（直属上级），`leader_decision` 步 = 链条末位（总经理），`actor` 必须与其 `id` 匹配，否则抛 `PermissionError`。
  2. API 层捕获 `PermissionError` → HTTP **403**（语义正确），并把报错写成可读中文（"应由 直属上级(2001) 决策，实际提交人 4001"）。
  3. 修正 `travel.yaml` 审批链：末位从 `财务` 改为 `总经理`，让"领导决策"名副其实，财务只收付款通知（**审批≠支付**）。
  4. 新增 3 个回归测试（服务层 + API 层），覆盖"越权被拒 / 正确审批人放行 / 两级步骤各自的权限边界"。
- **启示（面试话术）**："权限校验要放在**服务层唯一卡口**（不只依赖接口层），因为图编排会被不同入口调用；**审计记录必须记录真实操作人**，否则越权会被掩盖；改安全 bug 时要把相关的**设计不一致**一并清理，而不是只堵漏。"
- **配套代码**：`app/api/service.py::_authorize`、`app/api/reimburse.py`（403 映射）、`app/policy/travel.yaml`、`tests/test_graph_flow.py`、`tests/test_api.py`

---

## 二、环境 / 依赖类（Windows 开发环境，常见坑）

### 🛠️ Bug 2：Python 3.14 装不上 langgraph —— `_xxhash` DLL 加载失败

- **现象**：系统默认 Python 3.14 下 `pip install -r requirements.txt`，`ImportError: DLL load failed while importing _xxhash`。
- **根因**：langgraph 依赖的 `xxhash` 在 Python 3.14 上**没有预编译 wheel**，源码编译产物与运行时不匹配。
- **修复**：切换到 conda Python 3.13.9（有预编译包），`.venv` 重建于 3.13，全部依赖装通。
- **启示**："生态依赖（尤其带 C 扩展的包）存在版本滞后，**先确认目标 Python 版本有预编译 wheel** 再搭环境；固定解释器版本（3.13）保证可复现。"

### 🛠️ Bug 3：MSYS2 Python 的 pip 报 SSL 证书错误

- **现象**：用 MSYS2 Python 建 venv 后 `pip install` 全部 `SSLCertVerificationError`。
- **根因**：MSYS2 发行版未带系统的 CA 证书链，pip 无法校验 PyPI 证书。
- **修复**：放弃 MSYS2，统一用 conda Python。
- **启示**："Windows 上第三方 Python 发行版（MSYS2/多版本共存）的证书和 wheel 兼容性各不相同，**指定一个发行版并写进文档/脚本**，避免环境漂移。"

---

## 三、框架使用类（LangGraph 特性坑，体现源码阅读能力）

### 🛠️ Bug 4：langgraph 节点名不能含冒号 `:`

- **现象**：子图节点用 `f"{name}:{node}"` 命名，构建时报 `ValueError: ':' is a reserved character`。
- **根因**：langgraph 把 `:` 用作命名空间/保留字符，业务节点名里不能出现。
- **修复**：改成下划线 `f"{name}_{node}"`。
- **启示**："框架的**保留字符**是踩坑高频区；遇到这类 ValueError 先查框架对标识符的约束，而不是猜命名。"

### 🛠️ Bug 5：`langgraph.__version__` 属性不存在

- **现象**：想打印版本号，`langgraph.__version__` 抛 AttributeError。
- **根因**：新版本 langgraph 未定义 `__version__` 模块属性。
- **修复**：改用 `importlib.metadata.version("langgraph")`（从包元数据读）。
- **启示**："**不要依赖包的魔法属性**，元数据版本号要走 `importlib.metadata`，这是标准做法。"

---

## 四、编码 / 中文环境类（Windows 特有）

### 🛠️ Bug 6：控制台打印 `¥` / 中文报 UnicodeEncodeError

- **现象**：Mock 通知/邮件在控制台 `print` 含 `¥` 和中文时，报 `UnicodeEncodeError: 'gbk' codec can't encode`。
- **根因**：中文 Windows 控制台默认 GBK 编码，无法编码 `¥`（U+00A5）等字符。
- **修复**：装配根 `_force_utf8_console()`，启动时把 `stdout/stderr` `reconfigure(encoding="utf-8")`。
- **启示**："**编码问题要在进程边界统一**：入口处一次性切 UTF-8，而不是在每处 print 处理。"

### 🛠️ Bug 7：Git Bash 里 curl 传中文 JSON body 报 "error parsing the body"

- **现象**：`curl -X POST ... -d '{"comment":"复核通过"}'` 服务端回 `There was an error parsing the body`。
- **根因**：Git Bash 的 curl 把中文按本地编码转成字节，破坏了 JSON 的 UTF-8 完整性 / Content-Length。
- **修复**：开发/调试时用纯 ASCII 的 JSON，或把请求体写成 UTF-8 文件 `--data-binary @file.json`；multipart 表单（`-F`）不受影响。
- **启示**："调试工具与 Windows 终端的中文编码组合是个**暗坑**，接口联调遇到 parse 错误先怀疑 body 字节，而不是服务端。"

---

## 五、业务规则 / 审计类（设计正确性问题）

### 🛠️ Bug 8：批准后没通知财务 —— 审批 ≠ 支付 没落地

- **现象**：领导批准后流程就结束了，财务（出纳）收不到付款通知。
- **根因**：`approve_node` 只置 `approved` 状态，忘了触发 `notify_finance`。
- **修复**：在批准节点补 `notify.notify_finance(...)`，保证"审批通过 → 出纳收到付款任务"的闭环。
- **启示**："业务上**审批通过 ≠ 打款**，付款必须由财务独立执行。写流程图时就要把'角色'和'动作'分开，代码里别漏。"

### 🛠️ Bug 9：审核人员"批准"没写进审计记录

- **现象**：审计记录只有领导的最终决策，审核人员这级审批"隐形"了，无法支撑驾驶舱按级统计。
- **根因**：`email_leader` 节点推进到领导决策时，没把审核人员的 approve 追加进 `approval_records`。
- **修复**：该节点返回时补 `approval_records: _record(..., "approve")`。
- **启示**："**每一级决策都要留痕**，审计记录是数据产品（驾驶舱/统计）的基础，宁可多记不可漏记。"

---

## 六、接口 / 文档类

### 🛠️ Bug 10：Swagger 接口文档不可读（无中文说明、无返回结构）

- **现象**：`/docs` 页面上每个接口只有路径没有业务说明，字段含义靠猜，返回结构完全未知。
- **根因**：接口装饰器没写 `summary`/`description`，也没有 `response_model`，OpenAPI 元数据是默认值。
- **修复**：
  1. 每个接口补中文 `summary`（如"提交报销（报销端 · 第一步）"）+ 分步骤 `description`（含"允许的动作/结果"表格）。
  2. 建 `RequestDetail` / `AdvanceDetail` 返回模型，字段逐个写中文注释，Swagger 自动渲染出可读的响应结构。
  3. `main.py` 给整个文档页写"5 分钟体验闭环"概览 + 演示角色表 + 状态机速查。
- **启示**："**文档即接口的一部分**。FastAPI 用 OpenAPI 元数据自动生成文档，花 10 分钟把 summary/response_model 补全，调试和演示效率翻倍，也是工程素养的体现。"

### 🛠️ Bug 11：`decision` 字段类型声明错误导致响应校验失败（500）

- **现象**：加了 `response_model=RequestDetail` 后，`/decide` 返回 500，报 `Input should be a valid string`。
- **根因**：`decision` 实际存的是 **dict**（`{action, comment, actor}`），模型里却声明成 `Optional[str]`，FastAPI 响应校验不过。
- **修复**：改为 `Optional[dict]` 并注明结构。
- **启示**："**response_model 是双刃剑**：它把'接口返回什么'显式化，也让类型错误立刻暴露在测试里——这正是它该干的，别因为报错就删模型。"

### 🛠️ Bug 12：README / 架构文档里 mermaid 图渲染报错

- **现象**：`subgraph` 标题含中文全角括号，GitHub 上 mermaid 解析失败，架构图显示不出来。
- **根因**：mermaid 对标题内的全角字符 `（）` 解析不兼容。
- **修复**：给 subgraph 标题加引号 `subgraph "前端层（SPA…）"`。
- **启示**："**作品集的门面是文档**。mermaid 语法坑要用引号包标题规避；写完在 GitHub 预览确认再推送。"

---

## 七、开发流程 / 工具类

### 🛠️ Bug 13：PyCharm 运行配置是空壳

- **现象**：`.idea/workspace.xml` 里预置的 `run` 配置 `SCRIPT_NAME` 为空，点了跑不起来。
- **根因**：模板占位配置没填目标。
- **修复**：在 `.idea/runConfigurations/` 新建共享运行配置（`FlowInvoice server`：模块 `uvicorn` + 参数 `app.main:app --reload --port 8000`；`FlowInvoice tests`：pytest）。模块模式避免硬编码解释器路径。
- **启示**："项目**可一键运行**是交付底线；运行配置放共享目录、用模块模式，换机器/换解释器都不用改。"

### 🛠️ Bug 14：`--reload` 偶尔不热更 main.py

- **现象**：改了 `main.py` 的文档描述，WatchFiles 没触发重载，接口文档还是旧的。
- **根因**：WatchFiles 对个别文件监听有时失效（Debounce/锁）。
- **修复**：改入口文件后手动重启服务，不依赖热重载。
- **启示**："`--reload` 适合开发但不保证 100% 生效，**改入口/装配类文件后要重启验证**。"

---

## 速查表（面试 30 秒版）

| # | 一句话 | 关键词 |
|---|---|---|
| 1 | 越权审批：actor 不校验 + 审计伪造 + 审批链角色错位 | 权限 / 审计 / 服务层卡口 / 403 |
| 2 | Python 3.14 无 xxhash 预编译 wheel | 环境 / wheel / 版本滞后 |
| 3 | MSYS2 缺 CA 证书链 | 环境 / SSL |
| 4 | langgraph 节点名保留字符 `:` | 框架 / 保留字符 |
| 5 | `langgraph.__version__` 不存在 | 元数据 / importlib.metadata |
| 6 | GBK 控制台打不了 `¥` | 编码 / UTF-8 边界 |
| 7 | curl 中文 JSON body 解析失败 | 编码 / 调试工具 |
| 8 | 批准后没通知财务 | 审批≠支付 / 业务闭环 |
| 9 | 审核人员批准未进审计 | 审计 / 每一级留痕 |
| 10 | Swagger 无中文说明 | 文档即接口 / OpenAPI |
| 11 | decision 类型声明错 → 500 | response_model / 显式契约 |
| 12 | mermaid 全角括号报错 | 文档门面 / 引号包标题 |
| 13 | PyCharm 运行配置空壳 | 一键运行 / 共享配置 |
| 14 | --reload 不热更 main.py | 重启验证 |

> 面试建议：**讲 Bug 1（越权审批）做主线**——现象 → 根因（三层）→ 修复（四步）→ 启示，全程 3 分钟；其余 bug 挑 2-3 个当"亮点补充"，展示排查思路（如 2/4/6 反映环境与框架功底，8/9 反映业务理解）。
