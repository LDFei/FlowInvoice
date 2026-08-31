# 发票报销 Agent 系统 —— 全局代码规范

> 版本：v0.1 ｜ 适用范围：本仓库全部代码
> 核心目标：**分层清晰、复用充分、业务可插拔、每行代码可读可讲**

---

## 1. 目标与核心原则

| # | 原则 | 含义 |
|---|------|------|
| 1 | **分层清晰、依赖单向** | 上层依赖下层，同层互不依赖；禁止反向依赖 |
| 2 | **公共能力抽取复用** | 跨 2+ 业务使用的能力必须抽到共享层，如上传解析 |
| 3 | **业务可插拔** | 新增业务 = 新建模块 + 注册表注册，主框架零改动 |
| 4 | **低耦合高内聚** | 模块间只通过 State / 接口通信，不互相 import |
| 5 | **每行代码可讲** | 注释写明「作用」（技术）与「业务用途」（为什么） |
| 6 | **配置驱动** | 政策/审批链规则外置 YAML，不硬编码在 Agent 里 |
| 7 | **最小实现、不做过度设计** | 只写必要的代码和函数，不为「可能」的未来加抽象/参数/分支 |

---

## 2. 目录结构与分层

```
flowinvoice/
├── docs/                    # 文档
├── frontend/                # 前端 SPA（报销端/审核端/管理端，二期）
├── app/                     # 后端
│   ├── main.py              # FastAPI 入口（只做装配，不含逻辑）
│   ├── api/                 # ① 接入层：路由 / DTO / 鉴权
│   ├── graphs/              # ② 编排层：LangGraph
│   │   ├── router_graph.py  #    总控图（分类路由 / 分发 / 汇总）
│   │   ├── base_subgraph.py #    子图模板
│   │   └── businesses/      #    业务模块（插件，注册表装配）
│   │       ├── travel/      #      差旅（本期第一条闭环）
│   │       ├── office/      #      办公（后续）
│   │       ├── entertainment/#     招待（后续）
│   │       └── procurement/ #      采购（后续）
│   ├── shared/              # ③ 共享业务模块（跨业务复用）
│   │   ├── advance/         #    事前申请（实体/生命周期/校验）
│   │   ├── policies/        #    政策规则引擎（YAML 加载与匹配）
│   │   └── analytics/       #    统计驾驶舱（后续）
│   ├── tools/               # ④ Agent 工具（OCR/验真/通知/邮件…）
│   ├── adapters/            # ⑤ 外部系统接口（依赖倒置）
│   │   ├── base.py          #    接口定义（抽象）
│   │   ├── mock_oa.py       #    Mock 实现
│   │   └── storage.py       #    存储适配器
│   ├── core/                # ⑥ 通用基础（与业务无关）
│   │   ├── config.py        #    全局配置
│   │   ├── state.py         #    State Schema（图状态）
│   │   ├── uploader.py      #    上传解析（抽取复用！）
│   │   └── ids.py           #    ID 生成 / 单号
│   ├── registry.py          # 业务模块注册表
│   └── policy/              # 政策规则配置 YAML
└── tests/
```

**依赖方向（单向，从上到下）：**

```
api  →  graphs  →  businesses  →  shared  →  tools  →  adapters / core
                      └────────────┴───────────┘
                 （businesses 可依赖 shared/tools/adapters/core）
```

**分层职责：**

| 层 | 职责 | 禁止 |
|----|------|------|
| `api/` | 参数校验、鉴权、调图、返回 | 不允许写业务判断 |
| `graphs/` | 图编排、状态流转 | 不允许直接碰外部系统 |
| `businesses/*` | 业务专属节点与规则 | 不允许互相 import |
| `shared/*` | 跨业务复用能力 | 不允许反向依赖 businesses |
| `tools/` | 被 LLM/节点调用的动作 | 不允许持有图状态 |
| `adapters/` | 对接外部系统 | 不允许出现业务逻辑 |
| `core/` | 通用基础 | 不允许依赖任何业务层 |

---

## 3. 代码复用规范（重点）

### 3.1 抽取判定规则

> **一个能力被 2 个及以上业务使用 → 必须抽到共享层。**

| 能力 | 归属 | 复用面 |
|------|------|--------|
| 上传/文件解析 | `core/uploader.py` | 全部业务 |
| OCR 发票识别 | `tools/ocr_tool.py` | 全部业务 |
| 验真/查重 | `tools/verify_tool.py` | 全部业务 |
| 事前申请匹配 | `shared/advance/` | 差旅/招待/采购 |
| 政策规则引擎 | `shared/policies/` | 全部业务 |
| 通知/邮件 | `tools/notify_tool.py` / `email_tool.py` | 全部业务 |
| 审批链生成 | `shared/policies/` | 全部业务 |

### 3.2 案例：上传功能抽取（核心示例）

**为什么抽**：上传是**所有业务**的入口（差旅上传火车票、采购上传专票、招待上传餐饮票），若不抽取，每个业务模块都写一遍文件处理 = 高耦合 + 重复。

**抽取方式**：`core/uploader.py` 提供统一入口，输出**业务无关的标准 DTO** `InvoiceInput`，业务子图只消费 DTO，不关心文件从哪来、什么格式：

```python
# core/uploader.py —— 上传解析（跨业务复用的公共能力）
# 业务：所有业务（差旅/采购/招待/办公）共用此入口，
#       业务子图只接收标准 InvoiceInput，无需重复写文件处理。
from dataclasses import dataclass
from pathlib import Path


@dataclass
class InvoiceInput:
    """统一发票输入 DTO（业务无关，屏蔽来源差异）"""
    # 作用：上传文件落盘路径，供 OCR 工具读取
    # 业务：一张发票可能有图片/PDF/电子票等多种来源，统一落盘后处理
    file_path: Path
    # 作用：业务方向标记
    # 业务：travel/procurement/... 由申报人在提交时选择，供路由与事前申请匹配
    direction: str
    # 作用：申报人填写的事由
    # 业务：供 Agent 总结与合规判断参考（如"采购研发测试设备"）
    purpose: str = ""
    # 作用：申报人申报金额（税前）
    # 业务：后续与 OCR 抽取金额做差异比对（不一致 → 风险标记）
    declared_amount: float = 0.0
    # 作用：支付方式
    # 业务："personal"=员工垫付（财务补钱给员工）/"corporate"=对公付款（付供应商）
    payment_method: str = "personal"


class Uploader:
    """发票上传解析器（跨业务复用）"""

    def save_and_parse(self, file_storage, direction: str, **meta) -> InvoiceInput:
        # 作用：落盘文件 → 构造标准 DTO
        # 业务：任何业务走这里；新增业务无需改动此层
        file_path = self._persist(file_storage)          # 作用：写临时目录
        # 作用：返回统一 DTO，后续流程只认 InvoiceInput
        # 业务：屏蔽"发票来源/格式"差异，让业务子图专注审核逻辑
        return InvoiceInput(file_path=file_path, direction=direction, **meta)
```

---

## 4. 业务模块规范（插件式扩展）

### 4.1 模块接口（`graphs/businesses/__init__.py`）

```python
# graphs/businesses/__init__.py —— 业务模块基类
# 业务：所有业务（差旅/采购/招待/办公）遵循同一接口，
#       使总控图能用统一方式路由与执行任意业务。
from abc import ABC, abstractmethod


class BusinessModule(ABC):
    """业务模块基类：新业务继承它即可被总控图驱动"""

    name: str                    # 作用：业务标识
    invoice_types: list[str]     # 作用：该业务接受的发票类型清单

    @abstractmethod
    def build_graph(self):
        """作用：构建本业务专属 LangGraph 子图
        业务：差旅子图/采购子图……各自内部节点不同"""

    @abstractmethod
    def policy_rules(self):
        """作用：返回本业务政策规则
        业务：差旅看住宿标准/日期，采购看三单匹配，规则各异"""

    @abstractmethod
    def bind_tools(self):
        """作用：绑定本业务专属工具集
        业务：如采购额外绑定三单匹配工具"""
```

### 4.2 注册表（`registry.py`）

```python
# registry.py —— 业务模块注册表（扩展点）
# 业务：新增业务 = 新建 businesses/<name>/ 包 + 在此注册，主框架零改动
from graphs.businesses.travel import TravelModule

REGISTRY: dict[str, BusinessModule] = {
    "travel": TravelModule(),   # 本期：差旅闭环
    # "office": OfficeModule(),              # 二期加入
    # "entertainment": EntertainmentModule(),  # 三期加入
    # "procurement": ProcurementModule(),      # 四期加入
}


def route_to_module(direction: str) -> BusinessModule:
    """按业务方向路由到业务模块"""
    # 作用：总控图 classify 节点调用
    # 业务：direction 来自申报人提交或 classify 判定；未注册则报业务错误
    if direction not in REGISTRY:
        raise UnknownBusinessError(direction)
    return REGISTRY[direction]
```

---

## 5. 注释规范（每行代码可讲）

### 5.1 两类注释

| 注释 | 前缀 | 回答的问题 |
|------|------|-----------|
| **作用**（技术） | `# 作用：` | 这行代码做了什么 |
| **业务用途**（为什么） | `# 业务：` | 对应什么业务规则/为什么这么设计 |

### 5.2 规则

1. **关键行**（有逻辑/有分支/有外部副作用）→ 必须注释「作用」。
2. **业务相关**（规则、状态、决策）→ 必须注释「业务」，说明对应企业制度或流程环节。
3. 纯声明（import、简单变量）→ 可省略或一行块注释。
4. 函数/类 → 用 docstring 写「职责 + 业务上下文」。
5. **禁止**：注释复述代码本身（`x = x + 1  # x 加一`），只写"作用+业务"两件事。

### 5.3 示例（差旅合规节点）

```python
# graphs/businesses/travel/nodes.py
def check_travel_compliance(state):
    """差旅合规节点：校验日期与住宿标准"""
    # 业务：企业差旅制度 —— 发票日期须在出差申请区间内；住宿有每日标准上限。
    invoice = state["invoice_data"]                       # 作用：取 OCR 结果
    apply = state["advance_application"]                  # 作用：取关联的出差申请
    # 作用：比对发票日期是否落在出差申请区间
    # 业务：日期对不上 → 可能员工用非出差期间的票报销，需标记风险
    if not (apply.start_date <= invoice.date <= apply.end_date):
        state["risk_flags"].append("日期不在出差申请区间内")
    # 作用：比对住宿单价是否超标准上限
    # 业务：超标准不阻断，但加标记并提示需特殊审批
    if invoice.unit_price > state["policy"].hotel_daily_limit:
        state["risk_flags"].append(f"住宿超标准 ¥{invoice.unit_price - state['policy'].hotel_daily_limit}")
    return state
```

---

## 6. 命名规范

| 对象 | 规范 | 示例 |
|------|------|------|
| 文件 | `snake_case.py`，业务模块用名词 | `router_graph.py` |
| 类 | `PascalCase`，业务模块加 `Module` | `TravelModule` |
| 函数/方法 | `snake_case` 动词开头 | `route_to_module` |
| 变量 | `snake_case` 名词 | `approval_chain` |
| 状态字段 | `snake_case`，小写 | `return_reason` |
| 常量 | `UPPER_SNAKE` | `MAX_RESUBMIT_COUNT = 3` |
| 布尔 | `is_/can_/has_` 前缀 | `can_resubmit` |
| 单号 | `{业务缩写}-{日期}-{序号}` | `PA-20260831-001` |

---

## 7. State Schema 与数据流规范

### 7.1 职责分组（State 只放流程需要的数据）

```python
# core/state.py —— 图全局状态（LangGraph 各节点共享）
from typing import TypedDict, Optional


class ReimbursementState(TypedDict):
    """报销全流程状态"""
    # —— 输入（api 层注入，来自申报人）——
    invoice_input: dict            # 作用：标准输入 DTO（见 core/uploader）

    # —— 分类结果（路由依据）——
    business_type: str             # 作用：业务方向；业务：travel/procurement...

    # —— 各节点产出（按节点写入）——
    invoice_data: dict             # OCR 结构化数据（识别节点写）
    verification: dict             # 验真结果（验真节点写）
    advance_application: Optional[dict]  # 命中的事前申请（advance_check 写）
    compliance_checks: list        # 合规检查项（合规节点写）
    approval_chain: list           # 计划审批链（审批链节点写）
    summary: str                   # 审核总结（总结节点写）

    # —— 业务闭环（退回/作废）——
    return_reason: Optional[dict]  # 退回原因 {category, message, suggestion}
    process_status: str            # in_review / returned / approved / voided

    # —— 执行记录（驾驶舱数据基础）——
    approval_records: list         # 各级审批执行记录 {role, decision, actor, time}
```

### 7.2 数据流规则

- **单向写**：每个节点只写自己负责的字段（如 OCR 只写 `invoice_data`），不改他人字段。
- **只读输入**：节点读取上游字段用于判断，但不回写上游字段。
- **跨模块**：节点之间不直接传对象，全部通过 State 传递（低耦合的关键）。

---

## 8. 异常与业务闭环规范

| 场景 | 处理方式 | 回传 |
|------|---------|------|
| 可修复业务问题（识别失败/过期/缺申请） | `return_reason` 结构化原因 + `process_status="returned"` | 通知报销人 + 可重提 |
| 最终否决（领导不批） | `process_status="voided"` + `void_reason` | 通知**全部审批链角色** + 终止 |
| 工具异常（OCR 调用失败） | 标记风险 → 重试一次 → 仍失败转人工 | 风险标记入总结 |
| 未知业务方向 | `UnknownBusinessError` | 返回「无法识别业务类型」 |

```python
# shared/policies/errors.py
class UnknownBusinessError(Exception):
    """未知业务方向异常"""
    # 业务：申报人提交了未注册的业务类型，或 classify 判定失败
    def __init__(self, direction: str):
        self.direction = direction
        super().__init__(f"未知业务方向: {direction}")
```

---

## 9. 配置规范（政策规则 YAML）

```yaml
# policy/travel.yaml —— 差旅政策规则（配置驱动，不写死在代码）
# 业务：企业差旅制度数字化；改动制度只需改 YAML，不动代码
hotel_daily_limit: 500          # 住宿每日上限（元）
transport_standard: economy     # 交通标准：economy=经济舱/二等座
advance_required: true          # 差旅是否必须事前申请
advance_valid_days: 30          # 出差申请有效期（天）
approval_rules:                 # 审批链规则（金额阈值驱动）
  - max_amount: 2000
    chain: ["直属上级", "财务"]
  - min_amount: 2000
    chain: ["直属上级", "部门负责人", "财务"]
```

```python
# shared/policies/loader.py
def load_policy(direction: str) -> dict:
    """加载业务政策规则"""
    # 作用：从 policy/<direction>.yaml 读取规则
    # 业务：一个业务一个 YAML；配置驱动，改制度不改代码
    with open(f"app/policy/{direction}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
```

---

## 10. 最小闭环代码骨架（差旅，规范落地示例）

展示本规范如何落到第一条闭环（`travel`）上：

```
app/graphs/businesses/travel/
├── module.py        # TravelModule（build_graph / policy_rules / bind_tools）
├── nodes.py         # 子图节点：识别→验真→事前申请→合规→审批链
├── policies.py      # 差旅专属规则读取（或复用 shared/policies）
└── tools.py         # 差旅专属工具（如出差申请校验）
```

```python
# graphs/businesses/travel/module.py
from graphs.businesses import BusinessModule


class TravelModule(BusinessModule):
    """差旅报销业务模块（本期第一条闭环）"""
    # 作用：业务标识，注册表路由依据
    # 业务：申报人提交方向=travel 时，总控图路由到本模块
    name = "travel"
    # 作用：接受的发票类型清单
    # 业务：差旅场景常见票种；用于识别节点的类型校验
    invoice_types = ["火车票", "机票", "酒店发票", "打车行程单"]

    def build_graph(self):
        # 作用：构建差旅子图（状态图）
        # 业务：子图节点链 = 识别→验真→事前申请→合规→审批链，见 nodes.py
        from graphs.base_subgraph import build_subgraph
        from . import nodes
        return build_subgraph(
            name=self.name,
            nodes=[
                nodes.recognize,          # 识别
                nodes.verify,             # 验真/查重
                nodes.match_advance,      # 事前申请匹配
                nodes.check_compliance,   # 合规（日期/标准）
                nodes.build_approval_chain,  # 审批链
            ],
        )

    def policy_rules(self):
        # 作用：加载差旅政策规则
        # 业务：住宿标准/有效期/审批链阈值，全部来自 policy/travel.yaml
        from shared.policies.loader import load_policy
        return load_policy("travel")

    def bind_tools(self):
        # 作用：绑定差旅子图需要的工具
        # 业务：差旅无需联合审批/三单匹配，基础工具即可
        from tools import ocr_tool, verify_tool, advance_tool, notify_tool, email_tool
        return [ocr_tool, verify_tool, advance_tool, notify_tool, email_tool]
```

---

## 11. 扩展业务的操作清单（对维护者的承诺）

新增一个业务（如 `procurement`），只需：

1. 新建 `graphs/businesses/procurement/` 包（module/nodes/policies）；
2. 新建 `policy/procurement.yaml` 政策规则；
3. `registry.py` 注册 `"procurement": ProcurementModule()`；
4. （可选）新增该业务专属工具。

**主框架（总控图 / State / 适配器 / api）零改动** —— 这就是插件式扩展。

---

## 12. 评审清单（提交代码前自查）

- [ ] 依赖方向是否单向？（无业务模块互相 import）
- [ ] 是否用了 `core/uploader.py` 处理上传，而非各写各的？
- [ ] 被 2+ 业务使用的能力是否已抽到共享层？
- [ ] 政策/规则是否在 YAML，而非硬编码？
- [ ] 每处业务判断是否有「业务：」注释说明对应制度？
- [ ] 新业务是否走注册表，未改主框架？
- [ ] 异常是走 `return_reason` / `void_process`，而非静默失败？
- [ ] 是否产生了没有调用者的函数/参数/抽象？（不必要即删）
- [ ] 是否存在多余的防御分支（try/except、多环境适配）或未使用的 import？

---

## 13. 代码简洁：不要复杂化（硬性规则）

> 复杂化的代价比写慢的代价大得多：**能少写就少写，能简单就不复杂**。本节是硬性规则，违反即评审不通过。

| 规则 | 具体含义 |
|------|---------|
| **不生成不必要的函数** | 函数必须有明确职责 + 真实调用者；没有调用者的函数、一次性 helper、调试函数一律不写，写完即删 |
| **不做防御性过度设计** | 不为「可能出现的环境」写 try/except、多环境适配、锦上添花的参数——真实出问题了再加 |
| **能用官方 API 就不手写** | 标准库/框架已提供的能力直接用，不写自创实现；官方没有的 API 不臆造 |
| **不提前抽象** | 只有被 2+ 处真正复用的能力才抽公共层；单处使用就地写，不抽类、不建目录 |
| **删无价值代码** | 调试输出、临时打印、未使用的 import/变量/参数，提交前必须清掉 |

### 13.1 反例与正例

```python
# ✗ 反例：为一个不存在的环境写满防御逻辑 + 自创 API
def display(graph):                    # ← 无调用者，langgraph 官方也没有这个 API
    from pathlib import Path
    out = ...                          # 各种 try/except、Jupyter 判断、落盘……二十行
    return out

# ✓ 正例：需要画图就一行调官方 API；不需要就不写这个函数
png = graph.get_graph().draw_mermaid_png()
```

### 13.2 判定口诀

> 写代码前先问三句：**这个函数有必要吗？能更短吗？删掉有损失吗？** 三句里有任何一句答不上来，就不写。
